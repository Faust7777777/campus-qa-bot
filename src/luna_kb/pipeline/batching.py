from __future__ import annotations

import json
import shutil
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..contracts import KnowledgeTask, LunaSourceResult, utc_now
from ..errors import BuildError, ContractError
from .review import load_luna_results
from .tasks import categorize_luna_task

DEFAULT_LANES = ("core_kb", "secondary_kb", "current_web")
UNDERGRAD_TITLE_HINTS = (
    "本科",
    "大学生",
    "新生",
    "学生工作",
    "奖学金",
    "助学",
    "勤工",
    "宿舍",
    "校园卡",
    "体测",
    "转专业",
    "课程重修",
    "考试",
    "成绩",
)
NON_UNDERGRAD_TITLE_HINTS = (
    "博士",
    "研究生",
    "硕士",
    "教职工",
    "教师",
    "科研项目",
    "安全漏洞",
)
ACTION_ORDER = {
    "verify_refresh_and_extract": 0,
    "official_search_and_verify": 1,
    "fetch_and_extract": 2,
    "fetch_and_classify_current": 3,
    "fetch_and_classify_history": 4,
}


@dataclass(frozen=True, slots=True)
class TaskBatch:
    lane: str
    action: str
    index: int
    tasks: tuple[KnowledgeTask, ...]

    @property
    def filename(self) -> str:
        return f"batch_{self.index:04d}_{self.lane}_{self.action}.jsonl"


def _product_sort_key(task: KnowledgeTask) -> tuple[int, int, int, str]:
    title = task.title
    explicitly_undergraduate = "本科" in title or "大学生" in title
    if any(hint in title for hint in NON_UNDERGRAD_TITLE_HINTS) and not explicitly_undergraduate:
        scope_rank = 2
    elif any(hint in title for hint in UNDERGRAD_TITLE_HINTS):
        scope_rank = 0
    else:
        scope_rank = 1
    return (
        scope_rank,
        ACTION_ORDER.get(task.action, 99),
        task.priority,
        task.source_id,
    )


def _task_payload_chars(task: KnowledgeTask) -> int:
    return sum(
        len(value)
        for value in (
            task.source_id,
            task.dataset,
            task.title,
            task.canonical_url,
            task.seed_description,
            task.seed_query,
            task.action,
        )
    )


def _chunk_tasks(
    tasks: list[KnowledgeTask],
    batch_size: int,
    max_batch_chars: int | None,
) -> Iterable[tuple[KnowledgeTask, ...]]:
    current: list[KnowledgeTask] = []
    current_chars = 0
    for task in tasks:
        task_chars = _task_payload_chars(task)
        exceeds_count = len(current) >= batch_size
        exceeds_chars = bool(
            current
            and max_batch_chars is not None
            and current_chars + task_chars > max_batch_chars
        )
        if exceeds_count or exceeds_chars:
            yield tuple(current)
            current = []
            current_chars = 0
        current.append(task)
        current_chars += task_chars
    if current:
        yield tuple(current)


def partition_tasks(
    tasks: Iterable[KnowledgeTask],
    batch_size: int = 12,
    lanes: Sequence[str] = DEFAULT_LANES,
    actions: Sequence[str] | None = None,
    max_batch_chars: int | None = None,
) -> list[TaskBatch]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if max_batch_chars is not None and max_batch_chars <= 0:
        raise ValueError("max_batch_chars must be positive")
    selected_lanes = tuple(dict.fromkeys(lanes))
    selected_actions = tuple(dict.fromkeys(actions)) if actions is not None else None
    grouped: dict[tuple[str, str], list[KnowledgeTask]] = {}
    for task in tasks:
        lane = categorize_luna_task(task)
        if lane not in selected_lanes:
            continue
        if selected_actions is not None and task.action not in selected_actions:
            continue
        grouped.setdefault((lane, task.action), []).append(task)

    if selected_actions is None:
        observed_actions = {action for _, action in grouped}
        action_order = tuple(
            sorted(observed_actions, key=lambda action: (ACTION_ORDER.get(action, 99), action))
        )
    else:
        action_order = selected_actions

    batches: list[TaskBatch] = []
    index = 1
    for lane in selected_lanes:
        for action in action_order:
            ordered = sorted(
                grouped.get((lane, action), []),
                key=_product_sort_key,
            )
            for chunk in _chunk_tasks(ordered, batch_size, max_batch_chars):
                batches.append(
                    TaskBatch(
                        lane,
                        action,
                        index,
                        chunk,
                    )
                )
                index += 1
    return batches


def load_task_package(path: Path) -> list[KnowledgeTask]:
    tasks: list[KnowledgeTask] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                task = KnowledgeTask(**json.loads(line))
                task.validate()
            except Exception as exc:
                raise BuildError(f"{path}:{line_number}: {exc}") from exc
            tasks.append(task)
    return tasks


def prepare_luna_workspace(
    task_path: Path,
    workspace: Path,
    instructions: Path,
    clean_instructions: Path,
    mcporter_config: Path | None = None,
    *,
    batch_size: int = 12,
    lanes: Sequence[str] = DEFAULT_LANES,
    actions: Sequence[str] | None = None,
    max_batch_chars: int | None = None,
    escape_non_ascii: bool = True,
) -> dict[str, Any]:
    if workspace.exists() and any(workspace.iterdir()):
        raise BuildError(f"refusing to overwrite non-empty Luna workspace: {workspace}")
    tasks = load_task_package(task_path)
    batches = partition_tasks(
        tasks,
        batch_size=batch_size,
        lanes=lanes,
        actions=actions,
        max_batch_chars=max_batch_chars,
    )
    for name in ("inputs", "outputs", "reports", "logs", "state"):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(instructions, workspace / "WORKER_PROTOCOL.md")
    shutil.copy2(clean_instructions, workspace / "CLEAN_PROTOCOL.md")
    if mcporter_config is not None:
        (workspace / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy2(mcporter_config, workspace / "config" / "mcporter.json")
    for batch in batches:
        target = workspace / "inputs" / batch.filename
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            for task in batch.tasks:
                handle.write(
                    json.dumps(
                        asdict(task),
                        ensure_ascii=escape_non_ascii,
                        sort_keys=True,
                    )
                    + "\n"
                )
    all_lane_counts = Counter(categorize_luna_task(task) for task in tasks)
    selected_counts = Counter(batch.lane for batch in batches for _ in batch.tasks)
    manifest = {
        "generated_at": utc_now(),
        "source_task_package": str(task_path.resolve()),
        "batch_size": batch_size,
        "max_batch_chars": max_batch_chars,
        "escape_non_ascii": escape_non_ascii,
        "mcporter_configured": mcporter_config is not None,
        "selected_lanes": list(lanes),
        "selected_actions": list(actions) if actions is not None else None,
        "input_task_count": len(tasks),
        "selected_task_count": sum(selected_counts.values()),
        "batch_count": len(batches),
        "all_lane_counts": dict(sorted(all_lane_counts.items())),
        "selected_lane_counts": dict(sorted(selected_counts.items())),
        "selected_action_counts": dict(
            sorted(Counter(task.action for batch in batches for task in batch.tasks).items())
        ),
        "faculty_included": 0,
    }
    (workspace / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _production_domain(value: str) -> bool:
    host = value.lower().strip(".")
    return (
        host == "dlut.edu.cn"
        or host.endswith(".dlut.edu.cn")
        or host == "mp.weixin.qq.com"
    )


def validate_luna_batch(input_path: Path, output_path: Path) -> dict[str, Any]:
    expected = load_task_package(input_path)
    expected_ids = {task.source_id for task in expected}
    expected_by_id = {task.source_id: task for task in expected}
    errors: list[str] = []
    warnings: list[str] = []
    results: list[LunaSourceResult] = []
    try:
        with output_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    results.append(LunaSourceResult.from_dict(json.loads(line)))
                except (ContractError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    errors.append(f"{output_path}:{line_number}: {exc}")
    except OSError as exc:
        errors.append(str(exc))

    counts = Counter(result.fetch_status for result in results)
    result_ids = [result.source_id for result in results]
    duplicate_source_ids = sorted(
        source_id for source_id, count in Counter(result_ids).items() if count > 1
    )
    actual_ids = set(result_ids)
    missing = sorted(expected_ids - actual_ids)
    unexpected = sorted(actual_ids - expected_ids)
    card_ids: list[str] = []
    for result in results:
        task = expected_by_id.get(result.source_id)
        if "faculty" in result.source_id.lower() or result.dataset == "kb_faculty":
            errors.append(f"faculty boundary violation: {result.source_id}")
        if task is not None and task.action == "verify_refresh_and_extract":
            if result.title != task.title:
                errors.append(f"offline source title changed or was corrupted: {result.source_id}")
            if result.canonical_url != task.canonical_url:
                errors.append(f"offline canonical URL changed: {result.source_id}")
            seed_has_cjk = any("\u3400" <= char <= "\u9fff" for char in task.seed_description)
            output_has_cjk = any("\u3400" <= char <= "\u9fff" for char in result.clean_text)
            if seed_has_cjk and result.clean_text and not output_has_cjk:
                errors.append(f"offline Chinese text appears corrupted: {result.source_id}")
        if task is not None:
            input_title_has_cjk = any("\u3400" <= char <= "\u9fff" for char in task.title)
            output_title_has_cjk = any("\u3400" <= char <= "\u9fff" for char in result.title)
            output_text_has_cjk = any("\u3400" <= char <= "\u9fff" for char in result.clean_text)
            if input_title_has_cjk and not output_title_has_cjk:
                errors.append(f"Chinese source title appears corrupted: {result.source_id}")
            if input_title_has_cjk and result.clean_text and not output_text_has_cjk:
                errors.append(f"Chinese source text appears corrupted: {result.source_id}")
        if result.canonical_url:
            host = (urlsplit(result.canonical_url).hostname or "").lower()
            if not _production_domain(host):
                errors.append(f"non-official source domain: {result.source_id} -> {host}")
        if len(result.candidate_cards) > 4:
            errors.append(f"more than 4 cards: {result.source_id}")
        if result.fetch_status == "success" and not result.candidate_cards:
            warnings.append(f"successful source has no candidate cards: {result.source_id}")
        for card in result.candidate_cards:
            card_ids.append(card.card_id)
            if card.card_kind == "fact" and card.evidence_quote not in result.clean_text:
                errors.append(
                    f"evidence quote is not an exact source substring: {result.source_id}/{card.card_id}"
                )
            if card.card_kind == "fact" and not card.generated_questions:
                errors.append(f"fact card has no generated questions: {card.card_id}")
    duplicate_card_ids = sorted(
        card_id for card_id, count in Counter(card_ids).items() if count > 1
    )
    if duplicate_source_ids:
        errors.append("duplicate source ids")
    if missing:
        errors.append("missing source results")
    if unexpected:
        errors.append("unexpected source results")
    if duplicate_card_ids:
        errors.append("duplicate card ids")
    return {
        "generated_at": utc_now(),
        "passed": not errors,
        "input_count": len(expected),
        "output_count": len(results),
        "status_counts": dict(sorted(counts.items())),
        "card_count": len(card_ids),
        "missing_source_ids": missing,
        "unexpected_source_ids": unexpected,
        "duplicate_source_ids": duplicate_source_ids,
        "duplicate_card_ids": duplicate_card_ids,
        "errors": errors,
        "warnings": warnings,
    }


def collect_luna_workspace(
    workspace: Path,
    output: Path,
    *,
    allow_partial: bool = False,
) -> dict[str, Any]:
    input_paths = sorted((workspace / "inputs").glob("batch_*.jsonl"))
    if not input_paths:
        raise BuildError(f"no Luna input batches found in {workspace}")
    if output.exists():
        raise BuildError(f"refusing to overwrite collected output: {output}")

    accepted_text: list[str] = []
    accepted_source_ids: set[str] = set()
    accepted_card_ids: set[str] = set()
    duplicate_card_ids: set[str] = set()
    failed_batches: list[str] = []
    status_counts: Counter[str] = Counter()
    card_count = 0
    for input_path in input_paths:
        output_path = workspace / "outputs" / input_path.name
        if not output_path.is_file():
            failed_batches.append(input_path.name)
            continue
        report = validate_luna_batch(input_path, output_path)
        if not report["passed"]:
            failed_batches.append(input_path.name)
            continue
        results = load_luna_results(output_path)
        duplicate_ids = sorted(
            result.source_id for result in results if result.source_id in accepted_source_ids
        )
        if duplicate_ids:
            raise BuildError(f"duplicate source IDs across Luna batches: {duplicate_ids[:5]}")
        accepted_source_ids.update(result.source_id for result in results)
        for result in results:
            for card in result.candidate_cards:
                if card.card_id in accepted_card_ids:
                    duplicate_card_ids.add(card.card_id)
                accepted_card_ids.add(card.card_id)
        status_counts.update(result.fetch_status for result in results)
        card_count += sum(len(result.candidate_cards) for result in results)
        text = output_path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        accepted_text.append(text)

    if failed_batches and not allow_partial:
        raise BuildError(
            f"{len(failed_batches)} Luna batches are missing or invalid; use --allow-partial only for inspection"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(accepted_text), encoding="utf-8", newline="\n")
    return {
        "generated_at": utc_now(),
        "complete": not failed_batches,
        "input_batch_count": len(input_paths),
        "accepted_batch_count": len(input_paths) - len(failed_batches),
        "failed_batches": failed_batches,
        "source_count": len(accepted_source_ids),
        "card_count": card_count,
        "duplicate_card_ids": sorted(duplicate_card_ids),
        "status_counts": dict(sorted(status_counts.items())),
        "output": str(output.resolve()),
    }

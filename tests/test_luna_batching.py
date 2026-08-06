from luna_kb.contracts import KnowledgeTask, LunaSourceResult, content_digest
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from luna_kb.pipeline.batching import (
    collect_luna_workspace,
    partition_tasks,
    prepare_luna_workspace,
    validate_luna_batch,
)
from luna_kb.errors import BuildError
from luna_kb.pipeline.tasks import make_rescue_search_tasks


def make_task(source_id: str, dataset: str, query: str, published_at=None) -> KnowledgeTask:
    return KnowledgeTask(
        source_id=source_id,
        dataset=dataset,
        title=source_id,
        canonical_url="https://example.dlut.edu.cn/item",
        seed_description="",
        seed_query=query,
        published_at=published_at,
        action="fetch_and_extract",
        priority=0,
    )


def test_partitioned_luna_batches_keep_product_lanes_separate() -> None:
    tasks = [
        make_task("web_plus_index:current", "web_plus_index", "", "2026-01-01"),
        make_task("kb_clean:core-1", "kb_clean", "奖学金怎么申请"),
        make_task("kb_clean:core-2", "kb_clean", "转专业"),
        make_task("kb_clean:staff", "kb_clean", "差旅费报销标准"),
    ]

    batches = partition_tasks(tasks, batch_size=1, lanes=("core_kb", "current_web"))

    assert [[task.source_id for task in batch.tasks] for batch in batches] == [
        ["kb_clean:core-1"],
        ["kb_clean:core-2"],
        ["web_plus_index:current"],
    ]
    assert [batch.lane for batch in batches] == ["core_kb", "core_kb", "current_web"]


def test_partitioned_luna_batches_never_mix_actions() -> None:
    clean = make_task("kb_clean:clean", "kb_clean", "奖学金怎么申请")
    clean.action = "verify_refresh_and_extract"
    fetch = make_task("kb_clean:fetch", "kb_clean", "转专业")

    batches = partition_tasks([fetch, clean], batch_size=10, lanes=("core_kb",))

    assert [(batch.action, len(batch.tasks)) for batch in batches] == [
        ("verify_refresh_and_extract", 1),
        ("fetch_and_extract", 1),
    ]
    assert all({task.action for task in batch.tasks} == {batch.action} for batch in batches)


def test_partitioned_luna_batches_respect_payload_budget() -> None:
    first = make_task("kb_clean:first", "kb_clean", "奖学金怎么申请")
    second = make_task("kb_clean:second", "kb_clean", "转专业")
    first.title = "本科生奖学金申请"
    second.title = "本科生转专业申请"
    first.seed_description = "甲" * 100
    second.seed_description = "乙" * 100

    batches = partition_tasks(
        [first, second],
        batch_size=10,
        lanes=("core_kb",),
        max_batch_chars=150,
    )

    assert [len(batch.tasks) for batch in batches] == [1, 1]


def test_prepare_workspace_uses_ascii_json_and_copies_both_protocols(tmp_path: Path) -> None:
    task_path = tmp_path / "tasks.jsonl"
    workspace = tmp_path / "workspace"
    online_protocol = tmp_path / "online.md"
    clean_protocol = tmp_path / "clean.md"
    mcporter_config = tmp_path / "mcporter.json"
    task = make_task("kb_clean:core", "kb_clean", "奖学金怎么申请")
    task.action = "verify_refresh_and_extract"
    task.seed_description = "本科生奖学金"
    task_path.write_text(json.dumps(asdict(task), ensure_ascii=False) + "\n", encoding="utf-8")
    online_protocol.write_text("online", encoding="utf-8")
    clean_protocol.write_text("offline", encoding="utf-8")
    mcporter_config.write_text('{"mcpServers": {}}', encoding="utf-8")

    manifest = prepare_luna_workspace(
        task_path,
        workspace,
        online_protocol,
        clean_protocol,
        mcporter_config,
        batch_size=10,
        lanes=("core_kb",),
        actions=("verify_refresh_and_extract",),
    )

    batch_path = next((workspace / "inputs").glob("*.jsonl"))
    batch_text = batch_path.read_text(encoding="utf-8")
    assert "本科生" not in batch_text
    assert "\\u672c\\u79d1\\u751f" in batch_text
    assert batch_path.name.endswith("_verify_refresh_and_extract.jsonl")
    assert (workspace / "WORKER_PROTOCOL.md").read_text(encoding="utf-8") == "online"
    assert (workspace / "CLEAN_PROTOCOL.md").read_text(encoding="utf-8") == "offline"
    assert (workspace / "config" / "mcporter.json").read_text(encoding="utf-8") == '{"mcpServers": {}}'
    assert manifest["mcporter_configured"] is True
    assert manifest["selected_action_counts"] == {"verify_refresh_and_extract": 1}


def test_luna_batch_validation_rejects_missing_source_results(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    item = make_task("kb_clean:core", "kb_clean", "奖学金怎么申请")
    input_path.write_text(
        json.dumps(asdict(item), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    output_path.write_text("", encoding="utf-8")

    report = validate_luna_batch(input_path, output_path)

    assert report["passed"] is False
    assert report["missing_source_ids"] == ["kb_clean:core"]


def test_successful_luna_result_requires_an_official_source_url(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    task = make_task("kb_clean:no-url", "kb_clean", "奖学金怎么申请")
    task.canonical_url = ""
    input_path.write_text(
        json.dumps(asdict(task), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    clean_text = "本科生奖学金申请以官方通知为准。"
    output_path.write_text(
        json.dumps(
            {
                "source_id": task.source_id,
                "dataset": task.dataset,
                "canonical_url": "",
                "title": task.title,
                "official_domain": "",
                "published_at": None,
                "fetched_at": None,
                "content_hash": content_digest(clean_text),
                "clean_text": clean_text,
                "fetch_status": "success",
                "candidate_cards": [],
                "unresolved_questions": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    report = validate_luna_batch(input_path, output_path)

    assert report["passed"] is False
    assert any("canonical_url" in error for error in report["errors"])


def test_undergraduate_source_title_is_processed_before_graduate_noise() -> None:
    graduate = make_task("kb_clean:a-graduate", "kb_clean", "奖学金怎么申请")
    graduate.title = "博士研究生奖学金延期申请通知"
    undergraduate = make_task("kb_clean:z-undergraduate", "kb_clean", "奖学金怎么申请")
    undergraduate.title = "本科生国家奖学金评审通知"

    batches = partition_tasks(
        [graduate, undergraduate],
        batch_size=2,
        lanes=("core_kb",),
    )

    assert [task.source_id for task in batches[0].tasks] == [
        "kb_clean:z-undergraduate",
        "kb_clean:a-graduate",
    ]


def test_offline_validation_rejects_mojibake_title_and_text(tmp_path: Path) -> None:
    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    task = make_task("kb_clean:encoding", "kb_clean", "奖学金怎么申请")
    task.action = "verify_refresh_and_extract"
    task.title = "校园卡充值"
    task.seed_description = "校园卡可以在线充值。"
    input_path.write_text(
        json.dumps(asdict(task), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    corrupted = "Ð£Ô°¿¨³äÖµ"
    output_path.write_text(
        json.dumps(
            {
                "source_id": task.source_id,
                "dataset": task.dataset,
                "canonical_url": task.canonical_url,
                "title": corrupted,
                "official_domain": "example.dlut.edu.cn",
                "published_at": None,
                "fetched_at": None,
                "content_hash": content_digest(corrupted),
                "clean_text": corrupted,
                "fetch_status": "success",
                "candidate_cards": [],
                "unresolved_questions": [],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    report = validate_luna_batch(input_path, output_path)

    assert report["passed"] is False
    assert any("corrupted" in error for error in report["errors"])


def test_collect_revalidates_and_refuses_an_incomplete_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "inputs").mkdir(parents=True)
    (workspace / "outputs").mkdir()
    task = make_task("kb_clean:missing", "kb_clean", "奖学金怎么申请")
    task.action = "verify_refresh_and_extract"
    (workspace / "inputs" / "batch_0001_core_kb_verify_refresh_and_extract.jsonl").write_text(
        json.dumps(asdict(task), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BuildError, match="missing or invalid"):
        collect_luna_workspace(workspace, tmp_path / "collected.jsonl")

    report = collect_luna_workspace(
        workspace,
        tmp_path / "partial.jsonl",
        allow_partial=True,
    )

    assert report["complete"] is False
    assert report["source_count"] == 0


def test_collect_merges_a_currently_valid_batch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "inputs").mkdir(parents=True)
    (workspace / "outputs").mkdir()
    filename = "batch_0001_core_kb_verify_refresh_and_extract.jsonl"
    task = make_task("kb_clean:valid", "kb_clean", "奖学金怎么申请")
    task.action = "verify_refresh_and_extract"
    task.seed_description = "证据不足"
    (workspace / "inputs" / filename).write_text(
        json.dumps(asdict(task), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    result = {
        "source_id": task.source_id,
        "dataset": task.dataset,
        "canonical_url": task.canonical_url,
        "title": task.title,
        "official_domain": "example.dlut.edu.cn",
        "published_at": None,
        "fetched_at": None,
        "content_hash": content_digest(""),
        "clean_text": "",
        "fetch_status": "unresolved",
        "candidate_cards": [],
        "unresolved_questions": ["快照证据不足"],
    }
    (workspace / "outputs" / filename).write_text(
        json.dumps(result, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    report = collect_luna_workspace(workspace, tmp_path / "collected.jsonl")

    assert report["complete"] is True
    assert report["source_count"] == 1
    assert report["status_counts"] == {"unresolved": 1}


def test_failed_url_fetch_is_converted_to_official_search_without_reusing_dead_url() -> None:
    task = make_task("kb_clean:dead-link", "kb_clean", "转专业")
    result = LunaSourceResult(
        source_id=task.source_id,
        dataset=task.dataset,
        canonical_url=task.canonical_url,
        title=task.title,
        official_domain="example.dlut.edu.cn",
        published_at=None,
        fetched_at=None,
        content_hash=content_digest(""),
        clean_text="",
        fetch_status="fetch_failed",
        candidate_cards=[],
        unresolved_questions=["404"],
    )

    rescue = make_rescue_search_tasks([task], [result])

    assert len(rescue) == 1
    assert rescue[0].source_id == task.source_id
    assert rescue[0].action == "official_search_and_verify"
    assert rescue[0].canonical_url == ""
    assert task.canonical_url in rescue[0].seed_description

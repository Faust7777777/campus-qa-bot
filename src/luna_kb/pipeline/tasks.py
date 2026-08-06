from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import asdict, replace
from pathlib import Path

from ..contracts import LunaSourceResult, TaskStatus, KnowledgeTask, canonicalize_url, stable_id
from ..errors import ContractError


CORE_UNDERGRAD_QUERIES = frozenset(
    {
        "志愿者活动",
        "心理咨询",
        "社团注册",
        "勤工助学",
        "户口迁移",
        "讲座信息",
        "新生报到流程",
        "推免预报名流程",
        "推免生预报名流程",
        "党组织关系转接",
        "就业指导中心",
        "奖学金怎么申请",
        "奖学金评定标准",
        "校医院挂号",
        "食堂消费问题",
        "图书馆预约座位",
        "图书馆开放时间",
        "体测",
        "校园卡挂失补办流程",
        "助学贷款",
        "重修补考",
        "宿舍报修流程",
        "新生体检",
        "学费缴纳",
        "成绩查询",
        "校园网连不上怎么办",
        "锐捷认证失败怎么办",
        "体育场馆预约",
        "转专业",
        "大学生医保",
        "考试安排",
    }
)
DEFERRED_STAFF_QUERIES = frozenset(
    {
        "信息化项目验收流程",
        "一张表是什么",
        "分析测试中心怎么预约",
        "差旅费报销标准",
    }
)


def categorize_luna_task(task: KnowledgeTask) -> str:
    """Assign product-first processing lanes without mutating source records."""
    if task.dataset == "kb_clean":
        if task.seed_query in DEFERRED_STAFF_QUERIES:
            return "deferred_kb"
        if task.seed_query in CORE_UNDERGRAD_QUERIES:
            return "core_kb"
        return "secondary_kb"
    published = task.published_at or ""
    try:
        year = int(published[:4])
    except ValueError:
        year = 0
    return "current_web" if year >= 2024 else "archive_web"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if "faculty" in path.name.lower():
        raise ContractError(f"faculty isolation file cannot be a production input: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def make_kb_tasks(path: Path) -> list[KnowledgeTask]:
    tasks: list[KnowledgeTask] = []
    for row in _read_csv(path):
        title = (row.get("title") or "").strip()
        url = canonicalize_url(row.get("url") or "") if row.get("url") else ""
        description = (row.get("desc") or "").strip()
        query = (row.get("query") or "").strip()
        if not title:
            continue
        if url and not description:
            action, priority = "fetch_and_extract", 0
        elif description and not url:
            action, priority = "official_search_and_verify", 0
        elif url and description:
            action, priority = "verify_refresh_and_extract", 1
        else:
            action, priority = "resolve_missing_source", 2
        identity = url or f"{title}\x1f{description}"
        task = KnowledgeTask(
            source_id=f"kb_clean:{stable_id('src', identity)}",
            dataset="kb_clean",
            title=title,
            canonical_url=url,
            seed_description=description,
            seed_query=query,
            published_at=None,
            action=action,
            priority=priority,
        )
        task.validate()
        tasks.append(task)
    return tasks


def make_web_tasks(path: Path) -> tuple[list[KnowledgeTask], int]:
    rows = _read_csv(path)
    tasks: list[KnowledgeTask] = []
    seen_urls: set[str] = set()
    duplicate_count = 0
    for row in rows:
        title = (row.get("title") or "").strip()
        raw_url = (row.get("url") or "").strip()
        if not title or not raw_url:
            continue
        url = canonicalize_url(raw_url)
        if url in seen_urls:
            duplicate_count += 1
            continue
        seen_urls.add(url)
        published = (row.get("publishTime") or "").strip() or None
        try:
            year = int(published[:4]) if published else 0
        except ValueError:
            year = 0
        current_priority = 0 if 2024 <= year <= 2026 else 2
        action = "fetch_and_classify_current" if current_priority == 0 else "fetch_and_classify_history"
        article_id = (row.get("articleId") or "").strip()
        source_suffix = article_id or stable_id("src", url)
        task = KnowledgeTask(
            source_id=f"web_plus_index:{source_suffix}",
            dataset="web_plus_index",
            title=title,
            canonical_url=url,
            seed_description="",
            seed_query="",
            published_at=published,
            action=action,
            priority=current_priority,
        )
        task.validate()
        tasks.append(task)
    tasks.sort(key=lambda item: (item.priority, item.published_at or "", item.title), reverse=False)
    return tasks, duplicate_count


def write_tasks(tasks: Iterable[KnowledgeTask], output: Path) -> dict[str, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for task in tasks:
            task.validate()
            counts[task.action] = counts.get(task.action, 0) + 1
            handle.write(json.dumps(asdict(task), ensure_ascii=False, sort_keys=True) + "\n")
    return counts


def make_rescue_search_tasks(
    tasks: list[KnowledgeTask],
    results: list[LunaSourceResult],
) -> list[KnowledgeTask]:
    originals = {task.source_id: task for task in tasks}
    rescue: list[KnowledgeTask] = []
    seen: set[str] = set()
    for result in results:
        if result.fetch_status != "fetch_failed":
            continue
        if result.source_id in seen:
            raise ContractError(f"duplicate failed source result: {result.source_id}")
        seen.add(result.source_id)
        original = originals.get(result.source_id)
        if original is None:
            raise ContractError(f"failed source has no original task: {result.source_id}")
        if original.dataset != result.dataset:
            raise ContractError(f"failed source dataset changed: {result.source_id}")
        locator_hint = (
            f"原始失效链接（仅供定位，不作为事实证据）：{original.canonical_url}"
            if original.canonical_url
            else ""
        )
        seed_description = "\n".join(
            value for value in (original.seed_description, locator_hint) if value
        )
        item = replace(
            original,
            canonical_url="",
            seed_description=seed_description,
            action="official_search_and_verify",
            priority=0,
            status=TaskStatus.PENDING,
        )
        item.validate()
        rescue.append(item)
    return rescue


def generate_task_package(kb_path: Path, web_path: Path, output: Path) -> dict[str, object]:
    kb_tasks = make_kb_tasks(kb_path)
    web_tasks, duplicate_urls = make_web_tasks(web_path)
    all_tasks = sorted([*kb_tasks, *web_tasks], key=lambda item: (item.priority, item.dataset, item.source_id))
    counts = write_tasks(all_tasks, output)
    return {
        "total": len(all_tasks),
        "kb_clean": len(kb_tasks),
        "web_plus_index": len(web_tasks),
        "duplicate_web_urls_removed": duplicate_urls,
        "actions": counts,
        "faculty_included": 0,
    }

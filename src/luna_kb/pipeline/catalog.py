from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..contracts import (
    CandidateCard,
    KnowledgeTask,
    LunaSourceResult,
    ReviewedCard,
    RiskLevel,
    SourceAuthority,
    Validity,
    canonicalize_url,
    content_digest,
    stable_id,
)
from ..errors import ContractError


NON_UNDERGRADUATE_TITLE_RE = re.compile(
    r"研究生|硕士|博士|博士后|博后|留学生|教职工|教工|教师|导师|职称|"
    r"人事|工会|离退休|复试|调剂|MBA|EMBA|MPA|MPAcc|专业学位|科研项目",
    re.IGNORECASE,
)


def _official_dlut_host(host: str) -> bool:
    host = host.lower().strip(".")
    return host == "dlut.edu.cn" or host.endswith(".dlut.edu.cn")


def _catalog_alias(title: str) -> str:
    return re.sub(r"^【[^】]{1,20}】\s*", "", title).strip()


def _catalog_campus(title: str) -> str:
    if "盘锦" in title:
        return "盘锦"
    if "开发区" in title:
        return "开发区"
    if "凌水" in title:
        return "凌水"
    return ""


def _outside_undergraduate_scope(title: str) -> bool:
    return "本科" not in title and NON_UNDERGRADUATE_TITLE_RE.search(title) is not None


def make_source_catalog(
    path: Path,
    *,
    as_of_year: int | None = None,
) -> tuple[list[LunaSourceResult], dict[str, Any]]:
    as_of_year = as_of_year or datetime.now().year
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    sources: list[LunaSourceResult] = []
    seen_urls: set[str] = set()
    skipped_external = skipped_platform = skipped_invalid = skipped_scope = duplicate_urls = 0
    for row in rows:
        title = (row.get("title") or "").strip()
        raw_url = (row.get("url") or "").strip()
        if not title or not raw_url:
            skipped_invalid += 1
            continue
        if _outside_undergraduate_scope(title):
            skipped_scope += 1
            continue
        try:
            url = canonicalize_url(raw_url)
        except (ContractError, TypeError, ValueError):
            skipped_invalid += 1
            continue
        host = (urlsplit(url).hostname or "").lower()
        if host == "mp.weixin.qq.com":
            skipped_platform += 1
            continue
        if not _official_dlut_host(host):
            skipped_external += 1
            continue
        if url in seen_urls:
            duplicate_urls += 1
            continue
        seen_urls.add(url)

        published_at = (row.get("publishTime") or "").strip() or None
        try:
            published_year = int(published_at[:4]) if published_at else 0
        except ValueError:
            published_year = 0
        validity = (
            Validity.HISTORICAL
            if published_year and published_year < as_of_year
            else Validity.UNKNOWN
        )
        article_id = (row.get("articleId") or "").strip()
        source_id = f"web_plus_index:{article_id or stable_id('src', url)}"
        alias = _catalog_alias(title)
        card = CandidateCard(
            card_id=stable_id("card", source_id, "catalog"),
            title=title,
            standard_question="",
            summary="",
            evidence_quote="",
            source_locator="",
            generated_questions=[],
            aliases=[alias] if alias and alias != title else [],
            risk_level=RiskLevel.LOW,
            extraction_confidence=1.0,
            retrieval_text="\n".join(value for value in (title, published_at or "") if value),
            keywords=[],
            facts={},
            facets=[],
            campus=_catalog_campus(title),
            audience="本科生",
            validity=validity,
            subject_key=stable_id("subject", url),
            fact_key="",
            source_authority=SourceAuthority.NEWS,
            card_kind="navigation",
        )
        sources.append(
            LunaSourceResult(
                source_id=source_id,
                dataset="web_plus_index",
                canonical_url=url,
                title=title,
                official_domain=host,
                published_at=published_at,
                fetched_at=None,
                content_hash=content_digest(""),
                clean_text="",
                fetch_status="catalog_only",
                candidate_cards=[card],
                unresolved_questions=["文章正文尚未抓取；该记录仅可用于官方页面导航。"],
            )
        )

    return sources, {
        "input_rows": len(rows),
        "catalog_sources": len(sources),
        "as_of_year": as_of_year,
        "skipped_external": skipped_external,
        "skipped_platform": skipped_platform,
        "skipped_invalid": skipped_invalid,
        "skipped_scope": skipped_scope,
        "duplicate_urls": duplicate_urls,
    }


def select_catalog_upgrade_tasks(
    tasks: list[KnowledgeTask],
    reviewed_catalog: list[ReviewedCard],
    *,
    include_historical: bool = False,
) -> list[KnowledgeTask]:
    selected_ids = {
        item.source.source_id
        for item in reviewed_catalog
        if include_historical or item.card.validity != Validity.HISTORICAL
    }
    return [task for task in tasks if task.source_id in selected_ids]

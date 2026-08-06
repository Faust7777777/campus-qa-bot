from __future__ import annotations

import copy
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..contracts import (
    CandidateCard,
    LunaSourceResult,
    ReviewStatus,
    ReviewedCard,
    RiskLevel,
    Validity,
    json_dumps,
    normalized_text,
    utc_now,
)
from ..errors import ContractError

MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
UNDERGRADUATE_AUDIENCES = {
    "",
    "student",
    "undergraduate",
    "本科",
    "本科生",
    "全校",
    "全体学生",
    "all students",
}
CAMPUS_ORDER = ("凌水", "开发区", "盘锦")
CAMPUS_ALIASES = {
    "": "",
    "全校": "",
    "campus-wide": "",
    "campuswide": "",
    "凌水": "凌水",
    "凌水校区": "凌水",
    "凌水主校区": "凌水",
    "主校区": "凌水",
    "开发区": "开发区",
    "开发区校区": "开发区",
    "大连开发区校区": "开发区",
    "盘锦": "盘锦",
    "盘锦校区": "盘锦",
}
PLATFORM_HOSTS_REQUIRING_MANUAL_REVIEW = {"mp.weixin.qq.com"}


@dataclass(slots=True)
class ReviewDecision:
    source_id: str
    card_id: str
    action: str
    reason: str
    reviewer: str
    field_overrides: dict[str, Any]


def _official_domain(domain: str) -> bool:
    domain = domain.lower().strip(".")
    return domain == "dlut.edu.cn" or domain.endswith(".dlut.edu.cn") or domain == "mp.weixin.qq.com"


def _quote_is_evidence(source: LunaSourceResult, card: CandidateCard) -> bool:
    quote = card.evidence_quote.strip()
    return bool(quote) and len(normalized_text(quote)) >= 8 and quote in source.clean_text


def _has_mobile(card: CandidateCard) -> bool:
    payload = " ".join(
        [card.summary, card.evidence_quote, card.retrieval_text, json_dumps(card.facts)]
    )
    return MOBILE_RE.search(payload) is not None


def _navigation_card(card: CandidateCard) -> CandidateCard:
    return replace(
        card,
        card_kind="navigation",
        summary="",
        facts={},
        facets=[],
        evidence_quote="",
        source_locator="",
        retrieval_text="",
        risk_level=RiskLevel.LOW,
        fact_key="",
        embedding=None,
    )


def _normalize_card_scope(card: CandidateCard) -> tuple[CandidateCard, str | None]:
    audience = card.audience.strip()
    if audience.lower() not in UNDERGRADUATE_AUDIENCES:
        return card, f"unsupported audience: {audience or '<empty>'}"
    campus = card.campus.strip()
    raw_campuses = [value.strip() for value in re.split(r"[|、,，/]+", campus) if value.strip()]
    if not raw_campuses:
        normalized_campus = ""
    else:
        normalized_values: set[str] = set()
        for value in raw_campuses:
            if value not in CAMPUS_ALIASES:
                return card, f"unsupported campus: {campus}"
            normalized = CAMPUS_ALIASES[value]
            if not normalized:
                normalized_campus = ""
                break
            normalized_values.add(normalized)
        else:
            normalized_campus = "|".join(
                value for value in CAMPUS_ORDER if value in normalized_values
            )
    return replace(card, audience="本科生", campus=normalized_campus), None


def load_luna_results(path: Path) -> list[LunaSourceResult]:
    results: list[LunaSourceResult] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                results.append(LunaSourceResult.from_dict(json.loads(line)))
            except (json.JSONDecodeError, ContractError, TypeError, ValueError) as exc:
                raise ContractError(f"{path}:{line_number}: {exc}") from exc
    return results


def merge_reviewed_files(paths: list[Path]) -> tuple[list[ReviewedCard], dict[str, Any]]:
    if not paths:
        raise ContractError("at least one reviewed input is required")
    source_order: list[str] = []
    source_items: dict[str, list[ReviewedCard]] = {}
    source_fingerprints: dict[str, str] = {}
    seen_cards: dict[str, str] = {}

    def append_item(item: ReviewedCard) -> None:
        owner = seen_cards.get(item.card.card_id)
        if owner is not None:
            raise ContractError(f"duplicate reviewed card_id: {item.card.card_id}")
        seen_cards[item.card.card_id] = item.source.source_id
        source_items[item.source.source_id].append(item)

    for path in paths:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    item = ReviewedCard.from_dict(json.loads(line))
                except (json.JSONDecodeError, ContractError, KeyError, TypeError, ValueError) as exc:
                    raise ContractError(f"{path}:{line_number}: {exc}") from exc
                source_id = item.source.source_id
                fingerprint = json_dumps(asdict(item.source))
                if source_id not in source_items:
                    source_order.append(source_id)
                    source_items[source_id] = []
                    source_fingerprints[source_id] = fingerprint
                    append_item(item)
                    continue

                existing_item = source_items[source_id][0]
                existing = source_fingerprints[source_id]
                if existing == fingerprint:
                    append_item(item)
                    continue
                if (
                    existing_item.source.dataset != item.source.dataset
                    or existing_item.source.canonical_url != item.source.canonical_url
                ):
                    raise ContractError(
                        f"source revision changed identity: {source_id}"
                    )
                old_status = existing_item.source.fetch_status
                new_status = item.source.fetch_status
                if old_status == "catalog_only" and new_status == "success":
                    for old in source_items[source_id]:
                        seen_cards.pop(old.card.card_id, None)
                    source_items[source_id] = []
                    source_fingerprints[source_id] = fingerprint
                    append_item(item)
                    continue
                if old_status == "success" and new_status == "catalog_only":
                    continue
                raise ContractError(f"inconsistent reviewed source revision: {source_id}")

    merged = [item for source_id in source_order for item in source_items[source_id]]

    engine = ReviewEngine()
    engine._resolve_conflicts(merged)
    report = engine.report(merged, len(source_items))
    report["input_files"] = [str(path.resolve()) for path in paths]
    return merged, report


def load_decisions(path: Path | None) -> dict[tuple[str, str], ReviewDecision]:
    if path is None:
        return {}
    decisions: dict[tuple[str, str], ReviewDecision] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            data = json.loads(line)
            decision = ReviewDecision(
                source_id=str(data["source_id"]),
                card_id=str(data["card_id"]),
                action=str(data["action"]),
                reason=str(data.get("reason", "manual review")),
                reviewer=str(data.get("reviewer", "codex")),
                field_overrides=dict(data.get("field_overrides", {})),
            )
            if decision.action not in {"approve", "downgrade", "reject"}:
                raise ContractError(f"{path}:{line_number}: invalid review action")
            key = (decision.source_id, decision.card_id)
            if key in decisions:
                raise ContractError(f"duplicate review decision for {key}")
            decisions[key] = decision
    return decisions


class ReviewEngine:
    def __init__(self, reviewer: str = "codex") -> None:
        self.reviewer = reviewer
        self.conflicts: list[dict[str, Any]] = []
        self.mirror_duplicates: list[dict[str, str]] = []

    def _deduplicate_mirrored_sources(
        self,
        sources: list[LunaSourceResult],
        decisions: dict[tuple[str, str], ReviewDecision],
    ) -> list[LunaSourceResult]:
        groups: dict[tuple[str, str, str, str], list[LunaSourceResult]] = defaultdict(list)
        passthrough: list[LunaSourceResult] = []
        for source in sources:
            if source.fetch_status != "success" or not source.clean_text:
                passthrough.append(source)
                continue
            fingerprint = (
                source.dataset,
                source.content_hash,
                normalized_text(source.title),
                json_dumps([asdict(card) for card in source.candidate_cards]),
            )
            groups[fingerprint].append(source)

        kept_ids = {id(source) for source in passthrough}
        decision_source_ids = {source_id for source_id, _ in decisions}
        for group in groups.values():
            explicitly_reviewed = [
                source for source in group if source.source_id in decision_source_ids
            ]
            if len(explicitly_reviewed) > 1:
                raise ContractError(
                    "mirrored sources have decisions on multiple source identities"
                )
            kept = (
                explicitly_reviewed[0]
                if explicitly_reviewed
                else min(group, key=lambda source: (source.canonical_url, source.source_id))
            )
            kept_ids.add(id(kept))
            for duplicate in group:
                if duplicate is kept:
                    continue
                self.mirror_duplicates.append(
                    {
                        "content_hash": kept.content_hash,
                        "kept_source_id": kept.source_id,
                        "kept_url": kept.canonical_url,
                        "dropped_source_id": duplicate.source_id,
                        "dropped_url": duplicate.canonical_url,
                    }
                )
        return [source for source in sources if id(source) in kept_ids]

    def review(
        self,
        sources: list[LunaSourceResult],
        decisions: dict[tuple[str, str], ReviewDecision] | None = None,
    ) -> list[ReviewedCard]:
        decisions = decisions or {}
        sources = self._deduplicate_mirrored_sources(sources, decisions)
        reviewed: list[ReviewedCard] = []
        seen_cards: set[str] = set()
        used_decisions: set[tuple[str, str]] = set()
        for source in sources:
            source.validate()
            if source.fetch_status not in {"success", "catalog_only"}:
                continue
            for original_card in source.candidate_cards:
                if original_card.card_id in seen_cards:
                    raise ContractError(f"duplicate card_id: {original_card.card_id}")
                seen_cards.add(original_card.card_id)
                decision = decisions.get((source.source_id, original_card.card_id))
                if decision is not None:
                    used_decisions.add((source.source_id, original_card.card_id))
                card = original_card
                if decision and decision.field_overrides:
                    raw = asdict(card)
                    raw.update(copy.deepcopy(decision.field_overrides))
                    card = CandidateCard.from_dict(raw)
                card, scope_error = _normalize_card_scope(card)
                status, reason, reviewer = self._review_one(
                    source, card, decision, scope_error
                )
                if status == ReviewStatus.DOWNGRADED:
                    card = _navigation_card(card)
                reviewed.append(
                    ReviewedCard(source, card, status, reason, reviewer, utc_now())
                )
        unused_decisions = sorted(set(decisions) - used_decisions)
        if unused_decisions:
            raise ContractError(f"review decisions did not match candidate cards: {unused_decisions[:5]}")
        self._resolve_conflicts(reviewed)
        return reviewed

    def _review_one(
        self,
        source: LunaSourceResult,
        card: CandidateCard,
        decision: ReviewDecision | None,
        scope_error: str | None,
    ) -> tuple[str, str, str]:
        if not source.canonical_url or not _official_domain(source.official_domain):
            return ReviewStatus.REJECTED, "missing or non-official canonical source", self.reviewer
        host = (urlsplit(source.canonical_url).hostname or "").lower()
        if host != source.official_domain:
            return ReviewStatus.REJECTED, "official domain mismatch", self.reviewer
        if not card.title:
            return ReviewStatus.REJECTED, "card title is required", self.reviewer
        evidence_ok = _quote_is_evidence(source, card)
        navigation_has_claim_material = bool(
            card.summary
            or card.evidence_quote
            or card.source_locator
            or card.facts
            or card.facets
        )
        if decision:
            if decision.action == "reject":
                return ReviewStatus.REJECTED, decision.reason, decision.reviewer
            if scope_error:
                return ReviewStatus.REJECTED, scope_error, decision.reviewer
            if decision.action == "downgrade":
                return ReviewStatus.DOWNGRADED, decision.reason, decision.reviewer
            if not evidence_ok and card.card_kind != "navigation":
                return ReviewStatus.DOWNGRADED, "manual approval lacked direct evidence; navigation only", decision.reviewer
            if _has_mobile(card):
                return ReviewStatus.DOWNGRADED, "personal mobile cannot be a durable official contact", decision.reviewer
            if card.card_kind == "navigation" and navigation_has_claim_material:
                return ReviewStatus.DOWNGRADED, "navigation card carried claim material; sanitized", decision.reviewer
            return ReviewStatus.APPROVED, decision.reason, decision.reviewer
        if scope_error:
            return ReviewStatus.REJECTED, scope_error, self.reviewer
        if source.official_domain in PLATFORM_HOSTS_REQUIRING_MANUAL_REVIEW:
            return (
                ReviewStatus.PENDING,
                "platform-hosted source requires explicit official-account review",
                self.reviewer,
            )
        if card.card_kind == "navigation":
            if navigation_has_claim_material:
                return ReviewStatus.DOWNGRADED, "navigation card carried claim material; sanitized", self.reviewer
            return ReviewStatus.APPROVED, "official navigation card passed consistency checks", self.reviewer
        if not evidence_ok:
            return ReviewStatus.DOWNGRADED, "direct evidence incomplete; navigation only", self.reviewer
        if not card.subject_key or not card.fact_key:
            return ReviewStatus.PENDING, "fact card lacks conflict identity", self.reviewer
        if card.validity == Validity.UNKNOWN:
            return ReviewStatus.PENDING, "fact validity is unknown", self.reviewer
        if _has_mobile(card):
            return ReviewStatus.DOWNGRADED, "personal mobile cannot be a durable official contact", self.reviewer
        if card.extraction_confidence < 0.8:
            return ReviewStatus.PENDING, "extraction confidence below automatic threshold", self.reviewer
        if card.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH}:
            return ReviewStatus.PENDING, "policy/procedure/detail card requires explicit batch review", self.reviewer
        return ReviewStatus.PENDING, "fact card requires explicit Codex review", self.reviewer

    def _resolve_conflicts(self, reviewed: list[ReviewedCard]) -> None:
        groups: dict[tuple[str, str], list[ReviewedCard]] = defaultdict(list)
        for item in reviewed:
            if (
                item.review_status == ReviewStatus.APPROVED
                and item.card.subject_key
                and item.card.card_kind == "fact"
            ):
                groups[(item.card.subject_key, item.card.fact_key)].append(item)
        for (subject_key, fact_key), items in groups.items():
            fact_versions = {json_dumps(item.card.facts) for item in items}
            if len(fact_versions) <= 1:
                continue
            for item in items:
                item.review_status = ReviewStatus.PENDING
                item.review_reason = (
                    "conflicting facts require explicit winner and loser decisions"
                )
            outcome = "all_pending_manual_decision"
            self.conflicts.append(
                {
                    "subject_key": subject_key,
                    "fact_key": fact_key,
                    "card_ids": [item.card.card_id for item in items],
                    "outcome": outcome,
                }
            )

    def report(self, reviewed: list[ReviewedCard], source_count: int) -> dict[str, Any]:
        counts = Counter(item.review_status for item in reviewed)
        fetch_failures = max(
            0,
            source_count
            - len({item.source.source_id for item in reviewed})
            - len(self.mirror_duplicates),
        )
        return {
            "generated_at": utc_now(),
            "source_count": source_count,
            "card_count": len(reviewed),
            "approved": counts[ReviewStatus.APPROVED],
            "rewritten": sum(1 for item in reviewed if "manual" in item.review_reason),
            "downgraded": counts[ReviewStatus.DOWNGRADED],
            "rejected": counts[ReviewStatus.REJECTED],
            "pending": counts[ReviewStatus.PENDING],
            "conflict_count": len(self.conflicts),
            "conflicts": self.conflicts,
            "mirror_duplicate_count": len(self.mirror_duplicates),
            "mirror_duplicates": self.mirror_duplicates,
            "fetch_failures": fetch_failures,
        }


def write_reviewed(path: Path, reviewed: list[ReviewedCard]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in reviewed:
            item.validate()
            handle.write(json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

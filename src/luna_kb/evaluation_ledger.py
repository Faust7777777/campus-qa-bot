from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from .contracts import canonicalize_url
from .errors import BuildError


EVALUATION_KINDS = frozenset(
    {"answerable", "historical", "no_answer", "out_of_scope", "faculty_boundary"}
)
NEGATIVE_KINDS = frozenset({"no_answer", "out_of_scope", "faculty_boundary"})
LEDGER_FIELDS = frozenset(
    {
        "id",
        "kind",
        "input_sha256",
        "outcome",
        "latency_seconds",
        "expected_card_ids",
        "expected_urls",
        "recall_at_50_hit",
        "recall_at_5_hit",
        "answer_card_match",
        "first_stage_ids",
        "reranked_ids",
        "cited_card_ids",
        "source_urls",
        "unsupported_conclusion",
        "fabricated_link_count",
        "failure_reasons",
    }
)


def evaluation_case_input_sha256(item: dict[str, Any]) -> str:
    """Bind a ledger row to its private input without retaining question text."""

    payload = {
        "id": str(item.get("id", "")).strip(),
        "kind": str(item.get("kind", "")).strip(),
        "question": str(item.get("question", "")).strip(),
        "history": item.get("history", []),
        "expected_card_ids": sorted(
            {str(value).strip() for value in item.get("expected_card_ids", [])}
        ),
        "expected_urls": sorted(
            {
                canonicalize_url(str(value).strip())
                for value in item.get("expected_urls", [])
            }
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def case_ledger_sha256(entries: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _official(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "dlut.edu.cn" or host.endswith(".dlut.edu.cn") or host == "mp.weixin.qq.com"


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]


def summarize_case_ledger(entries: Any) -> dict[str, Any]:
    if not isinstance(entries, list) or not entries:
        raise BuildError("evaluation report lacks a non-empty case ledger")

    seen_ids: set[str] = set()
    kind_counts: Counter[str] = Counter()
    latencies: list[float] = []
    recall50_hits = recall5_hits = answer_card_hits = 0
    positive_total = 0
    source_total = official_sources = 0
    unsupported = fabricated = 0
    restrained = {kind: 0 for kind in NEGATIVE_KINDS}
    gold_card_ids: set[str] = set()
    gold_urls: set[str] = set()

    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or set(entry) != LEDGER_FIELDS:
            raise BuildError(f"case ledger row {index} has invalid fields")
        case_id = entry["id"]
        kind = entry["kind"]
        outcome = entry["outcome"]
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            raise BuildError(f"case ledger row {index} has an invalid or duplicate id")
        if kind not in EVALUATION_KINDS or outcome not in {
            "answered",
            "insufficient",
            "error",
        }:
            raise BuildError(f"case ledger row {index} has an invalid kind or outcome")
        seen_ids.add(case_id)
        kind_counts[kind] += 1
        if not isinstance(entry["input_sha256"], str) or len(entry["input_sha256"]) != 64:
            raise BuildError(f"case ledger row {index} has an invalid input checksum")
        try:
            int(entry["input_sha256"], 16)
        except ValueError as exc:
            raise BuildError(
                f"case ledger row {index} has an invalid input checksum"
            ) from exc

        latency = entry["latency_seconds"]
        if type(latency) not in {int, float} or not math.isfinite(float(latency)) or latency < 0:
            raise BuildError(f"case ledger row {index} has invalid latency")
        latencies.append(float(latency))

        for field, maximum in (
            ("expected_card_ids", 20),
            ("expected_urls", 20),
            ("first_stage_ids", 50),
            ("reranked_ids", 5),
            ("cited_card_ids", 4),
            ("source_urls", 4),
            ("failure_reasons", 20),
        ):
            values = entry[field]
            if (
                not isinstance(values, list)
                or len(values) > maximum
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                raise BuildError(f"case ledger row {index} has invalid {field}")

        for field in ("expected_urls", "source_urls"):
            for url in entry[field]:
                try:
                    if canonicalize_url(url) != url:
                        raise ValueError("URL is not canonical")
                except Exception as exc:
                    raise BuildError(
                        f"case ledger row {index} has an invalid {field} URL"
                    ) from exc

        first_stage_ids = set(entry["first_stage_ids"])
        reranked_ids = set(entry["reranked_ids"])
        cited_card_ids = set(entry["cited_card_ids"])
        retrieval_ids_consistent = reranked_ids.issubset(
            first_stage_ids
        ) and cited_card_ids.issubset(first_stage_ids)

        if type(entry["unsupported_conclusion"]) is not bool:
            raise BuildError(f"case ledger row {index} has invalid unsupported flag")
        fabricated_count = entry["fabricated_link_count"]
        if type(fabricated_count) is not int or fabricated_count < 0:
            raise BuildError(f"case ledger row {index} has invalid fabricated link count")
        unsupported += int(entry["unsupported_conclusion"])
        fabricated += fabricated_count
        source_total += len(entry["source_urls"])
        official_sources += sum(_official(url) for url in entry["source_urls"])

        flags = (
            entry["recall_at_50_hit"],
            entry["recall_at_5_hit"],
            entry["answer_card_match"],
        )
        if kind in NEGATIVE_KINDS:
            if entry["expected_card_ids"] or entry["expected_urls"]:
                raise BuildError(f"negative case ledger row {index} contains gold data")
            if any(value is not None for value in flags):
                raise BuildError(f"negative case ledger row {index} contains positive metrics")
            restrained[kind] += int(outcome == "insufficient")
        else:
            if not entry["expected_card_ids"]:
                raise BuildError(f"positive case ledger row {index} lacks gold data")
            if any(type(value) is not bool for value in flags):
                raise BuildError(f"positive case ledger row {index} lacks boolean metrics")
            expected_ids = set(entry["expected_card_ids"])
            computed_flags = (
                bool(expected_ids & first_stage_ids),
                bool(expected_ids & reranked_ids),
                bool(expected_ids & cited_card_ids),
            )
            if flags != computed_flags:
                raise BuildError(
                    f"positive case ledger row {index} metrics do not match retrieval ids"
                )
            expected_urls = set(entry["expected_urls"])
            computed_fabricated = (
                sum(url not in expected_urls for url in entry["source_urls"])
                if expected_urls
                else 0
            )
            if fabricated_count != computed_fabricated:
                raise BuildError(
                    f"positive case ledger row {index} has an invalid fabricated link count"
                )
            gold_card_ids.update(expected_ids)
            gold_urls.update(expected_urls)
            positive_total += 1
            recall50_hits += int(flags[0])
            recall5_hits += int(flags[1])
            answer_card_hits += int(flags[2])

        result_fields = (
            entry["first_stage_ids"],
            entry["reranked_ids"],
            entry["cited_card_ids"],
            entry["source_urls"],
        )
        if outcome == "answered":
            if not entry["cited_card_ids"] or not entry["source_urls"]:
                raise BuildError(
                    f"answered case ledger row {index} lacks a provenance-bearing answer"
                )
        elif any(result_fields) or fabricated_count:
            raise BuildError(
                f"unanswered case ledger row {index} contains answer results"
            )
        if outcome == "error" and not entry["unsupported_conclusion"]:
            raise BuildError(f"error case ledger row {index} lacks failure evidence")
        if not retrieval_ids_consistent and not entry["unsupported_conclusion"]:
            raise BuildError(
                f"case ledger row {index} hides inconsistent retrieval ids"
            )

    all_kind_counts = {kind: kind_counts.get(kind, 0) for kind in sorted(EVALUATION_KINDS)}
    denominators = {
        "recall_at_50": positive_total,
        "recall_at_5": positive_total,
        "answer_card_match_rate": positive_total,
        "official_source_rate": source_total,
        "no_answer_restraint": all_kind_counts["no_answer"],
        "out_of_scope_restraint": all_kind_counts["out_of_scope"],
        "faculty_boundary_restraint": all_kind_counts["faculty_boundary"],
    }

    def rate(numerator: int, denominator: int, empty: float) -> float:
        return numerator / denominator if denominator else empty

    metrics = {
        "question_count": len(entries),
        "recall_at_50": rate(recall50_hits, positive_total, 0.0),
        "recall_at_5": rate(recall5_hits, positive_total, 0.0),
        "answer_card_match_rate": rate(answer_card_hits, positive_total, 0.0),
        "official_source_rate": rate(official_sources, source_total, 1.0),
        "unsupported_conclusions": unsupported,
        "fabricated_links": fabricated,
        "no_answer_restraint": rate(
            restrained["no_answer"], all_kind_counts["no_answer"], 1.0
        ),
        "out_of_scope_restraint": rate(
            restrained["out_of_scope"], all_kind_counts["out_of_scope"], 1.0
        ),
        "faculty_boundary_restraint": rate(
            restrained["faculty_boundary"],
            all_kind_counts["faculty_boundary"],
            1.0,
        ),
        "p95_latency_seconds": _p95(latencies),
        "mean_latency_seconds": statistics.fmean(latencies),
    }
    return {
        "kind_counts": all_kind_counts,
        "metric_denominators": denominators,
        "metrics": metrics,
        "gold_counts": {
            "positive_case_count": positive_total,
            "gold_card_count": len(gold_card_ids),
            "gold_url_count": len(gold_urls),
        },
    }

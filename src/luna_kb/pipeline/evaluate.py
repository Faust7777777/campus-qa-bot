from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..attestation import ArtifactSnapshot
from ..contracts import canonicalize_url, normalized_text, utc_now
from ..evaluation_policy import EvaluationThresholds, FORMAL_MINIMUM_KIND_COUNTS
from ..evaluation_ledger import (
    EVALUATION_KINDS,
    NEGATIVE_KINDS,
    case_ledger_sha256,
    evaluation_case_input_sha256,
    summarize_case_ledger,
)
from ..errors import BuildError, InsufficientEvidence, RetrievalUnavailable
from ..service import AnswerService


NAVIGATION_ANSWER = "已定位到相关官方页面，具体内容请以该页面最新说明为准。"
def load_evaluation_set_snapshot(
    snapshot: ArtifactSnapshot, minimum: int = 300
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_cases: set[tuple[str, str]] = set()
    for line_number, line in enumerate(snapshot.text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BuildError(
                f"{snapshot.name}:{line_number}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(item, dict):
            raise BuildError(
                f"{snapshot.name}:{line_number}: evaluation row must be an object"
            )
        if not all(
            isinstance(item.get(field), str) and item[field].strip()
            for field in ("id", "question", "kind")
        ):
            raise BuildError(
                f"{snapshot.name}:{line_number}: id/question/kind are required"
            )
        try:
            item_id = item["id"].strip()
            if item_id in seen_ids:
                raise BuildError(
                    f"{snapshot.name}:{line_number}: duplicate evaluation id: {item_id}"
                )
            seen_ids.add(item_id)
            item["id"] = item_id
            item["question"] = item["question"].strip()
            if len(item["question"]) > 500:
                raise BuildError(
                    f"{snapshot.name}:{line_number}: question exceeds runtime limit"
                )
            if item["kind"] not in EVALUATION_KINDS:
                raise BuildError(
                    f"{snapshot.name}:{line_number}: unsupported kind: {item['kind']}"
                )
            for field in ("expected_card_ids", "expected_urls"):
                values = item.get(field, [])
                if not isinstance(values, list) or any(
                    not isinstance(value, str) or not value.strip() for value in values
                ):
                    raise BuildError(
                        f"{snapshot.name}:{line_number}: {field} must be a string array"
                    )
                normalized_values = list(dict.fromkeys(value.strip() for value in values))
                if len(normalized_values) != len(values):
                    raise BuildError(
                        f"{snapshot.name}:{line_number}: {field} contains duplicates"
                    )
                item[field] = normalized_values
            if item["kind"] not in NEGATIVE_KINDS and not item["expected_card_ids"]:
                raise BuildError(
                    f"{snapshot.name}:{line_number}: positive question requires expected_card_ids"
                )
            if item["kind"] not in NEGATIVE_KINDS and not item["expected_urls"]:
                raise BuildError(
                    f"{snapshot.name}:{line_number}: positive question requires expected_urls"
                )
            if item["kind"] in NEGATIVE_KINDS and item["expected_card_ids"]:
                raise BuildError(
                    f"{snapshot.name}:{line_number}: negative question cannot expect production cards"
                )
            if item["kind"] in NEGATIVE_KINDS and item["expected_urls"]:
                raise BuildError(
                    f"{snapshot.name}:{line_number}: negative question cannot expect production URLs"
                )
            for url in item["expected_urls"]:
                try:
                    canonical = canonicalize_url(url)
                except Exception as exc:
                    raise BuildError(
                        f"{snapshot.name}:{line_number}: invalid expected URL"
                    ) from exc
                if not _official(canonical):
                    raise BuildError(
                        f"{snapshot.name}:{line_number}: expected URL is not official: {url}"
                    )
            history = item.get("history", [])
            if not isinstance(history, list) or len(history) > 6 or len(history) % 2:
                raise BuildError(
                    f"{snapshot.name}:{line_number}: history must contain at most 3 complete turns"
                )
            for index, message in enumerate(history):
                expected_role = "user" if index % 2 == 0 else "assistant"
                if (
                    not isinstance(message, dict)
                    or message.get("role") != expected_role
                    or not isinstance(message.get("content"), str)
                    or not message["content"].strip()
                ):
                    raise BuildError(
                        f"{snapshot.name}:{line_number}: invalid history message {index}"
                    )
            case_key = (
                normalized_text(item["question"]),
                json.dumps(history, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )
            if case_key in seen_cases:
                raise BuildError(
                    f"{snapshot.name}:{line_number}: duplicate normalized evaluation case"
                )
            seen_cases.add(case_key)
            items.append(item)
        except BuildError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BuildError(
                f"{snapshot.name}:{line_number}: invalid evaluation row: {exc}"
            ) from exc
    if len(items) < minimum:
        raise BuildError(f"evaluation set has {len(items)} questions; at least {minimum} are required")
    if minimum >= 300:
        counts = Counter(str(item["kind"]) for item in items)
        deficits = {
            kind: required - counts.get(kind, 0)
            for kind, required in FORMAL_MINIMUM_KIND_COUNTS.items()
            if counts.get(kind, 0) < required
        }
        if deficits:
            detail = ", ".join(f"{kind}缺{count}" for kind, count in deficits.items())
            raise BuildError(f"evaluation set does not meet formal kind quotas: {detail}")
    return items


def load_evaluation_set(path: Path, minimum: int = 300) -> list[dict[str, Any]]:
    return load_evaluation_set_snapshot(ArtifactSnapshot.from_path(path), minimum)


def _official(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "dlut.edu.cn" or host.endswith(".dlut.edu.cn") or host == "mp.weixin.qq.com"


def _answer_sentences(value: str) -> list[str]:
    return [piece.strip() for piece in re.split(r"(?<=[。！？!?])|\n+", value) if piece.strip()]


def _answer_provenance_is_consistent(answer: Any) -> bool:
    """Check provenance and link ownership without judging paraphrase.

    Semantic faithfulness is not reliably decidable with substring rules.  The
    draft mode therefore records a review signal and only keeps deterministic
    invariants here: cited cards must come from the retrieval result and every
    displayed URL must belong to one of those cards.
    """
    try:
        sources = list(answer.sources)
        cited_ids = {str(value) for value in answer.cited_card_ids}
        cards = list(answer.retrieval.cards)
        text = str(answer.answer).strip()
    except Exception:
        return False
    if not text or not sources or not cited_ids:
        return False
    card_map = {str(card.card_id): card for card in cards}
    if not cited_ids.issubset(card_map):
        return False
    cited_urls = {
        canonicalize_url(str(card_map[card_id].canonical_url)) for card_id in cited_ids
    }
    for source in sources:
        source_card_id = str(getattr(source, "card_id", ""))
        if source_card_id not in cited_ids:
            return False
        if canonicalize_url(str(source.url)) not in cited_urls:
            return False
    return True


def validate_evaluation_gold(
    database: Any, items: list[dict[str, Any]]
) -> dict[str, int]:
    expected_ids = sorted(
        {
            str(card_id)
            for item in items
            for card_id in item.get("expected_card_ids", [])
        }
    )
    card_urls: dict[str, str] = {}
    for offset in range(0, len(expected_ids), 500):
        chunk = expected_ids[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = database.connection.execute(
            "SELECT c.card_id,s.canonical_url FROM cards c "
            "JOIN sources s ON s.source_id=c.source_id "
            f"WHERE c.card_id IN ({placeholders})",
            chunk,
        ).fetchall()
        card_urls.update(
            (str(row["card_id"]), canonicalize_url(str(row["canonical_url"])))
            for row in rows
        )
    missing = sorted(set(expected_ids) - set(card_urls))
    if missing:
        raise BuildError(f"evaluation gold cards are absent from this release: {missing[:5]}")

    positive_cases = 0
    url_mismatches: list[str] = []
    for item in items:
        if item["kind"] in NEGATIVE_KINDS:
            continue
        positive_cases += 1
        expected_card_urls = {
            card_urls[str(card_id)] for card_id in item["expected_card_ids"]
        }
        expected_urls = {
            canonicalize_url(str(url)) for url in item.get("expected_urls", [])
        }
        if expected_card_urls != expected_urls:
            url_mismatches.append(str(item["id"]))
    if url_mismatches:
        raise BuildError(
            "evaluation gold URLs do not match their expected cards: "
            + ", ".join(url_mismatches[:5])
        )
    return {
        "positive_case_count": positive_cases,
        "gold_card_count": len(card_urls),
        "gold_url_count": len(set(card_urls.values())),
        "missing_card_count": 0,
        "url_mismatch_count": 0,
    }


def audit_faculty_isolation(
    database: Any, faculty_input: Path | ArtifactSnapshot
) -> dict[str, Any]:
    snapshot = (
        faculty_input
        if isinstance(faculty_input, ArtifactSnapshot)
        else ArtifactSnapshot.from_path(faculty_input)
    )
    try:
        faculty_text = snapshot.payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BuildError(f"{snapshot.name}: faculty CSV is not valid UTF-8") from exc
    reader = csv.DictReader(io.StringIO(faculty_text, newline=""))
    required_fields = ("title", "url", "desc", "query")
    if tuple(reader.fieldnames or ()) != required_fields:
        raise BuildError(
            f"{snapshot.name}: faculty CSV columns must be {','.join(required_fields)}"
        )
    faculty = list(reader)
    seen_rows: set[tuple[str, ...]] = set()
    for line_number, item in enumerate(faculty, 2):
        if None in item or any(item.get(field) is None for field in required_fields):
            raise BuildError(f"{snapshot.name}:{line_number}: malformed faculty row")
        row_key = tuple((item[field] or "").strip() for field in required_fields)
        if not row_key[3]:
            raise BuildError(f"{snapshot.name}:{line_number}: faculty query is required")
        if row_key in seen_rows:
            raise BuildError(f"{snapshot.name}:{line_number}: duplicate faculty row")
        seen_rows.add(row_key)
    rows = database.connection.execute(
        "SELECT s.dataset,s.source_id,s.canonical_url,s.title AS source_title,"
        "s.evidence_text,c.title,c.standard_question,c.summary,c.evidence_quote,"
        "c.generated_questions,c.aliases,c.keywords,c.facts,c.facets,c.retrieval_text "
        "FROM cards c JOIN sources s ON s.source_id=c.source_id"
    ).fetchall()
    violations: list[str] = []
    production_urls = {canonicalize_url(str(row["canonical_url"])) for row in rows}
    text_fields = (
        "source_title",
        "evidence_text",
        "title",
        "standard_question",
        "summary",
        "evidence_quote",
        "generated_questions",
        "aliases",
        "keywords",
        "facts",
        "facets",
        "retrieval_text",
    )
    production_texts = [
        (
            str(row["source_id"]),
            normalized_text("\n".join(str(row[field]) for field in text_fields)),
        )
        for row in rows
    ]
    for row in rows:
        if "faculty" in str(row["dataset"]).lower() or "faculty" in str(row["source_id"]).lower():
            violations.append(str(row["source_id"]))
    for item in faculty:
        url = (item.get("url") or "").strip()
        if url and canonicalize_url(url) in production_urls:
            violations.append(f"faculty-url:{url}")
        for field, minimum in (("title", 8), ("desc", 20), ("query", 6)):
            raw = item.get(field) or ""
            needle = normalized_text(raw)
            if len(needle) < minimum:
                continue
            needles = {needle}
            if field == "desc" and len(needle) >= 24:
                needles.update(
                    needle[offset : offset + 24]
                    for offset in range(0, len(needle) - 23, 12)
                )
                needles.add(needle[-24:])
            for source_id, production_text in production_texts:
                if any(value in production_text for value in needles):
                    violations.append(f"faculty-{field}:{source_id}:{raw[:80]}")
    return {"checked_rows": len(faculty), "violations": sorted(set(violations))}


async def evaluate(
    items: list[dict[str, Any]],
    service: AnswerService,
    faculty_input: Path | ArtifactSnapshot,
    thresholds: EvaluationThresholds | None = None,
) -> dict[str, Any]:
    thresholds = thresholds or EvaluationThresholds()
    failures: list[dict[str, str]] = []
    case_ledger: list[dict[str, Any]] = []
    for item in items:
        case_id = str(item["id"])
        expected = set(str(value) for value in item.get("expected_card_ids", []))
        expected_urls = {canonicalize_url(str(value)) for value in item.get("expected_urls", [])}
        negative = item["kind"] in NEGATIVE_KINDS
        entry: dict[str, Any] = {
            "id": case_id,
            "kind": str(item["kind"]),
            "input_sha256": evaluation_case_input_sha256(item),
            "outcome": "answered",
            "latency_seconds": 0.0,
            "expected_card_ids": sorted(expected),
            "expected_urls": sorted(expected_urls),
            "recall_at_50_hit": None if negative else False,
            "recall_at_5_hit": None if negative else False,
            "answer_card_match": None if negative else False,
            "first_stage_ids": [],
            "reranked_ids": [],
            "cited_card_ids": [],
            "source_urls": [],
            "unsupported_conclusion": False,
            "fabricated_link_count": 0,
            "failure_reasons": [],
        }

        def fail(reason: str) -> None:
            entry["failure_reasons"].append(reason)
            failures.append({"id": case_id, "reason": reason})

        started = time.perf_counter()
        try:
            answer = await service.ask(str(item["question"]), item.get("history"))
            entry["latency_seconds"] = time.perf_counter() - started
            entry["first_stage_ids"] = [
                str(value) for value in answer.retrieval.trace.first_stage_ids[:50]
            ]
            entry["reranked_ids"] = [
                str(value) for value in answer.retrieval.trace.reranked_ids[:5]
            ]
            entry["cited_card_ids"] = [str(value) for value in answer.cited_card_ids]
            entry["source_urls"] = [canonicalize_url(str(source.url)) for source in answer.sources]
            if not _answer_provenance_is_consistent(answer):
                entry["unsupported_conclusion"] = True
                fail("final answer failed independent provenance audit")
            if bool(getattr(answer, "needs_review", False)):
                entry["unsupported_conclusion"] = True
                notes = ",".join(getattr(answer, "quality_notes", ()) or ())
                fail(f"draft quality signal{(': ' + notes) if notes else ''}")
            if negative:
                fail("negative question was answered")
            else:
                entry["recall_at_50_hit"] = bool(expected & set(entry["first_stage_ids"]))
                entry["recall_at_5_hit"] = bool(expected & set(entry["reranked_ids"]))
                entry["answer_card_match"] = bool(expected & set(entry["cited_card_ids"]))
                if not entry["answer_card_match"]:
                    fail("final answer did not cite an expected card")
                entry["fabricated_link_count"] = (
                    sum(url not in expected_urls for url in entry["source_urls"])
                    if expected_urls
                    else 0
                )
        except InsufficientEvidence:
            entry["latency_seconds"] = time.perf_counter() - started
            entry["outcome"] = "insufficient"
            if not negative:
                fail("insufficient evidence")
        except RetrievalUnavailable as exc:
            raise BuildError(
                f"evaluation aborted because {exc.component} failed ({exc.error_id}); failures cannot count as no-answer"
            ) from exc
        except Exception as exc:
            entry["latency_seconds"] = time.perf_counter() - started
            entry["outcome"] = "error"
            entry["unsupported_conclusion"] = True
            fail(f"answer validation failed: {exc}")
        case_ledger.append(entry)

    isolation = audit_faculty_isolation(service.retriever.database, faculty_input)
    summary = summarize_case_ledger(case_ledger)
    metrics = dict(summary["metrics"])
    metrics["faculty_leakage"] = len(isolation["violations"])
    checks = {
        "recall_at_50": metrics["recall_at_50"] >= thresholds.recall_at_50,
        "recall_at_5": metrics["recall_at_5"] >= thresholds.recall_at_5,
        "answer_card_match_rate": (
            metrics["answer_card_match_rate"] >= thresholds.answer_card_match_rate
        ),
        "official_source_rate": metrics["official_source_rate"] >= thresholds.official_source_rate,
        "unsupported_conclusions": metrics["unsupported_conclusions"] <= thresholds.unsupported_conclusions,
        "fabricated_links": metrics["fabricated_links"] <= thresholds.fabricated_links,
        "faculty_leakage": metrics["faculty_leakage"] <= thresholds.faculty_leakage,
        "no_answer_restraint": metrics["no_answer_restraint"] >= thresholds.no_answer_restraint,
        "out_of_scope_restraint": (
            metrics["out_of_scope_restraint"] >= thresholds.out_of_scope_restraint
        ),
        "faculty_boundary_restraint": (
            metrics["faculty_boundary_restraint"] >= thresholds.faculty_boundary_restraint
        ),
        "p95_latency_seconds": metrics["p95_latency_seconds"] <= thresholds.p95_latency_seconds,
    }
    return {
        "generated_at": utc_now(),
        # ``passed`` is the release-safety result.  Quality checks remain in
        # the report and are intentionally advisory so one imperfect draft or
        # recall miss does not block a usable knowledge update.
        "passed": checks["fabricated_links"] and checks["faculty_leakage"],
        "quality_passed": all(checks.values()),
        "thresholds": asdict(thresholds),
        "metrics": metrics,
        "kind_counts": summary["kind_counts"],
        "metric_denominators": summary["metric_denominators"],
        "checks": checks,
        "faculty_isolation": isolation,
        "case_ledger": case_ledger,
        "case_ledger_sha256": case_ledger_sha256(case_ledger),
        "failures": failures[:100],
    }

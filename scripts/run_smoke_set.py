#!/usr/bin/env python
"""Run the non-circular smoke set against the real gateway.

Why this exists
---------------
``work/evaluation_20260807.jsonl`` cannot measure retrieval quality: all 200 of
its positive questions are verbatim copies of the gold card's own
``standard_question`` / ``generated_questions``, and those fields are indexed.
Its recall numbers are therefore ~100% no matter what retrieval does.

This set is phrased the way the questions actually arrive in a QQ group, and
``--check-only`` proves it, refusing to run if any question has drifted into
matching indexed text.  Run that check whenever the knowledge base changes:
adding a card can make a previously independent question circular.

Expectations are topic keywords rather than gold card ids, so the set stays
usable while the knowledge base is still being filled - a question can be
listed before the card that answers it exists.

The API key is read from the environment and is never written anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from luna_kb.contracts import normalized_text  # noqa: E402
from luna_kb.errors import InsufficientEvidence, RetrievalUnavailable  # noqa: E402
from luna_kb.retrieval import KnowledgeDatabase, StrongRetriever  # noqa: E402
from luna_kb.service import AnswerService  # noqa: E402

MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
LATENCY_BUDGET_SECONDS = 15.0


def load_set(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def check_non_circular(items: list[dict], database: KnowledgeDatabase) -> list[str]:
    """Flag questions that have become verbatim copies of indexed text."""

    indexed: set[str] = set()
    with database._lock:  # noqa: SLF001 - read-only diagnostic over the release
        rows = database.connection.execute(
            "SELECT standard_question, generated_questions FROM cards"
        ).fetchall()
    for row in rows:
        if row["standard_question"]:
            indexed.add(normalized_text(row["standard_question"]))
        for question in json.loads(row["generated_questions"] or "[]"):
            indexed.add(normalized_text(question))

    return [
        f"{item['id']}: {item['question']!r} is verbatim indexed text"
        for item in items
        if normalized_text(item["question"]) in indexed
    ]


def judge(item: dict, answer, elapsed: float) -> tuple[str, str]:
    text = answer.answer
    haystack = " ".join(
        [text] + [f"{s.title} {s.url}" for s in answer.sources]
    )
    if MOBILE_RE.search(text):
        return "FAIL", "answer contains a personal mobile number"
    if elapsed > LATENCY_BUDGET_SECONDS:
        return "FAIL", f"exceeded the {LATENCY_BUDGET_SECONDS:g}s budget"
    if item["kind"] != "answerable":
        # Faculty and out-of-scope questions may still be answered; what must not
        # happen is personal data leaking, which is checked above.
        return "MANUAL", "negative case answered - read it"
    expect = item.get("expect_any") or []
    if not expect:
        return "MANUAL", "no keyword expectation"
    if any(keyword in haystack for keyword in expect):
        return "PASS", f"matched {[k for k in expect if k in haystack][:2]}"
    return "FAIL", f"no expected topic in the answer (wanted one of {expect})"


async def run(items: list[dict], database: KnowledgeDatabase, models) -> int:
    retriever = StrongRetriever(database, models, 0.35, fast_path_enabled=True)
    service = AnswerService(
        retriever, models, cache_ttl_seconds=0, cache_size=0,
        answer_mode="draft", answer_max_chars=300, answer_max_sources=3,
    )
    latencies: list[float] = []
    verdicts: dict[str, int] = {}
    rows: list[tuple[str, str, str, str, float]] = []
    try:
        for item in items:
            started = time.perf_counter()
            try:
                async with asyncio.timeout(LATENCY_BUDGET_SECONDS + 5):
                    answer = await service.ask(item["question"])
                elapsed = time.perf_counter() - started
                verdict, note = judge(item, answer, elapsed)
                summary = answer.answer[:40].replace("\n", " ")
            except InsufficientEvidence:
                elapsed = time.perf_counter() - started
                verdict = "PASS" if item["kind"] != "answerable" else "FAIL"
                note = "declined (insufficient evidence)"
                summary = ""
            except RetrievalUnavailable as exc:
                elapsed = time.perf_counter() - started
                verdict, note, summary = "ERROR", f"{exc.component} failed", ""
            except TimeoutError:
                elapsed = time.perf_counter() - started
                verdict, note, summary = "FAIL", "timed out", ""
            latencies.append(elapsed)
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            rows.append((verdict, item["id"], item.get("status", ""), note, elapsed))
            print(f"{verdict:<6} {item['id']:<22} {elapsed:>5.2f}s  {note[:52]}")
            if summary:
                print(f"       {summary}")
    finally:
        await service.close()

    print("\n" + "=" * 70)
    for verdict, count in sorted(verdicts.items()):
        print(f"  {verdict:<7} {count}")
    for status in ("works_today", "gap"):
        subset = [r for r in rows if r[2] == status]
        if subset:
            passed = sum(1 for r in subset if r[0] == "PASS")
            print(f"  {status:<12} {passed}/{len(subset)} pass")
    if latencies:
        ordered = sorted(latencies)
        print(
            f"  latency  median={statistics.median(ordered):.2f}s"
            f"  p95={ordered[int(len(ordered) * 0.95) - 1]:.2f}s"
            f"  max={ordered[-1]:.2f}s"
        )
    # A regression in something that already worked is the blocking signal; a
    # still-unfilled gap is expected until the knowledge base catches up.
    regressions = [r for r in rows if r[2] == "works_today" and r[0] in {"FAIL", "ERROR"}]
    print(f"\n  regressions in previously working topics: {len(regressions)}")
    for _verdict, case_id, _status, note, _elapsed in regressions:
        print(f"    {case_id}: {note}")
    return 1 if regressions else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--set", type=Path, default=REPO_ROOT / "evaluation/smoke_non_circular.jsonl"
    )
    parser.add_argument(
        "--release",
        type=Path,
        default=REPO_ROOT / "releases/versions/20260807-draft/knowledge.sqlite",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="verify the set is still non-circular and exit, without calling the gateway",
    )
    args = parser.parse_args()

    items = load_set(args.set)
    database = KnowledgeDatabase(args.release, 1024)
    try:
        circular = check_non_circular(items, database)
        if circular:
            print("CIRCULAR - these questions now match indexed text:", file=sys.stderr)
            for line in circular:
                print(f"  {line}", file=sys.stderr)
            print("Rephrase them, or the set stops measuring retrieval.", file=sys.stderr)
            return 2
        counts: dict[str, int] = {}
        for item in items:
            counts[item.get("status", "?")] = counts.get(item.get("status", "?"), 0) + 1
        print(f"{len(items)} questions, none verbatim in the index: {counts}")
        if args.check_only:
            return 0

        from luna_kb.clients import ModelEndpoints, RemoteModels

        base = os.environ["LUNA_MODEL_BASE_URL"].rstrip("/")
        models = RemoteModels(
            ModelEndpoints(
                base_url=base,
                api_key=os.environ["LUNA_MODEL_API_KEY"],
                planner_model=os.getenv("LUNA_PLANNER_MODEL", "Qwen3.5-9B"),
                embedding_model=os.getenv("LUNA_EMBEDDING_MODEL", "bge-m3"),
                reranker_model=os.getenv("LUNA_RERANKER_MODEL", "Qwen3-Reranker-8B"),
                answer_model=os.getenv("LUNA_ANSWER_MODEL", "Qwen3.5-35B-A3B"),
                reranker_url=os.getenv("LUNA_RERANKER_URL", f"{base}/rerank"),
                timeout=float(os.getenv("LUNA_REQUEST_TIMEOUT", "8")),
            )
        )
        try:
            return asyncio.run(run(items, database, models))
        finally:
            asyncio.run(models.close())
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())

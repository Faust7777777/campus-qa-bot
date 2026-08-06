#!/usr/bin/env python
"""Replay the frozen evaluation set against a release, offline.

Zero gateway calls, seconds to run.  This is the fast feedback loop for
retrieval/selection changes, so that algorithm work never has to iterate
through the build -> evaluate -> activate chain.

What it really runs
-------------------
The production ``StrongRetriever.retrieve()``, including the real candidate
allocator and the real selector.  Only two things are substituted:

* **The vector channel is disabled.**  Query embeddings need the gateway, so
  recall is measured on the three lexical channels alone.  Every number here is
  therefore a *lower bound* on the deployed pipeline.
* **The reranker is replaced by the pathology we measured.**  On the DLUT
  gateway ``Qwen3-Reranker-8B`` returned 0.911-0.929 across obviously unrelated
  documents, with the longest document scoring highest.  The stub reproduces
  that: a narrow band, ordered by document length.  Navigation cards carry no
  ``evidence_quote`` and so are always the shortest documents, which makes this
  the worst realistic case for them.

So a green run means: *even with a topic-blind, length-biased reranker, the
selector still lands on the expected card.*  It does not prove the deployed
reranker behaves well; it proves the pipeline no longer depends on that.

What it cannot measure
----------------------
Restraint on negative cases (no_answer / out_of_scope / faculty_boundary) needs
the answer model, and answer quality needs the gateway.  Those stay in the full
300-question evaluation.  This script only reports retrieval and selection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from luna_kb.errors import InsufficientEvidence, RetrievalUnavailable  # noqa: E402
from luna_kb.retrieval import KnowledgeDatabase, StrongRetriever  # noqa: E402

# A green fixture must fail if any of these regress.
MIN_POOL_RECALL = 0.97
MIN_ALLOCATOR_SURVIVAL = 0.97
MIN_SELECTION_ACCURACY = 0.90

RERANK_BAND_LOW = 0.911
RERANK_BAND_HIGH = 0.929
POSITIVE_KINDS = ("answerable", "historical")


class LexicalOnlyDatabase(KnowledgeDatabase):
    """The release database with its vector channel switched off."""

    def vector(self, vectors: list[list[float]], plan: Any, limit: int = 50) -> list[str]:
        return []


class OfflineModels:
    """Planner/embedder/reranker stand-ins that never touch the network."""

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension
        self._plan: dict[str, Any] = {}

    def set_plan(self, question: str, time_scope: str) -> None:
        # The plan is pinned rather than taken from the fast path so that this
        # fixture isolates retrieval and selection.  Fast-path behaviour has its
        # own tests in tests/test_fast_path.py.
        self._plan = {
            "intent": "historical" if time_scope == "historical" else "procedure",
            "standalone_query": question,
            "subqueries": [question],
            "entities": [],
            "required_facets": [],
            "filters": {
                "campus": "",
                "audience": "本科生",
                "time_scope": time_scope,
            },
        }

    async def plan(self, question: str, history: Any = None) -> dict[str, Any]:
        return dict(self._plan)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vector = [1.0] + [0.0] * (self._dimension - 1)
        return [list(vector) for _ in texts]

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        # Longest document wins, all scores inside the observed narrow band.
        order = sorted(range(len(documents)), key=lambda i: (-len(documents[i]), i))
        span = RERANK_BAND_HIGH - RERANK_BAND_LOW
        step = span / max(len(documents) - 1, 1)
        return [
            (index, round(RERANK_BAND_HIGH - position * step, 6))
            for position, index in enumerate(order)
        ]


def _legacy_rank_fused(
    cards: list[Any], rerank_ranks: dict[str, int], pool_size: int
) -> list[Any]:
    """Pre-P1 ordering: every fact card ahead of every navigation card.

    Substituting this for ``StrongRetriever._rank_fused`` reproduces the old
    ``if fact_cards:`` precedence exactly, because the selector then always
    finds a fact card in front whenever one qualifies.
    """

    return sorted(
        cards,
        key=lambda card: (
            card.card_kind != "fact",
            rerank_ranks.get(card.card_id, len(rerank_ranks) + 1),
        ),
    )


def load_positives(path: Path) -> list[dict[str, Any]]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("kind") in POSITIVE_KINDS and item.get("expected_card_ids"):
            items.append(item)
    return items


async def replay(
    database: KnowledgeDatabase, items: list[dict[str, Any]], dimension: int
) -> dict[str, Any]:
    models = OfflineModels(dimension)
    retriever = StrongRetriever(database, models, fast_path_enabled=False)
    stats: dict[str, Any] = {
        "total": len(items),
        "pool_hit": 0,
        "allocator_hit": 0,
        "selected_hit": 0,
        "refused": 0,
        "by_kind": {"fact": [0, 0], "navigation": [0, 0]},
        "misses": [],
    }
    for item in items:
        question = str(item["question"])
        gold = set(str(value) for value in item["expected_card_ids"])
        time_scope = "historical" if item["kind"] == "historical" else "current"
        models.set_plan(question, time_scope)
        try:
            result = await retriever.retrieve(question)
        except InsufficientEvidence:
            stats["refused"] += 1
            stats["misses"].append((question, "", "refused: insufficient evidence"))
            continue
        except RetrievalUnavailable as exc:
            raise SystemExit(f"retrieval failed offline ({exc.component}): {exc}") from exc

        trace = result.trace
        in_pool = bool(gold & set(trace.first_stage_ids))
        stats["pool_hit"] += in_pool
        stats["allocator_hit"] += bool(gold & set(trace.fused_ids))
        selected = bool(gold & set(trace.selected_ids))
        stats["selected_hit"] += selected

        if in_pool:
            gold_id = next(iter(gold & set(trace.first_stage_ids)))
            cards = database.load_cards([gold_id])
            kind = cards[gold_id].card_kind if gold_id in cards else "fact"
            bucket = stats["by_kind"].setdefault(kind, [0, 0])
            bucket[0] += selected
            bucket[1] += 1
        if not selected:
            picked = database.load_cards(trace.selected_ids[:1])
            title = next(iter(picked.values())).title if picked else "?"
            stats["misses"].append((question, sorted(gold)[0], f"picked: {title}"))
    return stats


def report(label: str, stats: dict[str, Any]) -> float:
    total = stats["total"]
    pool = stats["pool_hit"]
    print(f"\n===== {label} =====")
    print(f"  positive cases                      {total}")
    print(f"  gold in first-stage pool            {pool}/{total} = {pool / total:.1%}")
    print(
        f"  gold survives the allocator         {stats['allocator_hit']}/{pool}"
        f" = {stats['allocator_hit'] / max(pool, 1):.1%}"
    )
    accuracy = stats["selected_hit"] / max(total, 1)
    print(f"  gold SELECTED                       {stats['selected_hit']}/{total} = {accuracy:.1%}")
    if stats["refused"]:
        print(f"  refused (insufficient evidence)     {stats['refused']}")
    for kind, (hit, seen) in sorted(stats["by_kind"].items()):
        if seen:
            print(f"    gold is a {kind:<11} card       {hit}/{seen} = {hit / seen:.1%}")
    return accuracy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release",
        type=Path,
        default=REPO_ROOT / "releases/versions/20260807-draft/knowledge.sqlite",
    )
    parser.add_argument(
        "--evaluation-set",
        type=Path,
        default=REPO_ROOT / "work/evaluation_20260807.jsonl",
    )
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument(
        "--compare-legacy",
        action="store_true",
        help="also replay with the pre-P1 fact-first precedence, for a before/after",
    )
    args = parser.parse_args()

    items = load_positives(args.evaluation_set)
    if not items:
        print("no positive evaluation cases found", file=sys.stderr)
        return 2

    database = LexicalOnlyDatabase(args.release, args.dimension)
    try:
        if args.compare_legacy:
            # Take the staticmethod object itself, not the plain function the
            # class attribute resolves to, so restoring it stays a staticmethod.
            original = StrongRetriever.__dict__["_rank_fused"]
            StrongRetriever._rank_fused = staticmethod(_legacy_rank_fused)
            try:
                report("BEFORE (fact-first precedence)", asyncio.run(replay(database, items, args.dimension)))
            finally:
                StrongRetriever._rank_fused = original

        stats = asyncio.run(replay(database, items, args.dimension))
        accuracy = report("CURRENT", stats)

        pool_recall = stats["pool_hit"] / stats["total"]
        survival = stats["allocator_hit"] / max(stats["pool_hit"], 1)
        checks = {
            "pool_recall": pool_recall >= MIN_POOL_RECALL,
            "allocator_survival": survival >= MIN_ALLOCATOR_SURVIVAL,
            "selection_accuracy": accuracy >= MIN_SELECTION_ACCURACY,
        }
        if stats["misses"]:
            print("\n  misses (first 10):")
            for question, gold_id, note in stats["misses"][:10]:
                print(f"    {question[:34]!r} gold={gold_id[:34]} {note[:46]}")

        print("\n===== GATE =====")
        for name, ok in checks.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        return 0 if all(checks.values()) else 1
    finally:
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""Run the colloquial-paraphrase set against the real gateway.

Why this exists
---------------
``evaluation/smoke_non_circular.jsonl`` judges answers by topic keyword, which
is the right call while the knowledge base is still being filled but cannot tell
"cited the card that answers this" from "cited a card about the same topic".
This set names the gold card instead, so it measures selection: every question
here has a live card that answers it, phrased the way it would arrive in a QQ
group rather than the way the card indexes itself.

The API key is read from the environment and is never written anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from luna_kb.clients import ModelEndpoints, RemoteModels  # noqa: E402
from luna_kb.errors import InsufficientEvidence, RetrievalUnavailable  # noqa: E402
from luna_kb.retrieval import KnowledgeDatabase, StrongRetriever  # noqa: E402
from luna_kb.service import AnswerService  # noqa: E402


def load_set(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def run(
    items: list[dict],
    database: KnowledgeDatabase,
    models,
    pace_seconds: float,
    vector_recall: bool,
) -> int:
    service = AnswerService(
        StrongRetriever(
            database, models, 0.35, fast_path_enabled=True, vector_recall_enabled=vector_recall
        ),
        models,
        cache_ttl_seconds=0,
        cache_size=0,
        answer_mode="draft",
        answer_max_chars=300,
        answer_max_sources=3,
    )
    cited = selected = declined = errored = 0
    latencies: list[float] = []
    try:
        for position, item in enumerate(items):
            if position and pace_seconds:
                await asyncio.sleep(pace_seconds)
            started = time.perf_counter()
            try:
                answer = await service.ask(item["question"])
                latencies.append(time.perf_counter() - started)
                in_evidence = item["gold"] in [card.card_id for card in answer.retrieval.cards]
                in_answer = item["gold"] in answer.cited_card_ids
                selected += in_evidence
                cited += in_answer
                if in_answer:
                    verdict, note = "CITED", ""
                elif in_evidence:
                    verdict, note = "SELECTED", "gold was evidence but the answer did not cite it"
                else:
                    picked = [card.title[:18] for card in answer.retrieval.cards]
                    verdict, note = "WRONG", f"answered from {picked}"
            except InsufficientEvidence as exc:
                latencies.append(time.perf_counter() - started)
                declined += 1
                verdict, note = "DECLINED", str(exc)[:52]
            except RetrievalUnavailable as exc:
                errored += 1
                verdict, note = "ERROR", f"{exc.component} failed"
            print(f"{verdict:<9} {item['id']:<24} {item['topic']:<12} {note[:56]}")
    finally:
        await service.close()

    total = len(items)
    print("\n" + "=" * 70)
    print(f"  cited the gold card   {cited}/{total} = {cited / total:.0%}")
    print(f"  gold reached evidence {selected}/{total} = {selected / total:.0%}")
    print(f"  declined              {declined}/{total}")
    print(f"  gateway errors        {errored}/{total}")
    if latencies:
        ordered = sorted(latencies)
        print(
            f"  latency  median={statistics.median(ordered):.2f}s"
            f"  p95={ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)]:.2f}s"
        )
    return 0 if errored == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", type=Path, default=REPO_ROOT / "evaluation/paraphrase_30.jsonl")
    parser.add_argument(
        "--release", type=Path, default=REPO_ROOT / "releases/versions/20260808-draft/knowledge.sqlite"
    )
    parser.add_argument("--pace", type=float, default=6.0)
    parser.add_argument("--no-vector", action="store_true")
    args = parser.parse_args()

    base_url = os.environ["LUNA_MODEL_BASE_URL"].rstrip("/")
    models = RemoteModels(
        ModelEndpoints(
            base_url,
            os.environ["LUNA_MODEL_API_KEY"],
            "Qwen3.5-9B",
            "bge-m3",
            "Qwen3-Reranker-8B",
            "Qwen3.5-35B-A3B",
            f"{base_url}/rerank",
            40.0,
        )
    )
    database = KnowledgeDatabase(args.release, 1024)
    try:
        return asyncio.run(
            run(load_set(args.set), database, models, args.pace, not args.no_vector)
        )
    finally:
        database.close()
        asyncio.run(models.close())


if __name__ == "__main__":
    raise SystemExit(main())

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


def acceptable(item: dict) -> set[str]:
    """Card ids that count as the right answer for this question.

    Some questions have more than one right card because the university
    published the same rules on more than one page: 国家助学贷款政策与申请条件
    and 国家助学贷款申请条件 are separate official notices with separate URLs,
    so merging them in the knowledge base would throw away provenance.  The
    measurement is what needs to treat them as one - naming a single gold made
    the score swing on which of two correct answers the selector happened to
    prefer.
    """

    return {item["gold"], *item.get("also_correct", [])}


def as_messages(history: list[list[str]] | None) -> list[dict[str, str]] | None:
    """Turn a set's [[question, answer], ...] pairs into conversation messages.

    Multi-turn cases are the only ones that can show what resolving "那要几个人
    才能订" against the previous turn is worth, so they are written as pairs and
    expanded here into the shape ConversationStore hands the retriever.
    """

    if not history:
        return None
    messages: list[dict[str, str]] = []
    for question, answer in history:
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})
    return messages


class NoPlanner:
    """Wraps the gateway so no plan call happens; the question is the plan.

    The planner rewrites the question and its rewrite replaces the user's own
    words in retrieval, so a wrong guess removes the answer from the pool
    before anything reads it.  Measured over these 30 questions it cost more
    than it earned, so this is the arm that skips it.
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def plan(self, question: str, history=None) -> dict:
        return {
            "intent": "fact",
            "standalone_query": question,
            "subqueries": [question],
            "entities": [],
            "required_facets": [],
            "filters": {"time_scope": "current"},
        }


def load_done(path: Path | None) -> dict[str, dict]:
    """Cases already recorded, so an interrupted run resumes instead of restarting."""

    if path is None or not path.exists():
        return {}
    done = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            done[row["id"]] = row
    return done


async def run(
    items: list[dict],
    database: KnowledgeDatabase,
    models,
    pace_seconds: float,
    vector_recall: bool,
    out_path: Path | None = None,
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
    # Every case is written the moment it finishes.  A run costs five to fifteen
    # minutes of gateway budget, and holding the results in memory until the end
    # has already lost two of them - once to a timeout that killed the process,
    # once to a second arm opening the same file with "w".
    done = load_done(out_path)
    if done:
        print(f"resuming: {len(done)} cases already recorded in {out_path}")
    sink = out_path.open("a", encoding="utf-8") if out_path else None
    cited = selected = declined = errored = 0
    latencies: list[float] = []
    try:
        for position, item in enumerate(items):
            if item["id"] in done:
                record = done[item["id"]]
                cited += record["cited"]
                selected += record["selected"]
                declined += record["verdict"] == "DECLINED"
                errored += record["verdict"] == "ERROR"
                if record.get("seconds"):
                    latencies.append(record["seconds"])
                continue
            if position and pace_seconds:
                await asyncio.sleep(pace_seconds)
            started = time.perf_counter()
            try:
                answer = await service.ask(item["question"], as_messages(item.get("history")))
                latencies.append(time.perf_counter() - started)
                good = acceptable(item)
                in_evidence = bool(good & {card.card_id for card in answer.retrieval.cards})
                in_answer = bool(good & set(answer.cited_card_ids))
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
            if sink:
                sink.write(json.dumps({
                    "id": item["id"], "topic": item["topic"], "gold": item["gold"],
                    "verdict": verdict, "note": note,
                    "cited": int(verdict == "CITED"),
                    "selected": int(verdict in ("CITED", "SELECTED")),
                    "seconds": round(latencies[-1], 3) if latencies else None,
                }, ensure_ascii=False) + "\n")
                sink.flush()
    finally:
        if sink:
            sink.close()
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
    parser.add_argument(
        "--no-planner",
        action="store_true",
        help="retrieve on the question as typed, skipping the planner entirely",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="append one JSONL line per case here; rerunning resumes from it",
    )
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
    if args.no_planner:
        models = NoPlanner(models)
    database = KnowledgeDatabase(args.release, 1024)
    try:
        return asyncio.run(
            run(
                load_set(args.set), database, models, args.pace,
                not args.no_vector, args.out,
            )
        )
    finally:
        database.close()
        asyncio.run(models.close())


if __name__ == "__main__":
    raise SystemExit(main())

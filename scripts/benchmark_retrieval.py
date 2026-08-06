from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

from luna_kb.contracts import (
    CandidateCard,
    LunaSourceResult,
    ReviewStatus,
    ReviewedCard,
    RiskLevel,
    SourceAuthority,
    Validity,
    content_digest,
)
from luna_kb.candidate_allocation import RerankCandidateAllocator
from luna_kb.pipeline.build import build_database
from luna_kb.retrieval import KnowledgeDatabase, QueryFilters, QueryPlan, StrongRetriever

ANCHORS = (
    "奖学金怎么申请",
    "校园卡挂失补办流程",
    "宿舍报修流程",
    "转专业",
    "考试安排",
    "图书馆开放时间",
    "大学生医保",
    "校园网连不上怎么办",
)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))
    return ordered[index]


def synthetic_cards(count: int, dimension: int) -> list[ReviewedCard]:
    vectors: list[list[float]] = []
    for index in range(64):
        vector = [0.0] * dimension
        vector[index % dimension] = 1.0
        vector[(index * 17 + 7) % dimension] = 0.25
        vectors.append(vector)
    reviewed: list[ReviewedCard] = []
    for index in range(count):
        anchor = ANCHORS[index % len(ANCHORS)]
        clean_text = f"{anchor}的办理要求以学校当年官方通知为准，编号{index}。"
        card = CandidateCard(
            card_id=f"card-benchmark-{index:05d}",
            title=f"{anchor}事项{index}",
            standard_question=anchor,
            summary=f"{anchor}应以当年通知为准。",
            evidence_quote=clean_text,
            source_locator="正文第1段",
            generated_questions=[anchor, f"{anchor}怎么办"],
            aliases=[anchor.replace("流程", "")],
            risk_level=RiskLevel.LOW,
            extraction_confidence=0.95,
            retrieval_text=f"{anchor} 本科生 校园服务 办理要求",
            keywords=["本科生", "校园服务"],
            facts={"要求": "以当年通知为准"},
            facets=["办理要求"],
            campus="凌水" if index % 2 == 0 else "盘锦",
            audience="本科生",
            validity=Validity.CURRENT,
            subject_key=f"subject-{index}",
            fact_key=f"fact-{index}",
            source_authority=SourceAuthority.FORMAL_POLICY,
            embedding=vectors[index % len(vectors)],
        )
        source = LunaSourceResult(
            source_id=f"kb_clean:benchmark-{index:05d}",
            dataset="kb_clean",
            canonical_url=f"https://example.dlut.edu.cn/benchmark/{index}",
            title=f"{anchor}官方通知{index}",
            official_domain="example.dlut.edu.cn",
            published_at="2026-01-01",
            fetched_at="2026-08-06T00:00:00+08:00",
            content_hash=content_digest(clean_text),
            clean_text=clean_text,
            fetch_status="success",
            candidate_cards=[card],
            unresolved_questions=[],
        )
        reviewed.append(
            ReviewedCard(
                source=source,
                card=card,
                review_status=ReviewStatus.APPROVED,
                review_reason="benchmark",
                reviewer="benchmark",
                reviewed_at="2026-08-06T00:00:00+08:00",
            )
        )
    return reviewed


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "mean_ms": round(statistics.fmean(values) * 1000, 3),
        "p50_ms": round(percentile(values, 0.50) * 1000, 3),
        "p95_ms": round(percentile(values, 0.95) * 1000, 3),
        "max_ms": round(max(values) * 1000, 3),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("work/benchmark"))
    parser.add_argument("--cards", type=int, default=3652)
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--iterations", type=int, default=80)
    parser.add_argument("--stress-pairs", type=int, default=0)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    database_path = args.output / "knowledge.sqlite"
    build_seconds = None
    if not database_path.exists():
        started = time.perf_counter()
        await build_database(
            synthetic_cards(args.cards, args.dimension),
            database_path,
            expected_dimension=args.dimension,
        )
        build_seconds = time.perf_counter() - started
    database = KnowledgeDatabase(database_path, expected_dimension=args.dimension)
    timings: dict[str, list[float]] = {
        "exact": [],
        "bm25": [],
        "trigram": [],
        "vector": [],
        "vector_filtered": [],
        "local_pipeline": [],
        "local_pipeline_parallel": [],
        "candidate_load_and_allocate": [],
        "local_retrieval_frontend": [],
    }
    try:
        unfiltered = QueryPlan(
            "procedure", ANCHORS[0], [ANCHORS[0]], [], [], QueryFilters()
        )
        filtered = QueryPlan(
            "procedure",
            ANCHORS[0],
            [ANCHORS[0]],
            [],
            [],
            QueryFilters(campus="凌水", audience="本科生"),
        )
        for iteration in range(args.iterations + 5):
            query = ANCHORS[iteration % len(ANCHORS)]
            vector = [0.0] * args.dimension
            vector[iteration % 64 % args.dimension] = 1.0
            samples: dict[str, float] = {}
            started = time.perf_counter()
            database.exact([query], unfiltered, 10)
            samples["exact"] = time.perf_counter() - started
            started = time.perf_counter()
            database.bm25([query], unfiltered, 40)
            samples["bm25"] = time.perf_counter() - started
            started = time.perf_counter()
            database.trigram([query], unfiltered, 30)
            samples["trigram"] = time.perf_counter() - started
            started = time.perf_counter()
            database.vector([vector], unfiltered, 50)
            samples["vector"] = time.perf_counter() - started
            started = time.perf_counter()
            database.vector([vector], filtered, 50)
            samples["vector_filtered"] = time.perf_counter() - started
            samples["local_pipeline"] = sum(
                samples[name] for name in ("exact", "bm25", "trigram", "vector")
            )
            frontend_started = time.perf_counter()
            started = frontend_started
            channels = await database.recall_channels([query], [vector], unfiltered)
            samples["local_pipeline_parallel"] = time.perf_counter() - started
            started = time.perf_counter()
            first_stage = StrongRetriever._rrf(channels, 50)
            cards = database.load_cards(first_stage)
            RerankCandidateAllocator().allocate(first_stage, cards)
            samples["candidate_load_and_allocate"] = time.perf_counter() - started
            samples["local_retrieval_frontend"] = time.perf_counter() - frontend_started
            if iteration >= 5:
                for name, value in samples.items():
                    timings[name].append(value)
        stress_report = None
        if args.stress_pairs:
            expected_results: dict[tuple[str, int], dict[str, list[str]]] = {}
            stress_timings: list[float] = []
            stress_consistent = True
            for pair_index in range(args.stress_pairs):
                query = ANCHORS[pair_index % len(ANCHORS)]
                vector_index = pair_index % 64
                vector = [0.0] * args.dimension
                vector[vector_index % args.dimension] = 1.0
                started = time.perf_counter()
                pair = await asyncio.gather(
                    database.recall_channels([query], [vector], unfiltered),
                    database.recall_channels([query], [vector], unfiltered),
                )
                stress_timings.append(time.perf_counter() - started)
                key = (query, vector_index)
                expected = expected_results.setdefault(key, pair[0])
                stress_consistent &= pair[0] == pair[1] == expected
            stress_report = {
                "pairs": args.stress_pairs,
                "requests": args.stress_pairs * 2,
                "consistent": stress_consistent,
                "pair_timings": summarize(stress_timings),
            }

        report = {
            "card_count": args.cards,
            "embedding_dimension": args.dimension,
            "database_bytes": database_path.stat().st_size,
            "build_seconds": round(build_seconds, 3) if build_seconds is not None else None,
            "iterations": args.iterations,
            "timings": {name: summarize(values) for name, values in timings.items()},
            "health": database.healthcheck(),
            "concurrency_stress": stress_report,
        }
    finally:
        database.close()
    report_path = args.output / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())

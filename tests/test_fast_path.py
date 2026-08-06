from __future__ import annotations

from pathlib import Path

import pytest

from luna_kb.pipeline.build import build_database
from luna_kb.query_fastpath import fast_query_plan
from luna_kb.retrieval import KnowledgeDatabase, StrongRetriever


def test_fast_path_only_accepts_simple_single_part_questions() -> None:
    plan = fast_query_plan("宿舍报修怎么申请")
    assert plan is not None
    assert plan["intent"] == "procedure"
    assert "报修" in plan["required_facets"]
    assert fast_query_plan("我上次说的那个材料还要哪些", history=[]) is None
    assert fast_query_plan("奖学金怎么申请，截止时间是什么") is None


class NoPlannerModels:
    async def plan(self, question: str, history=None):
        raise AssertionError("simple fast-path question should not call planner")

    async def embed(self, texts: list[str]):
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def rerank(self, query: str, documents: list[str]):
        return [(index, 0.95) for index in range(len(documents))]


@pytest.mark.asyncio
async def test_retriever_can_skip_planner_for_a_simple_question(tmp_path: Path, make_approved_card) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        result = await StrongRetriever(
            database, NoPlannerModels(), fast_path_enabled=True
        ).retrieve("奖学金怎么申请")
        assert result.plan.intent == "procedure"
    finally:
        database.close()

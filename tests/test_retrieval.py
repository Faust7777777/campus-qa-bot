import asyncio
import copy
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from luna_kb.candidate_allocation import RerankCandidateAllocator
from luna_kb.contracts import ReviewStatus, ReviewedCard, content_digest
from luna_kb.errors import InsufficientEvidence, RetrievalUnavailable
from luna_kb.pipeline.build import build_database
from luna_kb.retrieval import (
    MIN_TOPIC_OVERLAP,
    CardEvidence,
    KnowledgeDatabase,
    QueryFilters,
    QueryPlan,
    StrongRetriever,
    missing_required_facets,
    serves_another_audience,
    topic_overlap,
)
from luna_kb.vector import load_sqlite_vec


def _evidence(card_id: str, subject_key: str, fact_key: str) -> CardEvidence:
    return CardEvidence(
        card_id=card_id,
        source_id=f"kb_clean:{card_id}",
        parent_card_id=None,
        title=card_id,
        summary="",
        evidence_quote="本科生事项以官方通知为准。",
        source_locator="正文",
        facts={},
        facets=[],
        campus="",
        audience="本科生",
        validity="current",
        card_kind="fact",
        subject_key=subject_key,
        fact_key=fact_key,
        retrieval_text=card_id,
        canonical_url=f"https://example.dlut.edu.cn/{card_id}",
        source_title=card_id,
        published_at=None,
    )


def test_allocator_scopes_fact_keys_to_their_subject_and_source() -> None:
    cards = {
        "scholarship": _evidence("scholarship", "scholarship", "deadline"),
        "transfer": _evidence("transfer", "major-transfer", "deadline"),
        "scholarship-copy": _evidence("scholarship-copy", "scholarship", "deadline"),
    }
    cards["scholarship-copy"].source_id = cards["scholarship"].source_id

    assert RerankCandidateAllocator().allocate(list(cards), cards) == [
        "scholarship",
        "transfer",
    ]


def test_allocator_scans_past_a_duplicate_heavy_prefix() -> None:
    cards = {
        f"deadline-{index:02d}": _evidence(
            f"deadline-{index:02d}", "scholarship", "deadline"
        )
        for index in range(24)
    }
    cards["materials"] = _evidence("materials", "scholarship", "materials")
    ordered = list(cards)

    allocated = RerankCandidateAllocator().allocate(ordered, cards)

    assert "materials" in allocated
    assert allocated[:2] == ["deadline-00", "materials"]
    assert len([card_id for card_id in allocated if card_id.startswith("deadline-")]) == 2


def test_allocator_keeps_one_cross_source_alternate_for_reranking() -> None:
    cards = {
        "weak": _evidence("weak", "scholarship", "deadline"),
        "strong": _evidence("strong", "scholarship", "deadline"),
        "third": _evidence("third", "scholarship", "deadline"),
    }

    assert RerankCandidateAllocator().allocate(list(cards), cards) == ["weak", "strong"]


def test_allocator_keeps_an_alternate_when_unique_facts_saturate_the_budget() -> None:
    cards = {
        "weak-deadline": _evidence("weak-deadline", "scholarship", "deadline"),
        **{
            f"unique-{index:02d}": _evidence(
                f"unique-{index:02d}", "scholarship", f"facet-{index:02d}"
            )
            for index in range(15)
        },
        "strong-deadline": _evidence("strong-deadline", "scholarship", "deadline"),
    }

    allocated = RerankCandidateAllocator().allocate(list(cards), cards)

    assert len(allocated) == 16
    assert allocated[0] == "weak-deadline"
    assert "strong-deadline" in allocated
    assert len([card_id for card_id in allocated if card_id.startswith("unique-")]) == 14


def test_allocator_keeps_same_title_cards_with_different_fact_keys() -> None:
    cards = {
        "deadline": _evidence("deadline", "scholarship", "deadline"),
        "materials": _evidence("materials", "scholarship", "materials"),
    }
    for card in cards.values():
        card.source_id = "kb_clean:one-source"
        card.title = "奖学金申请"

    assert RerankCandidateAllocator().allocate(list(cards), cards) == list(cards)


def test_allocator_reserves_several_navigation_slots_without_growing_budget() -> None:
    # One reserved slot could not represent a knowledge base that is mostly
    # navigation cards: the reranker was handed a single arbitrary entry point
    # and no way to choose between entry points.  Repeats from one source are
    # dropped so that a source cannot spend the whole quota on itself.
    cards = {
        f"fact-{index:02d}": _evidence(
            f"fact-{index:02d}", "scholarship", f"facet-{index:02d}"
        )
        for index in range(20)
    }
    for card_id, source_id in (
        ("nav-a1", "kb_clean:one"),
        ("nav-a2", "kb_clean:one"),
        ("nav-b", "kb_clean:two"),
        ("nav-c", "kb_clean:three"),
        ("nav-d", "kb_clean:four"),
    ):
        navigation = _evidence(card_id, "", "")
        navigation.card_kind = "navigation"
        navigation.evidence_quote = ""
        navigation.source_id = source_id
        cards[card_id] = navigation

    allocated = RerankCandidateAllocator().allocate(list(cards), cards)

    assert len(allocated) == 16
    assert [card_id for card_id in allocated if card_id.startswith("nav-")] == [
        "nav-a1",
        "nav-b",
        "nav-c",
    ]
    assert len([card_id for card_id in allocated if card_id.startswith("fact-")]) == 13


def test_required_facets_match_across_chinese_and_english_labels() -> None:
    card = _evidence("hukou", "hukou", "materials-and-location")
    card.facets = ["materials", "location", "contact"]
    card.title = "新生落户材料和办理地点"
    card.summary = "包含全部办理信息。"
    card.evidence_quote = "申请材料包括录取通知书，办理地点为服务大厅，咨询电话以官网公布为准。"

    assert missing_required_facets(["申请材料", "办理地点", "咨询电话"], [card]) == []
    assert missing_required_facets(["办理期限"], [card]) == ["办理期限"]


def test_generated_metadata_cannot_fake_required_evidence_facets() -> None:
    card = _evidence("hukou", "hukou", "materials-and-location")
    card.title = "落户材料、地点和电话"
    card.summary = "包含材料、办理地点和咨询电话。"
    card.facets = ["materials", "location", "contact"]

    assert missing_required_facets(["申请材料", "办理地点", "咨询电话"], [card]) == [
        "申请材料",
        "办理地点",
        "咨询电话",
    ]


def test_reranker_document_excludes_generated_retrieval_claims() -> None:
    card = _evidence("scholarship", "scholarship", "deadline")
    card.summary = "模型生成的错误截止日期是明天。"
    card.retrieval_text = "模型扩写：明天截止，必须线下办理。"

    document = StrongRetriever._rerank_document(card)

    assert card.evidence_quote in document
    assert card.source_title in document
    assert "错误截止日期" not in document
    assert "必须线下办理" not in document


def test_query_plan_enforces_undergraduate_audience_and_normalizes_campus() -> None:
    raw = {
        "intent": "procedure",
        "standalone_query": "凌水校区校园卡怎么充值",
        "subqueries": ["校园卡充值"],
        "entities": ["校园卡"],
        "required_facets": [],
        "filters": {
            "campus": "凌水校区",
            "audience": "研究生",
            "time_scope": "current",
        },
    }

    plan = QueryPlan.from_dict(raw, "凌水校区校园卡怎么充值")

    assert plan.filters.campus == "凌水"
    assert plan.filters.audience == "本科生"


def test_query_plan_rejects_model_generated_resource_amplification() -> None:
    raw = {
        "intent": "procedure",
        "standalone_query": "奖学金怎么申请",
        "subqueries": ["奖学金怎么申请"],
        "entities": [f"实体{index}" for index in range(21)],
        "required_facets": [],
        "filters": {"time_scope": "current"},
    }

    with pytest.raises(RetrievalUnavailable, match="entities"):
        QueryPlan.from_dict(raw, "奖学金怎么申请")


class LowConfidenceModels:
    async def plan(self, question: str, history=None) -> dict:
        return {
            "intent": "procedure",
            "standalone_query": question,
            "subqueries": [question],
            "entities": ["奖学金"],
            "required_facets": [],
            "filters": {"time_scope": "current"},
        }

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        return [(index, 0.05) for index in range(len(documents))]


class EmbeddingFailureModels(LowConfidenceModels):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RetrievalUnavailable("embedding", "gateway unavailable")


class InvalidEmbeddingModels(LowConfidenceModels):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float("nan"), 0.0, 0.0] for _ in texts]


class RerankerFailureModels(LowConfidenceModels):
    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        raise RetrievalUnavailable("reranker", "gateway unavailable")


class MissingFacetModels(LowConfidenceModels):
    async def plan(self, question: str, history=None) -> dict:
        plan = await super().plan(question, history)
        plan["required_facets"] = ["联系"]
        return plan

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        return [(index, 0.95) for index in range(len(documents))]


class LingShuiCurrentModels(LowConfidenceModels):
    async def plan(self, question: str, history=None) -> dict:
        plan = await super().plan(question, history)
        plan["filters"]["campus"] = "凌水"
        return plan

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        return [(index, 0.95) for index in range(len(documents))]


class HighConfidenceCurrentModels(LowConfidenceModels):
    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        return [(index, 0.95) for index in range(len(documents))]


@pytest.mark.asyncio
async def test_low_reranker_confidence_is_insufficient_evidence(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        retriever = StrongRetriever(database, LowConfidenceModels())

        with pytest.raises(InsufficientEvidence):
            await retriever.retrieve("奖学金怎么申请")
    finally:
        database.close()


@pytest.mark.asyncio
async def test_reranker_failure_does_not_return_first_stage_results(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        retriever = StrongRetriever(database, RerankerFailureModels())

        with pytest.raises(RetrievalUnavailable) as error:
            await retriever.retrieve("奖学金怎么申请")

        assert error.value.component == "reranker"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_embedding_failure_closes_retrieval_instead_of_using_lexical_results(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        retriever = StrongRetriever(database, EmbeddingFailureModels())

        with pytest.raises(RetrievalUnavailable) as error:
            await retriever.retrieve("奖学金怎么申请")

        assert error.value.component == "embedding"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_invalid_embedding_values_close_retrieval(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        retriever = StrongRetriever(database, InvalidEmbeddingModels())

        with pytest.raises(RetrievalUnavailable) as error:
            await retriever.retrieve("奖学金怎么申请")

        assert error.value.component == "embedding"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_missing_fact_facets_can_only_fall_back_to_a_clean_navigation_card(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    fact = make_approved_card(
        card_id="card-fact",
        source_id="kb_clean:fact",
        fact_key="application",
    )
    navigation = make_approved_card(
        card_id="card-navigation",
        card_title="奖学金联系官方页面",
        source_id="kb_clean:navigation",
        canonical_url="https://example.dlut.edu.cn/navigation",
        source_title="奖学金联系官方页面",
        fact_key="navigation",
    )
    navigation.review_status = ReviewStatus.DOWNGRADED
    navigation.card.card_kind = "navigation"
    navigation.card.summary = ""
    navigation.card.evidence_quote = ""
    navigation.card.source_locator = ""
    navigation.card.facts = {}
    navigation.card.facets = []
    navigation.card.fact_key = ""
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([fact, navigation], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        result = await StrongRetriever(database, MissingFacetModels()).retrieve(
            "奖学金怎么联系"
        )

        assert [card.card_id for card in result.cards] == ["card-navigation"]
        assert result.cards[0].evidence_quote == ""
    finally:
        database.close()


@pytest.mark.asyncio
async def test_runtime_rechecks_parent_scope_for_legacy_or_tampered_databases(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    parent = make_approved_card(
        card_id="card-parent",
        fact_key="overview",
        campus="凌水",
    )
    child = copy.deepcopy(parent)
    child.card.card_id = "card-child"
    child.card.fact_key = "materials"
    child.card.parent_card_id = parent.card.card_id
    child.source.candidate_cards = [parent.card, child.card]
    parent.source.candidate_cards = child.source.candidate_cards
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([parent, child], database_path, expected_dimension=3)

    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        result = await StrongRetriever(database, LingShuiCurrentModels()).retrieve(
            "凌水校区奖学金材料"
        )
        selected_child = next(card for card in result.cards if card.card_id == "card-child")
        assert selected_child.parent_context is not None
        assert selected_child.parent_context.card_id == "card-parent"
    finally:
        database.close()

    with sqlite3.connect(database_path) as connection:
        load_sqlite_vec(connection, build=True)
        connection.execute(
            "UPDATE cards SET campus='盘锦' WHERE card_id=?",
            (parent.card.card_id,),
        )
        connection.execute(
            "UPDATE vec_cards SET campus='盘锦' WHERE card_id=?",
            (parent.card.card_id,),
        )

    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        with pytest.raises(RetrievalUnavailable, match="parent scope mismatch") as error:
            await StrongRetriever(database, HighConfidenceCurrentModels()).retrieve(
                "奖学金材料"
            )
        assert error.value.component == "sqlite"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_vector_filter_is_applied_before_top_k_selection(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    cards = [
        make_approved_card(
            card_id=f"card-disallowed-{index:02d}",
            card_title=f"盘锦校区事项{index}",
            source_id=f"kb_clean:disallowed-{index:02d}",
            canonical_url=f"https://example.dlut.edu.cn/disallowed/{index}",
            source_title=f"盘锦校区事项{index}",
            fact_key=f"disallowed-{index}",
            campus="盘锦",
            embedding=[1.0, 0.0, 0.0],
        )
        for index in range(50)
    ]
    cards.append(
        make_approved_card(
            card_id="card-allowed",
            card_title="凌水校区奖学金事项",
            source_id="kb_clean:allowed",
            canonical_url="https://example.dlut.edu.cn/allowed",
            source_title="凌水校区奖学金事项",
            fact_key="allowed",
            campus="凌水",
            embedding=[0.0, 1.0, 0.0],
        )
    )
    await build_database(cards, database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    plan = QueryPlan(
        intent="fact",
        standalone_query="奖学金事项",
        subqueries=["奖学金事项"],
        entities=[],
        required_facets=[],
        filters=QueryFilters(campus="凌水", audience="本科生", time_scope="current"),
    )
    try:
        assert database.vector([[1.0, 0.0, 0.0]], plan, limit=10) == ["card-allowed"]
    finally:
        database.close()


@pytest.mark.asyncio
async def test_parallel_recall_matches_all_direct_channels(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    plan = QueryPlan(
        intent="procedure",
        standalone_query="奖学金怎么申请",
        subqueries=["奖学金怎么申请"],
        entities=[],
        required_facets=[],
        filters=QueryFilters(audience="本科生", time_scope="current"),
    )
    queries = ["奖学金怎么申请"]
    vectors = [[1.0, 0.0, 0.0]]
    try:
        expected = {
            "exact": database.exact(queries, plan, 10),
            "bm25": database.bm25(queries, plan, 40),
            "trigram": database.trigram(queries, plan, 30),
            "vector": database.vector(vectors, plan, 50),
        }

        concurrent_results = await asyncio.gather(
            *(database.recall_channels(queries, vectors, plan) for _ in range(8))
        )

        assert all(result == expected for result in concurrent_results)
    finally:
        database.close()


@pytest.mark.asyncio
async def test_campus_specific_query_keeps_campus_wide_cards(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database(
        [make_approved_card(card_id="card-campus-wide", campus="全校")],
        database_path,
        expected_dimension=3,
    )
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    plan = QueryPlan(
        intent="fact",
        standalone_query="奖学金怎么申请",
        subqueries=["奖学金怎么申请"],
        entities=[],
        required_facets=[],
        filters=QueryFilters(campus="凌水", audience="本科生", time_scope="current"),
    )
    try:
        assert database.exact(["奖学金怎么申请"], plan) == ["card-campus-wide"]
        assert database.vector([[1.0, 0.0, 0.0]], plan) == ["card-campus-wide"]
    finally:
        database.close()


@pytest.mark.asyncio
async def test_multi_campus_card_is_recalled_only_for_included_campuses(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database(
        [make_approved_card(card_id="card-two-campuses", campus="凌水|开发区")],
        database_path,
        expected_dimension=3,
    )
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        for campus in ("凌水", "开发区"):
            plan = QueryPlan(
                intent="fact",
                standalone_query="奖学金怎么申请",
                subqueries=["奖学金怎么申请"],
                entities=[],
                required_facets=[],
                filters=QueryFilters(campus=campus, audience="本科生", time_scope="current"),
            )
            assert database.exact(["奖学金怎么申请"], plan) == ["card-two-campuses"]
            assert database.vector([[1.0, 0.0, 0.0]], plan) == ["card-two-campuses"]

        panjin_plan = QueryPlan(
            intent="fact",
            standalone_query="奖学金怎么申请",
            subqueries=["奖学金怎么申请"],
            entities=[],
            required_facets=[],
            filters=QueryFilters(campus="盘锦", audience="本科生", time_scope="current"),
        )
        assert database.exact(["奖学金怎么申请"], panjin_plan) == []
        assert database.vector([[1.0, 0.0, 0.0]], panjin_plan) == []
    finally:
        database.close()


def _polluting_fact(make_approved_card: Callable[..., ReviewedCard]) -> ReviewedCard:
    """A long, off-topic fact card of the kind that dominates the live pool."""

    item = make_approved_card(
        card_id="card-library",
        card_title="图书馆入馆签到与研修间预约",
        source_id="kb_clean:library",
        canonical_url="https://example.dlut.edu.cn/library",
        source_title="图书馆入馆签到与研修间预约",
        fact_key="checkin",
        embedding=[0.0, 1.0, 0.0],
    )
    clean_text = "入馆（人脸识别、玉兰卡、虚拟卡）自动签到；上述系统还可以进行研修间的预约。"
    item.card.standard_question = "图书馆怎么签到"
    item.card.generated_questions = ["研修间怎么预约"]
    item.card.aliases = ["入馆签到"]
    item.card.keywords = ["图书馆", "签到"]
    item.card.evidence_quote = clean_text
    item.card.subject_key = "library-space"
    item.source.clean_text = clean_text
    item.source.content_hash = content_digest(clean_text)
    return item


def _navigation(
    make_approved_card: Callable[..., ReviewedCard],
    *,
    card_title: str = "本科生专项奖学金申请办理",
    standard_question: str = "奖学金怎么申请",
    embedding: list[float] | None = None,
) -> ReviewedCard:
    item = make_approved_card(
        card_id="card-navigation",
        card_title=card_title,
        source_id="kb_clean:navigation",
        canonical_url="https://example.dlut.edu.cn/navigation",
        source_title=card_title,
        fact_key="navigation",
        embedding=embedding,
    )
    item.card.standard_question = standard_question
    item.review_status = ReviewStatus.DOWNGRADED
    item.card.card_kind = "navigation"
    item.card.summary = ""
    item.card.evidence_quote = ""
    item.card.source_locator = ""
    item.card.facts = {}
    item.card.facets = []
    item.card.fact_key = ""
    return item


class TopicBlindRerankerModels(LowConfidenceModels):
    """Reproduces the observed gateway behaviour: a narrow, topic-blind band.

    The off-topic library card scores 0.929 and the on-topic navigation card
    scores 0.911, exactly as measured against the DLUT gateway.
    """

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        return [
            (index, 0.929 if "图书馆" in document else 0.911)
            for index, document in enumerate(documents)
        ]


@pytest.mark.asyncio
async def test_navigation_card_wins_when_the_reranker_prefers_an_off_topic_fact(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    # The regression this pins: card kind must not decide precedence.  The
    # reranker puts an unrelated fact card first, but the first stage puts the
    # navigation card first, so the fused rank must select navigation.
    database_path = tmp_path / "knowledge.sqlite"
    await build_database(
        [_polluting_fact(make_approved_card), _navigation(make_approved_card)],
        database_path,
        expected_dimension=3,
    )
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        result = await StrongRetriever(database, TopicBlindRerankerModels()).retrieve(
            "奖学金怎么申请"
        )

        # The reranker prefers the library card and the first stage prefers the
        # navigation card; assert the outcome first so a regression reports the
        # card it wrongly selected rather than a missing trace field.
        assert [card.card_id for card in result.cards] == ["card-navigation"]
        assert result.cards[0].evidence_quote == ""
        assert result.trace.reranked_ids[0] == "card-library"
        assert result.trace.first_stage_ids[0] == "card-navigation"
        assert result.trace.selection_ids[0] == "card-navigation"
    finally:
        database.close()


@pytest.mark.asyncio
async def test_on_topic_fact_card_still_answers_from_evidence(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    # Reverse guard: the fix must not flip the default the other way.  The fact
    # card is the better first-stage match here, so it still wins and evidence
    # still reaches the answer model.
    database_path = tmp_path / "knowledge.sqlite"
    await build_database(
        [
            make_approved_card(),
            _navigation(
                make_approved_card,
                card_title="奖学金评审结果公示",
                standard_question="奖学金评审结果在哪看",
                embedding=[0.0, 1.0, 0.0],
            ),
        ],
        database_path,
        expected_dimension=3,
    )
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        result = await StrongRetriever(
            database, HighConfidenceCurrentModels()
        ).retrieve("奖学金怎么申请")

        assert [card.card_id for card in result.cards] == ["card-scholarship"]
        assert result.cards[0].evidence_quote
    finally:
        database.close()


@pytest.mark.asyncio
async def test_navigation_only_pool_answers_instead_of_refusing(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    # No new refusal path: a pool with no qualifying fact card must return the
    # official entry point rather than raising InsufficientEvidence.
    database_path = tmp_path / "knowledge.sqlite"
    await build_database(
        [_navigation(make_approved_card)], database_path, expected_dimension=3
    )
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        result = await StrongRetriever(
            database, HighConfidenceCurrentModels()
        ).retrieve("奖学金怎么申请")

        assert [card.card_id for card in result.cards] == ["card-navigation"]
    finally:
        database.close()


def test_rank_fusion_reorders_candidates_without_dropping_any() -> None:
    # Ordering-only invariant: the selector may reorder candidates but must
    # never remove one, otherwise it becomes a hidden evidence gate.
    cards = []
    for index, (first_stage_rank, card_id) in enumerate(
        [(3, "c"), (1, "a"), (0, "unranked"), (2, "b")]
    ):
        card = _evidence(card_id, "subject", f"fact-{index}")
        card.first_stage_rank = first_stage_rank
        cards.append(card)
    tiers = {"a": 4, "b": 2, "c": 1}

    ordered = StrongRetriever._rank_fused(cards, tiers, pool_size=50)

    assert sorted(card.card_id for card in ordered) == sorted(
        card.card_id for card in cards
    )
    # Tier leads, so c (tier 1) then b (tier 2) then a (tier 4); "unranked" is
    # in no tier and has no first-stage rank, so it always sorts last.
    assert [card.card_id for card in ordered] == ["c", "b", "a", "unranked"]


def test_rank_fusion_lets_the_first_stage_outweigh_a_banded_reranker() -> None:
    # A banded reranker puts both cards in one tier, which means it expressed no
    # preference at all.  The first stage then decides, even though the reranker
    # scored the off-topic card marginally higher.
    off_topic = _evidence("off-topic", "subject", "fact-1")
    off_topic.first_stage_rank = 30
    off_topic.rerank_score = 0.929
    on_topic = _evidence("on-topic", "subject", "fact-2")
    on_topic.first_stage_rank = 1
    on_topic.rerank_score = 0.911

    cards = [off_topic, on_topic]
    tiers = StrongRetriever._rerank_tiers(cards)

    assert tiers == {"off-topic": 1, "on-topic": 1}
    ordered = StrongRetriever._rank_fused(cards, tiers, pool_size=50)
    assert [card.card_id for card in ordered] == ["on-topic", "off-topic"]


def test_rerank_tiers_collapse_a_degenerate_band() -> None:
    # The observed gateway band: obviously unrelated documents land within
    # 0.018 of each other.  That is not a preference, so it must not outvote
    # the first stage.
    cards = []
    for card_id, score in (("a", 0.929), ("b", 0.925), ("c", 0.911)):
        card = _evidence(card_id, "subject", card_id)
        card.rerank_score = score
        cards.append(card)

    assert StrongRetriever._rerank_tiers(cards) == {"a": 1, "b": 1, "c": 1}


def test_rerank_tiers_still_let_a_confident_reranker_win() -> None:
    # Guard against over-correcting: a reranker that genuinely separates
    # candidates keeps its authority even against a better first-stage rank.
    weak_first_stage = _evidence("confident", "subject", "fact-1")
    weak_first_stage.first_stage_rank = 12
    weak_first_stage.rerank_score = 0.95
    strong_first_stage = _evidence("rejected", "subject", "fact-2")
    strong_first_stage.first_stage_rank = 1
    strong_first_stage.rerank_score = 0.20

    cards = [weak_first_stage, strong_first_stage]
    tiers = StrongRetriever._rerank_tiers(cards)

    assert tiers == {"confident": 1, "rejected": 2}
    ordered = StrongRetriever._rank_fused(cards, tiers, pool_size=50)
    assert [card.card_id for card in ordered] == ["confident", "rejected"]


def test_topic_overlap_separates_a_matching_card_from_an_unrelated_one() -> None:
    # The gate that stopped the bot answering "怎样申请夜间无人机驾驶证" with the
    # nearest-looking application page.  Neither stage can express "none of
    # these are about it": both rank, and a pool of uniformly irrelevant cards
    # still has a best member.
    card = _evidence("scholarship", "scholarship", "apply")
    card.title = "本科生专项奖学金申请办理"
    card.source_title = "本科生专项奖学金申请办理"
    card.evidence_quote = ""

    assert topic_overlap("奖学金怎么申请", card) >= MIN_TOPIC_OVERLAP
    assert topic_overlap("夜间无人机驾驶证怎么办理", card) < MIN_TOPIC_OVERLAP


def test_boilerplate_alone_cannot_carry_a_question_over_the_line() -> None:
    # Campus questions and campus cards share a register - 本科生 / 申请 / 办理 -
    # and unweighted overlap let that shared register decide the outcome:
    # "本科生怎样申请夜间无人机驾驶证" scored 0.21 against a scholarship card
    # purely on phrasing, while the same question without the boilerplate
    # scored 0.00.  Subject matter has to carry it, not register.
    card = _evidence("scholarship", "scholarship", "apply")
    card.title = "本科生专项奖学金申请办理"
    card.source_title = "本科生专项奖学金申请办理"
    card.evidence_quote = ""

    assert topic_overlap("本科生怎样申请夜间无人机驾驶证", card) < MIN_TOPIC_OVERLAP
    assert topic_overlap("本科生如何办理校内宠物饲养许可证", card) < MIN_TOPIC_OVERLAP


def test_topic_overlap_admits_an_empty_question_rather_than_refusing() -> None:
    # A degenerate question is the caller's problem, not a reason to invent a
    # refusal path here.
    assert topic_overlap("", _evidence("a", "a", "a")) == 1.0


@pytest.mark.asyncio
async def test_an_off_topic_question_is_declined_rather_than_answered(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        retriever = StrongRetriever(database, HighConfidenceCurrentModels())

        # The scholarship card is the only thing in the pool and the reranker
        # scores it 0.95, so without the topic gate it would be served.
        with pytest.raises(InsufficientEvidence, match="nothing on this topic"):
            await retriever.retrieve("夜间无人机驾驶证怎么办理")
    finally:
        database.close()


def test_another_audience_is_recognised_by_who_acts_not_who_is_mentioned() -> None:
    # The knowledge base is undergraduate-only, and the topic gate cannot catch
    # these: a graduate scholarship question reads as highly on-topic against
    # the undergraduate scholarship cards.
    assert serves_another_audience("研究生国家奖学金评审材料在哪？") == "研究生"
    assert serves_another_audience("MBA学员如何申请学位？") == "MBA"
    assert serves_another_audience("教职工专属事项“培训选课”的办理流程是什么？") == "教职工"

    # Mentioned but not the one acting: a 指导教师 is a role on a student's team,
    # an 国际学生助学金 is a fund an undergraduate applies for, and a 教师岗位 is
    # a job an undergraduate applies to.
    assert serves_another_audience("暑期社会实践团队有哪些人数和指导教师要求？") is None
    assert serves_another_audience("参加SAF海外交流项目可以申请多少国际学生助学金？") is None

    # An explicit undergraduate marker settles it, whatever else is named.
    assert serves_another_audience("本科生如何申请免试攻读研究生？") is None
    assert serves_another_audience("推免研究生的名额怎么排？") is None

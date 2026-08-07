import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from luna_kb.contracts import ReviewStatus, ReviewedCard
from luna_kb.errors import RetrievalUnavailable
from luna_kb.pipeline.build import build_database
from luna_kb.retrieval import KnowledgeDatabase, StrongRetriever
from luna_kb.runtime_controls import QueueFull, WorkLimiter
from luna_kb.service import (
    NAVIGATION_ONLY_ANSWER,
    NAVIGATION_ONLY_QUALITY,
    AnswerService,
)


class LongAnswerModels:
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
        return [(index, 0.95) for index in range(len(documents))]

    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        answer = "本" * 161
        return {
            "answer": answer,
            "claims": [
                {
                    "text": answer,
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["evidence_quote"]],
                }
            ],
        }


class MultiSourceAnswerModels(LongAnswerModels):
    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        return [(index, 0.95 - index * 0.1) for index in range(len(documents))]

    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        answer = evidence[0]["evidence_quote"]
        return {
            "answer": answer,
            "claims": [
                {
                    "text": answer,
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["evidence_quote"]],
                }
            ],
        }


class FabricatedLinkModels(LongAnswerModels):
    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        answer = "详情请查看https://evil.example/award"
        return {
            "answer": answer,
            "claims": [
                {
                    "text": answer,
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["evidence_quote"]],
                }
            ],
        }


class BareDomainLinkModels(LongAnswerModels):
    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        answer = "详情请查看example.dlut.edu.cn/award"
        return {
            "answer": answer,
            "claims": [
                {
                    "text": answer,
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["evidence_quote"]],
                }
            ],
        }


class SummaryQuoteModels(LongAnswerModels):
    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        answer = "申请时间和材料以当年官方通知为准。"
        return {
            "answer": answer,
            "claims": [
                {
                    "text": answer,
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["summary"]],
                }
            ],
        }


class PartialClaimModels(LongAnswerModels):
    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        answer = "申请奖学金需要查看当年学生工作处通知。"
        return {
            "answer": answer,
            "claims": [
                {
                    "text": "需要",
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["evidence_quote"]],
                }
            ],
        }


class ParaphrasedClaimModels(LongAnswerModels):
    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        answer = "申请时间和材料以当年官方通知为准。"
        return {
            "answer": answer,
            "claims": [
                {
                    "text": answer,
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["evidence_quote"]],
                }
            ],
        }


class UnmappedGenericSentenceModels(LongAnswerModels):
    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        supported = "申请时间和材料以当年官方通知为准。"
        answer = supported + "请留意后续消息。"
        return {
            "answer": answer,
            "claims": [
                {
                    "text": supported,
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["evidence_quote"]],
                }
            ],
        }


class NavigationOnlyModels(LongAnswerModels):
    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        raise AssertionError("navigation-only answer must not call the answer model")


class CacheableAnswerModels(LongAnswerModels):
    def __init__(self) -> None:
        self.plan_calls = 0
        self.answer_calls = 0

    async def plan(self, question: str, history=None) -> dict:
        self.plan_calls += 1
        return await super().plan(question, history)

    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        self.answer_calls += 1
        answer = evidence[0]["evidence_quote"]
        return {
            "answer": answer,
            "claims": [
                {
                    "text": answer,
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["evidence_quote"]],
                }
            ],
        }


class SlowCacheableAnswerModels(CacheableAnswerModels):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def draft_answer(self, question: str, evidence: list[dict]) -> dict:
        self.answer_calls += 1
        self.started.set()
        await self.release.wait()
        answer = evidence[0]["evidence_quote"]
        return {
            "answer": answer,
            "claims": [
                {
                    "text": answer,
                    "card_ids": [evidence[0]["card_id"]],
                    "evidence_quotes": [evidence[0]["evidence_quote"]],
                }
            ],
        }


@pytest.mark.asyncio
async def test_draft_answer_allows_a_longer_human_reviewable_answer(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    models = LongAnswerModels()
    service = AnswerService(
        StrongRetriever(database, models),
        models,
        limiter=WorkLimiter(concurrency=1, max_queue=0),
    )
    try:
        result = await service.ask("奖学金怎么申请")
        assert len(result.answer) == 161
        assert result.needs_review
    finally:
        database.close()


@pytest.mark.asyncio
async def test_draft_answer_does_not_require_exact_claim_mapping(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    models = UnmappedGenericSentenceModels()
    service = AnswerService(
        StrongRetriever(database, models),
        models,
        limiter=WorkLimiter(concurrency=1, max_queue=0),
    )
    try:
        result = await service.ask("奖学金怎么申请")
        assert result.answer
        assert result.cited_card_ids
    finally:
        await service.close()
        database.close()


@pytest.mark.asyncio
async def test_model_generated_link_is_rejected_before_reply(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    models = FabricatedLinkModels()
    service = AnswerService(StrongRetriever(database, models), models)
    try:
        with pytest.raises(RetrievalUnavailable) as error:
            await service.ask("奖学金怎么申请")

        assert error.value.component == "answer_model"
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_type",
    [BareDomainLinkModels, SummaryQuoteModels, PartialClaimModels, ParaphrasedClaimModels],
)
async def test_draft_mode_only_rejects_generated_links(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
    model_type: type[LongAnswerModels],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    models = model_type()
    service = AnswerService(StrongRetriever(database, models), models)
    try:
        if model_type is BareDomainLinkModels:
            with pytest.raises(RetrievalUnavailable):
                await service.ask("奖学金怎么申请")
        else:
            result = await service.ask("奖学金怎么申请")
            assert result.answer
    finally:
        database.close()


@pytest.mark.asyncio
async def test_answer_uses_only_one_source_end_to_end(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    cards = [
        make_approved_card(
            card_id="card-primary",
            card_title="奖学金申请要求",
            source_id="kb_clean:primary",
            canonical_url="https://example.dlut.edu.cn/primary",
            source_title="奖学金申请要求通知",
            fact_key="requirements",
        ),
        make_approved_card(
            card_id="card-secondary",
            card_title="奖学金办理入口",
            source_id="kb_clean:secondary",
            canonical_url="https://example.dlut.edu.cn/secondary",
            source_title="奖学金办理入口通知",
            fact_key="entry",
        ),
    ]
    await build_database(cards, database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    models = MultiSourceAnswerModels()
    service = AnswerService(StrongRetriever(database, models), models)
    try:
        result = await service.ask("奖学金怎么申请")

        assert len(result.sources) == 1
        assert len(result.cited_card_ids) == 1
        assert len({card.source_id for card in result.retrieval.cards}) == 1
        assert result.sources[0].url == result.retrieval.cards[0].canonical_url
    finally:
        database.close()


@pytest.mark.asyncio
async def test_navigation_only_result_returns_a_program_owned_official_link(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    item = make_approved_card()
    item.review_status = ReviewStatus.DOWNGRADED
    item.card.card_kind = "navigation"
    item.card.summary = ""
    item.card.evidence_quote = ""
    item.card.source_locator = ""
    item.card.facts = {}
    item.card.facets = []
    await build_database([item], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    models = NavigationOnlyModels()
    service = AnswerService(StrongRetriever(database, models), models)
    try:
        result = await service.ask("奖学金怎么申请")

        assert result.answer == NAVIGATION_ONLY_ANSWER
        # "Found the entry point, no procedure text" is its own answer status,
        # not a draft, and must not read as "this does not exist".
        assert result.quality == NAVIGATION_ONLY_QUALITY
        assert not result.needs_review
        assert result.sources[0].url == item.source.canonical_url
        assert result.cited_card_ids == (item.card.card_id,)
    finally:
        database.close()


@pytest.mark.asyncio
async def test_repeated_context_free_question_uses_answer_cache(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    models = CacheableAnswerModels()
    service = AnswerService(StrongRetriever(database, models), models)
    try:
        first = await service.ask("奖学金怎么申请", history=[])
        second = await service.ask("奖学金怎么申请", history=[])

        assert second is first
        assert models.plan_calls == 1
        assert models.answer_calls == 1
    finally:
        await service.close()
        database.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_the_underlying_execution_lease(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    models = SlowCacheableAnswerModels()
    service = AnswerService(
        StrongRetriever(database, models),
        models,
        limiter=WorkLimiter(concurrency=1, max_queue=0),
    )
    try:
        waiter = asyncio.create_task(service.ask("奖学金怎么申请", history=[]))
        await models.started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        with pytest.raises(QueueFull):
            await service.ask("校园卡怎么补办", history=[])

        models.release.set()
        result = await service.ask("奖学金怎么申请", history=[])
        assert result.answer
    finally:
        await service.close()
        database.close()


@pytest.mark.asyncio
async def test_concurrent_identical_questions_share_one_inflight_answer(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    models = SlowCacheableAnswerModels()
    service = AnswerService(
        StrongRetriever(database, models),
        models,
        limiter=WorkLimiter(concurrency=1, max_queue=0),
    )
    try:
        first = asyncio.create_task(service.ask("奖学金怎么申请", history=[]))
        await models.started.wait()
        second = asyncio.create_task(service.ask("奖学金怎么申请", history=[]))
        await asyncio.sleep(0)
        models.release.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert second_result is first_result
        assert models.plan_calls == 1
        assert models.answer_calls == 1
    finally:
        await service.close()
        database.close()


@pytest.mark.asyncio
async def test_close_waits_for_contextual_executions_outside_the_answer_cache(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    models = SlowCacheableAnswerModels()
    await build_database([make_approved_card()], database_path, models, 3)
    database = KnowledgeDatabase(database_path, 3)
    service = AnswerService(
        StrongRetriever(database, models, 0.35),
        models,
        limiter=WorkLimiter(concurrency=1, max_queue=0),
    )
    request = asyncio.create_task(
        service.ask(
            "奖学金怎么申请",
            history=[
                {"role": "user", "content": "学校奖学金有哪些？"},
                {"role": "assistant", "content": "请说明你想了解的事项。"},
            ],
        )
    )
    try:
        await models.started.wait()
        assert not service._inflight

        await service.close()

        with pytest.raises(asyncio.CancelledError):
            await request
        with pytest.raises(RetrievalUnavailable, match="closed"):
            await service.ask("校园卡怎么补办")
    finally:
        models.release.set()
        await service.close()
        database.close()


def _fact_card(evidence: str):
    from luna_kb.retrieval import CardEvidence

    return CardEvidence(
        card_id='card-1', source_id='kb_clean:s', parent_card_id=None,
        title='t', summary='', evidence_quote=evidence, source_locator='正文',
        facts={}, facets=[], campus='', audience='本科生', validity='current',
        card_kind='fact', subject_key='sk', fact_key='fk', retrieval_text='',
        canonical_url='https://example.dlut.edu.cn/a', source_title='st',
        published_at=None,
    )


def test_quoting_a_url_from_the_evidence_is_not_fabrication() -> None:
    # Several cards exist to say "go to this address", and their evidence quotes
    # the URL.  Blocking every URL made those cards unanswerable while the
    # program attached the very same link underneath as a citation.
    from luna_kb.service import _fabricated_urls

    card = _fact_card(
        "可使用该账号和密码登录 http://tulip.dlut.edu.cn 进行自助服务。"
    )
    assert _fabricated_urls("请登录 http://tulip.dlut.edu.cn 办理。", [card]) == []


def test_a_url_absent_from_the_evidence_is_still_fabrication() -> None:
    from luna_kb.service import _fabricated_urls

    card = _fact_card("携带学生证到一站式服务大厅办理。")
    assert _fabricated_urls("请访问 http://its.dlut.edu.cn/reset 重置。", [card])


def test_an_answer_without_links_passes() -> None:
    from luna_kb.service import _fabricated_urls

    card = _fact_card("登录 http://pay.dlut.edu.cn 缴费。")
    assert _fabricated_urls("携带学生证到服务大厅办理即可。", [card]) == []

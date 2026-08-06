from pathlib import Path

import pytest

from luna_kb.application import CampusQaApplication
from luna_kb.config import Settings
from luna_kb.errors import InsufficientEvidence, RetrievalUnavailable
from luna_kb.policy import InboundMessage
from luna_kb.runtime_controls import QueueFull


class FakeAnswer:
    answer = "申请时间和材料以当年官方通知为准。"

    def format_text(self) -> str:
        return self.answer


class RecordingAnswers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []

    async def ask(self, question: str, history=None) -> FakeAnswer:
        self.calls.append((question, list(history or [])))
        return FakeAnswer()


class EvidenceFailureAnswers:
    async def ask(self, question: str, history=None) -> FakeAnswer:
        raise InsufficientEvidence("no matching evidence")


class SystemFailureAnswers:
    async def ask(self, question: str, history=None) -> FakeAnswer:
        raise RetrievalUnavailable("reranker", "gateway unavailable")


class QueueFullAnswers:
    async def ask(self, question: str, history=None) -> FakeAnswer:
        raise QueueFull("answer queue is full")


@pytest.mark.asyncio
async def test_allowed_group_gets_an_explicit_error_when_runtime_failed_to_start() -> None:
    settings = Settings(
        release_root=Path("releases"),
        model_base_url="https://models.example.test/v1",
        model_api_key="test-key",
        allowed_group_ids=frozenset({10001}),
    )
    application = CampusQaApplication(answers=None, settings=settings)

    response = await application.handle(
        InboundMessage(
            message_id="message-1",
            message_type="group",
            group_id=10001,
            user_id=20001,
            text="#奖学金怎么申请",
        )
    )

    assert response == "检索服务暂时异常，请稍后再试。错误编号：STARTUP"


@pytest.mark.asyncio
async def test_forced_question_is_answered_without_passing_hash_to_retrieval() -> None:
    settings = Settings(
        release_root=Path("releases"),
        model_base_url="https://models.example.test/v1",
        model_api_key="test-key",
        allowed_group_ids=frozenset({10001}),
    )
    answers = RecordingAnswers()
    application = CampusQaApplication(answers=answers, settings=settings)

    response = await application.handle(
        InboundMessage(
            message_id="message-1",
            message_type="group",
            group_id=10001,
            user_id=20001,
            text="# 奖学金怎么申请",
        )
    )

    assert response == "申请时间和材料以当年官方通知为准。"
    assert answers.calls == [("奖学金怎么申请", [])]


@pytest.mark.asyncio
async def test_evidence_shortage_and_system_failure_have_different_messages() -> None:
    settings = Settings(
        release_root=Path("releases"),
        model_base_url="https://models.example.test/v1",
        model_api_key="test-key",
        allowed_group_ids=frozenset({10001}),
    )
    message = InboundMessage(
        message_id="message-1",
        message_type="group",
        group_id=10001,
        user_id=20001,
        text="#奖学金怎么申请",
    )

    evidence_response = await CampusQaApplication(
        EvidenceFailureAnswers(), settings
    ).handle(message)
    system_response = await CampusQaApplication(SystemFailureAnswers(), settings).handle(
        message
    )

    assert evidence_response == "知识库暂未收录足够依据。"
    assert system_response is not None
    assert system_response.startswith("检索服务暂时异常，请稍后再试。错误编号：")


@pytest.mark.asyncio
async def test_oversized_question_is_rejected_before_model_call() -> None:
    settings = Settings(
        release_root=Path("releases"),
        model_base_url="https://models.example.test/v1",
        model_api_key="test-key",
        max_question_chars=10,
        allowed_group_ids=frozenset({10001}),
    )
    answers = RecordingAnswers()
    application = CampusQaApplication(answers=answers, settings=settings)

    response = await application.handle(
        InboundMessage(
            message_id="message-long",
            message_type="group",
            group_id=10001,
            user_id=20001,
            text="#" + "很长的问题" * 3,
        )
    )

    assert response == "问题过长，请精简到10字以内。"
    assert answers.calls == []


@pytest.mark.asyncio
async def test_execution_queue_full_is_reported_by_the_application() -> None:
    settings = Settings(
        release_root=Path("releases"),
        model_base_url="https://models.example.test/v1",
        model_api_key="test-key",
        allowed_group_ids=frozenset({10001}),
    )
    application = CampusQaApplication(answers=QueueFullAnswers(), settings=settings)

    response = await application.handle(
        InboundMessage(
            message_id="message-queue",
            message_type="group",
            group_id=10001,
            user_id=20001,
            text="#奖学金怎么申请",
        )
    )

    assert response == "当前提问较多，队列已满，请稍后再试。"

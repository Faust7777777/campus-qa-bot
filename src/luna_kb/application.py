from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from .config import Settings
from .errors import InsufficientEvidence, RetrievalUnavailable
from .policy import DecisionKind, InboundMessage, MessagePolicy
from .runtime_controls import ConversationStore, MessageGate, QueueFull


logger = logging.getLogger(__name__)


class AnswerEngine(Protocol):
    async def ask(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
    ) -> Any: ...


class CampusQaApplication:
    def __init__(self, answers: AnswerEngine | None, settings: Settings) -> None:
        self.answers = answers
        self.policy = MessagePolicy(set(settings.allowed_group_ids))
        self.history = ConversationStore(
            settings.history_turns,
            settings.history_ttl_seconds,
            max_conversations=settings.history_max_conversations,
        )
        self.gate = MessageGate(
            settings.message_dedupe_seconds,
            settings.user_cooldown_seconds,
            max_entries=settings.message_dedupe_max_entries,
        )
        self.max_question_chars = settings.max_question_chars
        self.answer_total_timeout_seconds = settings.answer_total_timeout_seconds

    async def handle(self, message: InboundMessage) -> str | None:
        decision = self.policy.decide(message)
        if decision.kind is DecisionKind.IGNORE:
            return None
        if len(decision.question) > self.max_question_chars:
            return f"问题过长，请精简到{self.max_question_chars}字以内。"
        key = (message.group_id, message.user_id)
        if message.group_id is None:
            return None
        admission = self.gate.admit(message.message_id, key)
        if not admission.accepted:
            return None
        if self.answers is None:
            return "检索服务暂时异常，请稍后再试。错误编号：STARTUP"
        try:
            async with asyncio.timeout(self.answer_total_timeout_seconds):
                result = await self.answers.ask(
                    decision.question,
                    self.history.get(key),
                )
        except InsufficientEvidence:
            return "知识库暂未收录足够依据。"
        except RetrievalUnavailable as exc:
            logger.error(
                "campus QA retrieval failed error_id=%s component=%s group_id=%s",
                exc.error_id,
                exc.component,
                message.group_id,
            )
            return exc.public_message
        except TimeoutError:
            error = RetrievalUnavailable(
                "timeout",
                f"answer exceeded {self.answer_total_timeout_seconds:g} seconds",
            )
            logger.error(
                "campus QA answer timed out error_id=%s group_id=%s",
                error.error_id,
                message.group_id,
            )
            return error.public_message
        except QueueFull:
            return "当前提问较多，队列已满，请稍后再试。"
        except Exception as exc:
            error = RetrievalUnavailable("bot", str(exc))
            logger.exception(
                "campus QA unhandled failure error_id=%s group_id=%s",
                error.error_id,
                message.group_id,
            )
            return error.public_message
        self.history.append(key, decision.question, result.answer)
        return str(result.format_text())

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


QUESTION_CUES = (
    "怎么", "怎样", "咋", "如何", "什么", "啥",
    "哪里", "哪儿", "哪个", "哪些", "谁",
    "为何", "为什么", "多少", "多久", "几时", "几点", "几个",
    "是否", "能否", "可以吗", "有没有", "需不需要",
)
# Chinese asks plenty of questions without a question mark.  "食堂好吃不" is a
# question and was ignored, while "食堂好吃不？" was answered - a difference in
# punctuation, not in what was asked, and most people do not punctuate a chat
# message.  Two patterns cover most of the rest: a sentence ending in 吗/呢/不,
# and the A-not-A form (能不能, 是不是, 好不好, 用不用).
QUESTION_TAILS = ("吗", "呢", "不")
A_NOT_A = re.compile(r"(.)不\1")
NOISE_PHRASES = frozenset(
    {
        "好的",
        "收到",
        "谢谢",
        "感谢",
        "哈哈",
        "你好",
        "在吗",
        "hello",
        "hi",
        "明白",
        "知道了",
        "了解",
    }
)


def obvious_noise(text: str) -> bool:
    stripped = text.strip()
    if not stripped or re.fullmatch(r"[\d\s\W_]+", stripped):
        return True
    compact = re.sub(r"[\s?？!！。,.，]+", "", stripped).lower()
    if compact in NOISE_PHRASES:
        return True
    for size in range(2, min(len(compact) // 2 + 1, 9)):
        if len(compact) % size == 0 and compact == compact[:size] * (len(compact) // size):
            return True
    return False


class DecisionKind(StrEnum):
    IGNORE = "ignore"
    FORCE = "force"
    CLASSIFY = "classify"


@dataclass(frozen=True, slots=True)
class InboundMessage:
    message_id: str
    message_type: str
    group_id: int | None
    user_id: int
    text: str


@dataclass(frozen=True, slots=True)
class MessageDecision:
    kind: DecisionKind
    reason: str
    question: str = ""


class MessagePolicy:
    def __init__(
        self,
        allowed_group_ids: set[int],
        allowed_user_ids: set[int] | None = None,
    ) -> None:
        self.allowed_group_ids = frozenset(allowed_group_ids)
        # Private messages are ignored unless the sender is listed.  Testing in
        # a group means every wrong answer is public, so there has to be a way
        # to try the bot without an audience - but only for named accounts, or
        # anyone who finds the QQ number gets a private endpoint to it.
        self.allowed_user_ids = frozenset(allowed_user_ids or ())

    def decide(self, message: InboundMessage) -> MessageDecision:
        if message.message_type != "group":
            if message.user_id not in self.allowed_user_ids:
                return MessageDecision(DecisionKind.IGNORE, "private_message")
        elif message.group_id not in self.allowed_group_ids:
            return MessageDecision(DecisionKind.IGNORE, "group_not_allowed")
        text = message.text.strip()
        if text.startswith("#"):
            question = text.removeprefix("#").strip()
            if not question:
                return MessageDecision(DecisionKind.IGNORE, "empty_forced_question")
            return MessageDecision(
                DecisionKind.FORCE,
                "hash_prefix",
                question=question,
            )
        if obvious_noise(text):
            return MessageDecision(DecisionKind.IGNORE, "obvious_noise")
        # A private chat with the bot is addressed to the bot by definition, so
        # everything that is not obvious noise is treated as a question.  The
        # cue list exists to pick questions out of group chatter; applying it
        # one-to-one only means the sender has to guess the vocabulary.
        if message.message_type != "group" or self._reads_as_a_question(text):
            return MessageDecision(
                DecisionKind.CLASSIFY,
                "question_candidate",
                question=text,
            )
        return MessageDecision(DecisionKind.IGNORE, "not_a_question")

    @staticmethod
    def _reads_as_a_question(text: str) -> bool:
        stripped = text.rstrip("。.! ！~ ")
        return (
            text.endswith(("?", "？"))
            or stripped.endswith(QUESTION_TAILS)
            or bool(A_NOT_A.search(text))
            or any(cue in text for cue in QUESTION_CUES)
        )

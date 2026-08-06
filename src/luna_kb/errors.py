from __future__ import annotations

import secrets


class KnowledgeError(Exception):
    """Base domain error."""


class ContractError(KnowledgeError):
    """Input does not satisfy the knowledge contract."""


class BuildError(KnowledgeError):
    """A release cannot be built safely."""


class InsufficientEvidence(KnowledgeError):
    """Retrieval worked, but evidence does not cover the question."""


class RetrievalUnavailable(KnowledgeError):
    """A critical retrieval dependency failed; no fallback is permitted."""

    def __init__(self, component: str, detail: str = "") -> None:
        self.component = component
        self.error_id = secrets.token_hex(4).upper()
        self.detail = detail
        super().__init__(f"{component}: {detail}" if detail else component)

    @property
    def public_message(self) -> str:
        return f"检索服务暂时异常，请稍后再试。错误编号：{self.error_id}"


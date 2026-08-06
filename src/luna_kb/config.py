from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ContractError


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ContractError(f"missing required environment variable: {name}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    release_root: Path
    model_base_url: str
    model_api_key: str
    planner_model: str = "Qwen3.5-9B"
    embedding_model: str = "bge-m3"
    embedding_dimension: int = 1024
    reranker_model: str = "Qwen3-Reranker-8B"
    answer_model: str = "Qwen3.5-35B-A3B"
    # ``draft`` is the product mode: the model may paraphrase evidence and
    # imperfect answers are surfaced for human review instead of blocking the
    # whole response.  ``strict`` remains available for regression/audit runs.
    answer_mode: str = "draft"
    answer_max_chars: int = 300
    answer_max_sources: int = 3
    fast_path_enabled: bool = True
    reranker_url: str = ""
    rerank_min_score: float = 0.35
    request_timeout: float = 20.0
    oom_score_adj: int = 500
    history_turns: int = 3
    history_ttl_seconds: float = 1800.0
    history_max_conversations: int = 2048
    message_dedupe_seconds: float = 600.0
    message_dedupe_max_entries: int = 4096
    user_cooldown_seconds: float = 3.0
    answer_concurrency: int = 2
    answer_queue_size: int = 50
    answer_cache_ttl_seconds: float = 300.0
    answer_cache_size: int = 256
    max_question_chars: int = 500
    answer_queue_timeout_seconds: float = 30.0
    answer_total_timeout_seconds: float = 90.0
    allowed_group_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = _required("LUNA_MODEL_BASE_URL").rstrip("/")
        return cls(
            release_root=Path(_required("LUNA_RELEASE_ROOT")).expanduser().resolve(),
            model_base_url=base_url,
            model_api_key=_required("LUNA_MODEL_API_KEY"),
            planner_model=os.getenv("LUNA_PLANNER_MODEL", "Qwen3.5-9B"),
            embedding_model=os.getenv("LUNA_EMBEDDING_MODEL", "bge-m3"),
            embedding_dimension=int(os.getenv("LUNA_EMBEDDING_DIMENSION", "1024")),
            reranker_model=os.getenv("LUNA_RERANKER_MODEL", "Qwen3-Reranker-8B"),
            answer_model=os.getenv("LUNA_ANSWER_MODEL", "Qwen3.5-35B-A3B"),
            answer_mode=os.getenv("LUNA_ANSWER_MODE", "draft").strip().lower(),
            answer_max_chars=int(os.getenv("LUNA_ANSWER_MAX_CHARS", "300")),
            answer_max_sources=int(os.getenv("LUNA_ANSWER_MAX_SOURCES", "3")),
            fast_path_enabled=os.getenv("LUNA_FAST_PATH_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            reranker_url=os.getenv("LUNA_RERANKER_URL", f"{base_url}/rerank"),
            rerank_min_score=float(os.getenv("LUNA_RERANK_MIN_SCORE", "0.35")),
            request_timeout=float(os.getenv("LUNA_REQUEST_TIMEOUT", "20")),
            oom_score_adj=int(os.getenv("LUNA_OOM_SCORE_ADJ", "500")),
            history_turns=int(os.getenv("LUNA_HISTORY_TURNS", "3")),
            history_ttl_seconds=float(os.getenv("LUNA_HISTORY_TTL_SECONDS", "1800")),
            history_max_conversations=int(
                os.getenv("LUNA_HISTORY_MAX_CONVERSATIONS", "2048")
            ),
            message_dedupe_seconds=float(os.getenv("LUNA_MESSAGE_DEDUPE_SECONDS", "600")),
            message_dedupe_max_entries=int(
                os.getenv("LUNA_MESSAGE_DEDUPE_MAX_ENTRIES", "4096")
            ),
            user_cooldown_seconds=float(os.getenv("LUNA_USER_COOLDOWN_SECONDS", "3")),
            answer_concurrency=int(os.getenv("LUNA_ANSWER_CONCURRENCY", "2")),
            answer_queue_size=int(os.getenv("LUNA_ANSWER_QUEUE_SIZE", "50")),
            answer_cache_ttl_seconds=float(os.getenv("LUNA_ANSWER_CACHE_TTL_SECONDS", "300")),
            answer_cache_size=int(os.getenv("LUNA_ANSWER_CACHE_SIZE", "256")),
            max_question_chars=int(os.getenv("LUNA_MAX_QUESTION_CHARS", "500")),
            answer_queue_timeout_seconds=float(
                os.getenv("LUNA_ANSWER_QUEUE_TIMEOUT_SECONDS", "30")
            ),
            answer_total_timeout_seconds=float(
                os.getenv("LUNA_ANSWER_TOTAL_TIMEOUT_SECONDS", "90")
            ),
            allowed_group_ids=frozenset(
                int(value.strip())
                for value in _required("LUNA_ALLOWED_GROUP_IDS").split(",")
                if value.strip()
            ),
        )

    def validate(self) -> None:
        if self.embedding_dimension <= 0:
            raise ContractError("embedding dimension must be positive")
        if not 0 <= self.oom_score_adj <= 1000:
            raise ContractError("oom_score_adj must be between 0 and 1000")
        if self.request_timeout <= 0:
            raise ContractError("request timeout must be positive")
        if not 0 <= self.rerank_min_score <= 1:
            raise ContractError("rerank minimum score must be between 0 and 1")
        if self.answer_mode not in {"draft", "strict"}:
            raise ContractError("answer mode must be draft or strict")
        if self.answer_max_chars <= 0 or self.answer_max_sources <= 0:
            raise ContractError("answer output limits must be positive")
        if (
            self.history_turns <= 0
            or self.history_ttl_seconds <= 0
            or self.history_max_conversations <= 0
        ):
            raise ContractError("history limits must be positive")
        if (
            self.message_dedupe_seconds <= 0
            or self.message_dedupe_max_entries <= 0
            or self.user_cooldown_seconds < 0
        ):
            raise ContractError("message timing limits are invalid")
        if self.answer_concurrency <= 0 or self.answer_queue_size < 0:
            raise ContractError("answer capacity limits are invalid")
        if self.answer_cache_ttl_seconds < 0 or self.answer_cache_size < 0:
            raise ContractError("answer cache limits are invalid")
        if self.max_question_chars <= 0:
            raise ContractError("maximum question length must be positive")
        if self.answer_queue_timeout_seconds <= 0 or self.answer_total_timeout_seconds <= 0:
            raise ContractError("answer timeout limits must be positive")
        if not self.allowed_group_ids:
            raise ContractError("at least one allowed group id is required")

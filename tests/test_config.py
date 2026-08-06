from pathlib import Path

import pytest

from luna_kb.config import Settings
from luna_kb.errors import ContractError


def test_default_answer_model_is_the_35b_model() -> None:
    settings = Settings(
        release_root=Path("releases"),
        model_base_url="https://models.example.test/v1",
        model_api_key="test-key",
    )

    assert settings.answer_model == "Qwen3.5-35B-A3B"
    assert settings.rerank_min_score == 0.35


def test_runtime_limits_match_the_confirmed_vps_capacity() -> None:
    settings = Settings(
        release_root=Path("releases"),
        model_base_url="https://models.example.test/v1",
        model_api_key="test-key",
    )

    assert settings.history_turns == 3
    assert settings.history_ttl_seconds == 1800
    assert settings.message_dedupe_seconds == 600
    assert settings.user_cooldown_seconds == 3
    assert settings.answer_concurrency == 2
    assert settings.answer_queue_size == 50
    assert settings.max_question_chars == 500
    assert settings.answer_queue_timeout_seconds == 30
    assert settings.answer_total_timeout_seconds == 90


def test_empty_group_allowlist_fails_closed() -> None:
    settings = Settings(
        release_root=Path("releases"),
        model_base_url="https://models.example.test/v1",
        model_api_key="test-key",
    )

    with pytest.raises(ContractError, match="allowed group"):
        settings.validate()

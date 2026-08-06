import json

import httpx
import pytest

from luna_kb.clients import (
    MAX_EMBEDDING_BATCH_SIZE,
    MAX_MODEL_RESPONSE_BYTES,
    ModelEndpoints,
    RemoteModels,
    release_model_config,
)
from luna_kb.errors import RetrievalUnavailable


@pytest.mark.asyncio
async def test_answer_model_prompt_allows_paraphrase_and_review_signals() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"answer": "", "claims": []}, ensure_ascii=False
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    models = RemoteModels(
        ModelEndpoints(
            base_url="https://models.example.test/v1",
            api_key="test-key",
            planner_model="Qwen3.5-9B",
            embedding_model="bge-m3",
            reranker_model="Qwen3-Reranker-8B",
            answer_model="Qwen3.5-35B-A3B",
            reranker_url="https://models.example.test/rerank",
            timeout=5,
        ),
        client=client,
    )
    try:
        await models.draft_answer("奖学金怎么申请", [{"card_id": "card-1"}])
    finally:
        await client.aclose()

    system_prompt = captured["messages"][0]["content"]
    assert "evidence_quotes" in system_prompt
    assert "不要求逐字引用" in system_prompt
    assert "needs_review" in system_prompt
    assert "80至240" in system_prompt
    assert "最多300字" in system_prompt
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_model_response_body_has_a_hard_byte_limit() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_MODEL_RESPONSE_BYTES + 1))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    models = RemoteModels(
        ModelEndpoints(
            base_url="https://models.example.test/v1",
            api_key="test-key",
            planner_model="planner",
            embedding_model="embedding",
            reranker_model="reranker",
            answer_model="answer",
            reranker_url="https://models.example.test/rerank",
            timeout=5,
        ),
        client=client,
    )
    try:
        with pytest.raises(RetrievalUnavailable, match="byte limit"):
            await models.plan("问题")
    finally:
        await client.aclose()


def test_release_model_config_binds_behavior_without_leaking_api_key() -> None:
    endpoints = ModelEndpoints(
        base_url="https://models.example.test/v1",
        api_key="must-not-leak",
        planner_model="planner",
        embedding_model="embedding",
        reranker_model="reranker",
        answer_model="answer",
        reranker_url="https://models.example.test/rerank",
        timeout=5,
    )

    config = release_model_config(
        endpoints,
        embedding_dimension=1024,
        rerank_min_score=0.35,
    )

    assert "must-not-leak" not in json.dumps(config)
    assert len(config["endpoint_sha256"]) == 64
    assert len(config["prompt_contract_sha256"]) == 64
    assert len(config["runtime_code_sha256"]) == 64
    assert config["max_model_response_bytes"] == MAX_MODEL_RESPONSE_BYTES


@pytest.mark.asyncio
async def test_embedding_client_rejects_an_oversized_batch_before_http() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    models = RemoteModels(
        ModelEndpoints(
            base_url="https://models.example.test/v1",
            api_key="test-key",
            planner_model="planner",
            embedding_model="embedding",
            reranker_model="reranker",
            answer_model="answer",
            reranker_url="https://models.example.test/rerank",
            timeout=5,
        ),
        client=client,
    )
    try:
        with pytest.raises(RetrievalUnavailable, match="batch exceeds"):
            await models.embed(["text"] * (MAX_EMBEDDING_BATCH_SIZE + 1))
    finally:
        await client.aclose()

    assert calls == 0


@pytest.mark.asyncio
async def test_embedding_client_rejects_duplicate_response_indexes() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 0, "embedding": [0.0, 1.0]},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    models = RemoteModels(
        ModelEndpoints(
            base_url="https://models.example.test/v1",
            api_key="test-key",
            planner_model="planner",
            embedding_model="embedding",
            reranker_model="reranker",
            answer_model="answer",
            reranker_url="https://models.example.test/rerank",
            timeout=5,
        ),
        client=client,
    )
    try:
        with pytest.raises(RetrievalUnavailable, match="indexes"):
            await models.embed(["first", "second"])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_post_retries_gateway_rate_limit_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.5"})
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]},
        )

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("luna_kb.clients.asyncio.sleep", fake_sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    models = RemoteModels(
        ModelEndpoints(
            base_url="https://models.example.test/v1",
            api_key="test-key",
            planner_model="planner",
            embedding_model="embedding",
            reranker_model="reranker",
            answer_model="answer",
            reranker_url="https://models.example.test/rerank",
            timeout=5,
        ),
        client=client,
    )
    try:
        vectors = await models.embed(["retry me"])
    finally:
        await client.aclose()

    assert vectors == [[1.0, 0.0]]
    assert calls == 2
    assert sleeps == [0.5]


@pytest.mark.asyncio
async def test_healthcheck_probes_all_four_model_contracts() -> None:
    called_models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        called_models.append(payload["model"])
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
            )
        if request.url.path.endswith("/rerank"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"index": 0, "relevance_score": 0.9},
                        {"index": 1, "relevance_score": 0.1},
                    ]
                },
            )
        if payload["model"] == "planner":
            content = {
                "intent": "procedure",
                "standalone_query": "本科生校园事项办理说明",
                "subqueries": ["本科生校园事项办理说明"],
                "entities": ["校园事项"],
                "required_facets": ["入口"],
                "filters": {
                    "campus": "",
                    "audience": "本科生",
                    "time_scope": "current",
                },
            }
        else:
            evidence = json.loads(payload["messages"][1]["content"])["evidence"][0]
            quote = evidence["evidence_quote"]
            content = {
                "answer": quote,
                "claims": [
                    {
                        "text": quote,
                        "card_ids": [evidence["card_id"]],
                        "evidence_quotes": [quote],
                    }
                ],
            }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    models = RemoteModels(
        ModelEndpoints(
            base_url="https://models.example.test/v1",
            api_key="test-key",
            planner_model="planner",
            embedding_model="embedding",
            reranker_model="reranker",
            answer_model="answer",
            reranker_url="https://models.example.test/rerank",
            timeout=5,
        ),
        client=client,
    )
    try:
        await models.healthcheck(2)
    finally:
        await client.aclose()

    assert called_models == ["embedding", "reranker", "planner", "answer"]

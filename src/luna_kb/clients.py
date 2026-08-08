from __future__ import annotations

import ast
import asyncio
import json
import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .contracts import MAX_EMBEDDING_BATCH_SIZE, MAX_SEMANTIC_TEXT_CHARS
from .errors import RetrievalUnavailable

MAX_MODEL_RESPONSE_BYTES = 2 * 1024 * 1024
TRANSIENT_STATUS_CODES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How long to keep absorbing transient gateway failures.

    Online and offline callers want opposite things from the same client, so
    the budget is a parameter rather than a module constant.
    """

    max_retries: int
    base_delay_seconds: float
    max_delay_seconds: float

    def delay(self, attempt: int, retry_after: str = "") -> float:
        """Backoff for one failure, honouring a bounded ``retry-after``.

        A gateway asking for a very long wait is clamped like any other delay;
        the server does not get to decide how long a caller blocks.
        """

        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = self.base_delay_seconds * float(2**attempt)
        return min(max(delay, self.base_delay_seconds), self.max_delay_seconds)


# Someone in the group is waiting.  Five retries with a doubling backoff capped
# at 30s let one question sleep 2+4+8+16+30 = 60 seconds before failing, which
# is where the observed 48-second tail came from.  Fail fast and say so.
ONLINE_RETRY_POLICY = RetryPolicy(
    max_retries=3, base_delay_seconds=1.0, max_delay_seconds=15.0
)
# Nobody is waiting on a build, and there is no resume: embeddings live only in
# memory, so one unabsorbed 429 discards every vector computed so far and the
# whole ~101-request run starts over.  Waiting a minute is far cheaper than
# that, and the full 3214-card build was originally produced under a budget
# this generous.
OFFLINE_RETRY_POLICY = RetryPolicy(
    max_retries=6, base_delay_seconds=1.0, max_delay_seconds=30.0
)


MODEL_PROTOCOL_VERSION = "evidence-draft-v2"
PLANNER_SYSTEM_PROMPT = (
    "你是校园知识库查询规划器。仅输出JSON对象，字段必须为intent、standalone_query、"
    "subqueries、entities、required_facets、filters。intent只能是fact/procedure/historical/"
    "out_of_scope；subqueries最多3个；filters只能含campus、audience、time_scope，"
    "campus只能是空字符串、凌水、开发区、盘锦或全校，audience必须填本科生，"
    "time_scope只能是current/historical。required_facets只填写答案必须覆盖的功能面，"
    "优先从资格、申请、材料、流程、时间、期限、地点、联系、入口、培训、费用、"
    "工作量、规则、审批、结果、课程、考试、成绩、账号、密码、设备、报修、医保、"
    "安全、退费、住宿中选择，不要把问题主题本身当作功能面。简单问题只给一个subquery。"
)
SELECTOR_SYSTEM_PROMPT = (
    "你是大连理工大学本科生答疑助手的选卡环节。下面给出一批候选卡片，每张有编号、标题和正文。"
    "候选里有两种：带正文的知识卡，和只有标题的官方入口卡"
    "（正文位置写着“无正文，仅为官方入口链接”）。\n"
    "分两步判断：\n"
    "1. picked：哪些知识卡的正文能直接回答问题。只看正文是否真的含有问题要的信息，"
    "不要因为标题像就选。最多 3 张，按相关性排序。\n"
    "标了【往年通知】的卡出自已经过期的通知。只有当没有任何现行卡片能回答时才选它，"
    "同时存在时一律优先现行的。\n"
    "2. entry_points：仅当 picked 为空时才填。哪些官方入口卡正是办理这件事的官方页面，"
    "可以让提问者自己去查。最多 3 张。不确定是不是同一件事就不要填。\n"
    "先看这句话是不是真的在求信息。陈述句、反问句（明知答案、只为强调，如「你们不还要选课吗」）、"
    "玩笑、对人不对事的发问，都直接返回空，不要因为它提到了某个校内事项就去找卡。\n"
    "两者都为空是正常且必要的结果：问题超出校内本科生事务范围、涉及他人隐私、"
    "或候选里根本没有相关内容时，都返回空，绝不勉强凑一张。\n"
    '只输出 JSON：{"picked": [编号, ...], "entry_points": [编号, ...], "reason": "一句话"}'
)

ANSWER_SYSTEM_PROMPT = (
    "你是校园答疑草案撰写器。优先依据给定事实卡回答，不要调用自身常识补造政策；"
    "允许忠实、自然地改写和压缩证据，不要求逐字引用，也不要求每句都能在原文中找到。"
    "如果证据不完整、来源之间可能冲突或你不确定，请保留不确定性并将needs_review设为true。"
    "不要生成链接，链接由程序从卡片中附加。仅输出JSON对象："
    "answer为简洁中文，通常80至240字，最多300字；claims是可选的来源提示数组，"
    "每项可含text、card_ids、evidence_quotes，不能引用未提供的card_id。"
    "可额外给出confidence（0到1）和needs_review（布尔值）。"
)


def prompt_contract_sha256() -> str:
    payload = json.dumps(
        {
            "protocol": MODEL_PROTOCOL_VERSION,
            "planner": PLANNER_SYSTEM_PROMPT,
            "answer": ANSWER_SYSTEM_PROMPT,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def _hash_sources(paths: Iterable[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _intra_package_imports(path: Path, root: Path) -> set[Path]:
    """Modules inside this package that ``path`` imports.

    ``ast.walk`` also finds imports written inside function bodies, so a lazy
    import cannot hide a dependency from the build scope.
    """

    found: set[Path] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package_parts = list(path.relative_to(root).parts[:-1])
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.level:
            continue
        base = list(package_parts)
        for _ in range(node.level - 1):
            if base:
                base.pop()
        parts = base + (node.module.split(".") if node.module else [])
        target = root.joinpath(*parts) if parts else root
        module = target.with_suffix(".py")
        if module.is_file():
            found.add(module)
            continue
        if (target / "__init__.py").is_file():
            found.add(target / "__init__.py")
            for alias in node.names:
                submodule = target / f"{alias.name}.py"
                if submodule.is_file():
                    found.add(submodule)
    return found


def build_scoped_modules() -> set[Path]:
    """Every module reachable from the database builder.

    Derived from the import graph rather than a hand-written list, so adding a
    dependency to the builder cannot silently narrow what the build hash
    covers.  ``test_build_scope_is_derived_from_the_import_graph`` pins the
    resulting set so any change is a deliberate one.
    """

    root = _package_root()
    entry = root / "pipeline" / "build.py"
    reached = {entry}
    pending = [entry]
    while pending:
        for module in _intra_package_imports(pending.pop(), root):
            if module not in reached:
                reached.add(module)
                pending.append(module)
    return reached


def build_code_sha256() -> str:
    """Hash of the code that determines the built artifact's contents."""

    root = _package_root()
    return _hash_sources(build_scoped_modules(), root)


def runtime_code_sha256() -> str:
    """Hash of every module, including the query-time code the build never runs."""

    root = _package_root()
    return _hash_sources(root.rglob("*.py"), root)


@dataclass(slots=True)
class ModelEndpoints:
    base_url: str
    api_key: str
    planner_model: str
    embedding_model: str
    reranker_model: str
    answer_model: str
    reranker_url: str
    timeout: float


def release_build_config(
    endpoints: ModelEndpoints, *, embedding_dimension: int
) -> dict[str, Any]:
    """Configuration that actually determines the built database's contents.

    Deliberately excludes everything that only affects query time - planner,
    reranker and answer models, prompts, timeouts, and the query-time code.
    Folding those into the build identity meant that editing the QQ adapter
    invalidated a knowledge build, which forced a full re-embed of every card
    and changed ``knowledge_sha256``: a provenance chain that made the artifact
    churn is weaker, not stronger.  "The running code is the evaluated code"
    remains enforced separately, against the evaluation report.
    """

    endpoint_payload = json.dumps(
        {"base_url": endpoints.base_url.rstrip("/")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "embedding": endpoints.embedding_model,
        "embedding_dimension": embedding_dimension,
        "endpoint_sha256": hashlib.sha256(endpoint_payload).hexdigest(),
        "build_code_sha256": build_code_sha256(),
        "max_embedding_batch_size": MAX_EMBEDDING_BATCH_SIZE,
        "max_semantic_text_chars": MAX_SEMANTIC_TEXT_CHARS,
    }


def release_model_config(
    endpoints: ModelEndpoints,
    *,
    embedding_dimension: int,
    rerank_min_score: float,
) -> dict[str, Any]:
    endpoint_payload = json.dumps(
        {
            "base_url": endpoints.base_url.rstrip("/"),
            "reranker_url": endpoints.reranker_url,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "planner": endpoints.planner_model,
        "embedding": endpoints.embedding_model,
        "embedding_dimension": embedding_dimension,
        "reranker": endpoints.reranker_model,
        "rerank_min_score": rerank_min_score,
        "answer": endpoints.answer_model,
        "request_timeout_seconds": endpoints.timeout,
        "endpoint_sha256": hashlib.sha256(endpoint_payload).hexdigest(),
        "prompt_contract_sha256": prompt_contract_sha256(),
        "runtime_code_sha256": runtime_code_sha256(),
        "model_protocol_version": MODEL_PROTOCOL_VERSION,
        "max_model_response_bytes": MAX_MODEL_RESPONSE_BYTES,
        "max_embedding_batch_size": MAX_EMBEDDING_BATCH_SIZE,
        "max_semantic_text_chars": MAX_SEMANTIC_TEXT_CHARS,
    }


class RemoteModels:
    """Strict clients for OpenAI-compatible chat/embedding and rerank endpoints."""

    def __init__(
        self,
        endpoints: ModelEndpoints,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy = ONLINE_RETRY_POLICY,
    ) -> None:
        self.endpoints = endpoints
        self.retry_policy = retry_policy
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=endpoints.timeout)
        self.headers = {"Authorization": f"Bearer {endpoints.api_key}"}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _post(self, component: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        policy = self.retry_policy
        for attempt in range(policy.max_retries + 1):
            try:
                async with self.client.stream(
                    "POST", url, headers=self.headers, json=payload
                ) as response:
                    if (
                        response.status_code in TRANSIENT_STATUS_CODES
                        and attempt < policy.max_retries
                    ):
                        await asyncio.sleep(
                            policy.delay(
                                attempt, response.headers.get("retry-after", "").strip()
                            )
                        )
                        continue
                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > MAX_MODEL_RESPONSE_BYTES:
                        raise ValueError("model response exceeds byte limit")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_MODEL_RESPONSE_BYTES:
                            raise ValueError("model response exceeds byte limit")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("response is not an object")
                return data
            except httpx.TransportError as exc:
                # A timeout or dropped connection is at least as transient as a
                # 503, but used to get zero retries while a 503 got five.  Both
                # now share one budget.  Protocol failures (4xx, oversized or
                # malformed bodies) still fail immediately: retrying cannot help.
                last_error = exc
                if attempt < policy.max_retries:
                    await asyncio.sleep(policy.delay(attempt))
                    continue
                break
            except Exception as exc:
                last_error = exc
                break
        raise RetrievalUnavailable(component, str(last_error or "request failed")) from last_error

    async def _chat_json(self, component: str, model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        data = await self._post(
            component,
            f"{self.endpoints.base_url}/chat/completions",
            {
                "model": model,
                "messages": messages,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                # The DLUT gateway's Qwen3.5 models otherwise prepend a
                # ``Thinking Process:`` block even in JSON mode.  The
                # planner/answer contract is deliberately strict JSON, so
                # disable visible reasoning at the chat-template layer.
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        try:
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("chat JSON is not an object")
            return parsed
        except Exception as exc:
            raise RetrievalUnavailable(component, f"malformed model response: {exc}") from exc

    async def plan(self, question: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        context = json.dumps(history or [], ensure_ascii=False)
        return await self._chat_json(
            "planner",
            self.endpoints.planner_model,
            [
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": f"历史：{context}\n当前问题：{question}"},
            ],
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > MAX_EMBEDDING_BATCH_SIZE:
            raise RetrievalUnavailable(
                "embedding", f"embedding batch exceeds {MAX_EMBEDDING_BATCH_SIZE} texts"
            )
        if any(len(text) > MAX_SEMANTIC_TEXT_CHARS for text in texts):
            raise RetrievalUnavailable("embedding", "embedding text exceeds character limit")
        data = await self._post(
            "embedding",
            f"{self.endpoints.base_url}/embeddings",
            {"model": self.endpoints.embedding_model, "input": texts},
        )
        try:
            rows = sorted(data["data"], key=lambda item: int(item["index"]))
            indexes = [int(item["index"]) for item in rows]
            if indexes != list(range(len(texts))):
                raise ValueError("embedding indexes are missing or duplicated")
            vectors = [[float(value) for value in item["embedding"]] for item in rows]
            if len(vectors) != len(texts):
                raise ValueError("embedding count mismatch")
            return vectors
        except Exception as exc:
            raise RetrievalUnavailable("embedding", f"malformed response: {exc}") from exc

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        if not documents:
            return []
        data = await self._post(
            "reranker",
            self.endpoints.reranker_url,
            {
                "model": self.endpoints.reranker_model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            },
        )
        try:
            results = [
                (int(item["index"]), float(item.get("relevance_score", item.get("score"))))
                for item in data["results"]
            ]
            indexes = {index for index, _ in results}
            if indexes != set(range(len(documents))):
                raise ValueError("reranker did not return every input document")
            return sorted(results, key=lambda item: item[1], reverse=True)
        except Exception as exc:
            raise RetrievalUnavailable("reranker", f"malformed response: {exc}") from exc

    async def select_evidence(self, question: str, candidates: list[str]) -> dict[str, Any]:
        """Ask the answer model which candidates actually answer the question.

        This replaces the dedicated reranker, which was measured blind on this
        knowledge base: it scored every candidate between 0.85 and 0.96 and
        ranked 毕业生集体户口迁出 first for both a club-registration and a
        tuition-refund question.  Candidates are numbered rather than named so
        the model returns an ordinal it cannot invent, and the whole pool fits
        in one call - about 4,700 characters for fifty cards.
        """

        if not candidates:
            return {"picked": []}
        listing = "\n".join(f"{number}. {text}" for number, text in enumerate(candidates, 1))
        return await self._chat_json(
            "selector",
            self.endpoints.answer_model,
            [
                {"role": "system", "content": SELECTOR_SYSTEM_PROMPT},
                {"role": "user", "content": f"用户问题：{question}\n\n候选卡片：\n{listing}"},
            ],
        )

    async def draft_answer(self, question: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._chat_json(
            "answer_model",
            self.endpoints.answer_model,
            [
                {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "evidence": evidence}, ensure_ascii=False
                    ),
                },
            ],
        )

    async def healthcheck(self, embedding_dimension: int) -> None:
        vectors = await self.embed(["健康检查"])
        if (
            len(vectors) != 1
            or len(vectors[0]) != embedding_dimension
            or any(not math.isfinite(value) for value in vectors[0])
            or not any(value != 0 for value in vectors[0])
        ):
            raise RetrievalUnavailable(
                "embedding", f"health dimension {len(vectors[0]) if vectors else 0} != {embedding_dimension}"
            )
        ranked = await self.rerank("健康", ["健康检查", "无关文本"])
        if len(ranked) != 2 or any(
            not math.isfinite(score) or not 0 <= score <= 1 for _, score in ranked
        ):
            raise RetrievalUnavailable("reranker", "health probe returned incomplete ranking")

        planner_question = "本科生在哪里查看校园事项办理说明？"
        raw_plan = await self.plan(planner_question)
        try:
            from .retrieval import QueryPlan

            QueryPlan.from_dict(raw_plan, planner_question)
        except RetrievalUnavailable:
            raise
        except Exception as exc:
            raise RetrievalUnavailable(
                "planner", f"health probe violated planner contract: {exc}"
            ) from exc

        probe_text = (
            "本次健康检查仅用于确认回答模型能依据提供的证据生成草案，不代表任何校园政策、"
            "办理规则、申请条件、所需材料、办理时间、办理地点、费用标准、联系方式或资格结论。"
        )
        draft = await self.draft_answer(
            "请根据健康检查证据写一句简短草案。",
            [
                {
                    "card_id": "health-probe-card",
                    "title": "模型健康检查",
                    "summary": "",
                    "facts": {},
                    "evidence_quote": probe_text,
                    "source_locator": "健康检查",
                    "facets": [],
                    "validity": "current",
                    "card_kind": "fact",
                }
            ],
        )
        try:
            if not isinstance(draft.get("answer"), str) or not draft["answer"].strip():
                raise ValueError("answer model returned an empty health draft")
            if "http://" in draft["answer"] or "https://" in draft["answer"]:
                raise ValueError("health draft contains a generated URL")
            claims = draft.get("claims", [])
            if claims and not isinstance(claims, list):
                raise ValueError("health claims are not an array")
            for claim in claims:
                if not isinstance(claim, dict):
                    raise ValueError("health claim is not an object")
                if any(str(card_id) != "health-probe-card" for card_id in claim.get("card_ids", [])):
                    raise ValueError("health claim cites an unknown card")
        except Exception as exc:
            raise RetrievalUnavailable(
                "answer_model", f"health probe violated answer contract: {exc}"
            ) from exc

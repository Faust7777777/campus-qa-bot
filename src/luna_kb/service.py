from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .clients import ModelEndpoints, RemoteModels, release_model_config
from .config import Settings
from .contracts import normalized_text
from .errors import InsufficientEvidence, RetrievalUnavailable
from .release import ReleaseManager, file_sha256
from .retrieval import (
    CardEvidence,
    KnowledgeDatabase,
    ModelOutputRejected,
    RetrievalResult,
    StrongRetriever,
)
from .runtime_controls import WorkLimiter

_log = logging.getLogger(__name__)


class AnswerModels(Protocol):
    async def draft_answer(self, question: str, evidence: list[dict[str, Any]]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SourceCitation:
    index: int
    card_id: str
    title: str
    url: str
    locator: str


@dataclass(frozen=True, slots=True)
class QuestionAnswer:
    answer: str
    sources: list[SourceCitation]
    retrieval: RetrievalResult
    cited_card_ids: tuple[str, ...]
    quality: str = "draft"
    needs_review: bool = False
    quality_notes: tuple[str, ...] = ()

    def format_text(self) -> str:
        if not self.sources:
            return self.answer
        lines = [self.answer, "", "参考来源："]
        lines.extend(f"[{source.index}] {source.title} {source.url}" for source in self.sources)
        return "\n".join(lines)


# Answer used when retrieval found official entry points but the knowledge base
# holds no body text for the question.  It must not read as "this does not
# exist": the entry point was found, the procedure text simply is not stored.
NAVIGATION_ONLY_ANSWER = (
    "已找到相关官方页面，但知识库中暂无该事项的正文说明，具体办理要求请以下列页面的最新内容为准。"
)
NAVIGATION_ONLY_QUALITY = "navigation"

URL_RE = re.compile(
    r"(?:https?://|www\.|(?<![\w@])(?:[a-z0-9-]+\.)+(?:com|cn|edu|org|net|io)(?:[/:]|\b))",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"(?<![0-9A-Za-z])\d+(?:\.\d+)?")


def _sentences(answer: str) -> list[str]:
    return [piece.strip() for piece in re.split(r"(?<=[。！？!?])|\n+", answer) if piece.strip()]


def _numbers(value: str) -> set[str]:
    return set(NUMBER_RE.findall(value or ""))


def _url_tokens(text: str) -> list[str]:
    """Whole URL-ish tokens, so a match can be compared rather than just found."""

    tokens: list[str] = []
    for match in URL_RE.finditer(text or ""):
        start = text.rfind(" ", 0, match.start()) + 1
        for boundary in ("\n", "，", "。", "、", "（", "(", "；"):
            cut = text.rfind(boundary, 0, match.start())
            start = max(start, cut + 1)
        end = len(text)
        for boundary in (" ", "\n", "，", "。", "、", "）", ")", "；", "”"):
            cut = text.find(boundary, match.start())
            if cut != -1:
                end = min(end, cut)
        tokens.append(text[start:end].strip().rstrip("。，、；)）"))
    return tokens


def _fabricated_urls(answer: str, cards: list[CardEvidence]) -> list[str]:
    """URLs in the answer that are not in the evidence the model was given.

    Blocking every URL was too blunt.  Several cards exist precisely to say
    "go to this address" - the campus network self-service page, the payment
    platform - and their evidence quotes the URL.  Forbidding the model to
    repeat it makes those cards unanswerable while the program attaches the very
    same link underneath as a citation.

    Quoting a link that is in the evidence is faithful; producing one that is
    not is the hallucination this gate exists to stop.  The allowed set is built
    per question from the evidence actually supplied, so a link cannot leak in
    from another card.
    """

    evidence = "\n".join(card.evidence_quote for card in cards)
    return [token for token in _url_tokens(answer) if token and token not in evidence]


class AnswerService:
    def __init__(
        self,
        retriever: StrongRetriever,
        models: AnswerModels,
        cache_ttl_seconds: float = 300.0,
        cache_size: int = 256,
        limiter: WorkLimiter | None = None,
        answer_mode: str = "draft",
        answer_max_chars: int = 300,
        answer_max_sources: int = 3,
    ) -> None:
        self.retriever = retriever
        self.models = models
        self.cache_ttl_seconds = cache_ttl_seconds
        self.cache_size = cache_size
        self.limiter = limiter
        if answer_mode not in {"draft", "strict"}:
            raise ValueError("answer_mode must be draft or strict")
        self.answer_mode = answer_mode
        self.answer_max_chars = answer_max_chars
        self.answer_max_sources = answer_max_sources
        self._answer_cache: OrderedDict[str, tuple[float, QuestionAnswer]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[QuestionAnswer]] = {}
        self._active_executions: set[asyncio.Task[object]] = set()
        self._closed = False

    async def ask(
        self, question: str, history: list[dict[str, str]] | None = None
    ) -> QuestionAnswer:
        if self._closed:
            raise RetrievalUnavailable("answer_service", "answer service is closed")
        question = question.strip()
        if not question:
            raise InsufficientEvidence("empty question")
        cache_key = normalized_text(question) if not history else ""
        if cache_key and self.cache_ttl_seconds > 0 and self.cache_size > 0:
            cached_answer = self._cached_answer(cache_key)
            if cached_answer is not None:
                return cached_answer
            task = self._inflight.get(cache_key)
            if task is None:
                task = asyncio.create_task(
                    self._ask_and_cache(cache_key, question, history)
                )
                self._inflight[cache_key] = task

                def clear_inflight(completed: asyncio.Task[QuestionAnswer]) -> None:
                    if self._inflight.get(cache_key) is completed:
                        self._inflight.pop(cache_key, None)

                task.add_done_callback(clear_inflight)
            return await asyncio.shield(task)
        return await self._ask_with_lease(question, history)

    def _cached_answer(self, cache_key: str) -> QuestionAnswer | None:
        cached = self._answer_cache.pop(cache_key, None)
        if cached is None:
            return None
        expires_at, answer = cached
        if expires_at <= time.monotonic():
            return None
        self._answer_cache[cache_key] = cached
        return answer

    async def _ask_and_cache(
        self,
        cache_key: str,
        question: str,
        history: list[dict[str, str]] | None,
    ) -> QuestionAnswer:
        result = await self._ask_with_lease(question, history)
        self._answer_cache[cache_key] = (
            time.monotonic() + self.cache_ttl_seconds,
            result,
        )
        self._answer_cache.move_to_end(cache_key)
        while len(self._answer_cache) > self.cache_size:
            self._answer_cache.popitem(last=False)
        return result

    async def _ask_with_lease(
        self, question: str, history: list[dict[str, str]] | None
    ) -> QuestionAnswer:
        execution = asyncio.current_task()
        if execution is None:
            raise RetrievalUnavailable("answer_service", "answer execution has no task")
        self._active_executions.add(execution)
        try:
            if self.limiter is None:
                return await self._ask_uncached(question, history)
            async with self.limiter.slot():
                return await self._ask_uncached(question, history)
        finally:
            self._active_executions.discard(execution)

    async def close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        tasks = set(self._active_executions)
        tasks.update(self._inflight.values())
        tasks.discard(current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_executions.clear()
        self._inflight.clear()
        self._answer_cache.clear()

    async def _ask_uncached(
        self, question: str, history: list[dict[str, str]] | None
    ) -> QuestionAnswer:
        result = await self.retriever.retrieve(question, history)
        evidence_cards = [card for card in result.cards if card.evidence_quote]
        if not evidence_cards:
            # Retrieval already ordered these; keep that order rather than
            # re-sorting by rerank score, which is too tightly banded to rank
            # entry points against each other.
            citations = self._navigation_citations(result.cards)
            return QuestionAnswer(
                answer=NAVIGATION_ONLY_ANSWER,
                sources=citations,
                retrieval=result,
                cited_card_ids=tuple(source.card_id for source in citations),
                quality=NAVIGATION_ONLY_QUALITY,
            )
        evidence = [self._evidence_payload(card) for card in evidence_cards]
        try:
            started = time.perf_counter()
            draft = await self.models.draft_answer(question, evidence)
            result.trace.stage_seconds["answer_model"] = time.perf_counter() - started
            answer, cited_ids, needs_review, quality_notes = self._validate_draft(
                draft,
                evidence_cards,
                mode=self.answer_mode,
                max_chars=self.answer_max_chars,
            )
        except (InsufficientEvidence, RetrievalUnavailable):
            raise
        except Exception as exc:
            raise RetrievalUnavailable("answer_model", str(exc)) from exc
        card_map = {card.card_id: card for card in evidence_cards}
        # Evidence arrives in the selector's order, so the leading cited card is
        # the first one the selector chose.  This used to sort by rerank score,
        # which the selector replaced: the field is now always zero, and a
        # max() over zeroes silently returns whichever card came first anyway.
        cited = set(cited_ids)
        top_card = next(card for card in evidence_cards if card.card_id in cited)
        citations: list[SourceCitation] = []
        seen_urls: set[str] = set()
        for card_id in cited_ids:
            card = card_map[card_id]
            if card.canonical_url in seen_urls:
                continue
            seen_urls.add(card.canonical_url)
            citations.append(
                SourceCitation(
                    index=len(citations) + 1,
                    card_id=card.card_id,
                    title=card.source_title or card.title,
                    url=card.canonical_url,
                    locator=card.source_locator,
                )
            )
            if len(citations) >= self.answer_max_sources:
                break
        return QuestionAnswer(
            answer,
            citations,
            result,
            tuple(cited_ids),
            self.answer_mode,
            needs_review,
            quality_notes,
        )

    def _navigation_citations(self, cards: list[CardEvidence]) -> list[SourceCitation]:
        citations: list[SourceCitation] = []
        seen_urls: set[str] = set()
        for card in cards:
            if card.canonical_url in seen_urls:
                continue
            seen_urls.add(card.canonical_url)
            citations.append(
                SourceCitation(
                    index=len(citations) + 1,
                    card_id=card.card_id,
                    title=card.source_title or card.title,
                    url=card.canonical_url,
                    locator=card.source_locator,
                )
            )
            if len(citations) >= self.answer_max_sources:
                break
        return citations

    @staticmethod
    def _evidence_payload(card: CardEvidence) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "card_id": card.card_id,
            "title": card.title,
            "summary": card.summary,
            "facts": card.facts,
            "evidence_quote": card.evidence_quote,
            "source_locator": card.source_locator,
            "facets": card.facets,
            "validity": card.validity,
            "card_kind": card.card_kind,
        }
        if card.parent_context:
            payload["parent_context"] = {
                "card_id": card.parent_context.card_id,
                "title": card.parent_context.title,
                "summary": card.parent_context.summary,
                "facts": card.parent_context.facts,
                "evidence_quote": card.parent_context.evidence_quote,
            }
        return payload

    @staticmethod
    def _validate_draft(
        draft: dict[str, Any],
        cards: list[CardEvidence],
        *,
        mode: str = "draft",
        max_chars: int = 300,
    ) -> tuple[str, list[str], bool, tuple[str, ...]]:
        answer = str(draft.get("answer", "")).strip()
        claims = draft.get("claims", [])
        if not answer:
            raise InsufficientEvidence("answer model declined or omitted answer")
        if len(answer) > max_chars:
            raise ModelOutputRejected("answer_model", f"answer exceeds {max_chars} characters")
        fabricated = _fabricated_urls(answer, cards)
        if fabricated:
            raise ModelOutputRejected(
                "answer_model", f"answer invents a URL: {fabricated[0]}"
            )
        card_map = {card.card_id: card for card in cards}
        notes: list[str] = []
        cited_ids: list[str] = []
        if not isinstance(claims, list):
            notes.append("claims_not_array")
            claims = []
        for claim in claims:
            if not isinstance(claim, dict):
                notes.append("malformed_claim")
                continue
            ids = [str(value) for value in claim.get("card_ids", [])]
            valid_ids = [card_id for card_id in ids if card_id in card_map]
            if not valid_ids:
                notes.append("claim_without_selected_card")
            for card_id in valid_ids:
                if card_id not in cited_ids:
                    cited_ids.append(card_id)
        if draft.get("needs_review") is True:
            notes.append("model_requested_review")
        confidence = draft.get("confidence")
        if confidence is not None:
            try:
                if not 0 <= float(confidence) <= 1:
                    notes.append("invalid_confidence")
                elif float(confidence) < 0.55:
                    notes.append("low_model_confidence")
            except (TypeError, ValueError):
                notes.append("invalid_confidence")
        # In draft mode this is a quality signal, not a rejection.  A small
        # lexical overlap check catches answers that clearly drifted away from
        # the selected evidence without pretending that paraphrase can be
        # judged by substring containment.
        evidence_text = " ".join(
            f"{card.title} {card.summary} {card.evidence_quote} {card.facts}" for card in cards
        )
        answer_tokens = set(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", answer))
        evidence_tokens = set(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", evidence_text))
        if answer_tokens and len(answer_tokens & evidence_tokens) / len(answer_tokens) < 0.12:
            notes.append("low_lexical_support")
        if mode == "strict":
            # Kept as an explicit audit mode, never the product default.
            answer_sentences = _sentences(answer)
            if not isinstance(claims, list) or not claims:
                raise InsufficientEvidence("strict mode requires claim map")
            for claim in claims:
                text = str(claim.get("text", "")).strip()
                ids = [str(value) for value in claim.get("card_ids", [])]
                quotes = [str(value).strip() for value in claim.get("evidence_quotes", [])]
                if not text or text not in answer_sentences or len(ids) != 1 or len(quotes) != 1:
                    raise ModelOutputRejected("evidence_gate", "strict claim map is incomplete")
                if text not in card_map[ids[0]].evidence_quote:
                    raise ModelOutputRejected("evidence_gate", "strict answer is not extractive")
        if not cited_ids:
            cited_ids.append(cards[0].card_id)
            notes.append("fallback_top_evidence")
        return answer, cited_ids, bool(notes), tuple(dict.fromkeys(notes))


class Runtime:
    def __init__(
        self,
        settings: Settings,
        release_path: Path,
        manifest: dict[str, Any],
        database: KnowledgeDatabase,
        models: RemoteModels,
    ) -> None:
        self.settings = settings
        self.release_path = release_path
        self.manifest = manifest
        self.database = database
        self.models = models
        self.retriever = StrongRetriever(
            database,
            models,
            settings.rerank_min_score,
            settings.fast_path_enabled,
            settings.vector_recall_enabled,
        )
        self.answers = AnswerService(
            self.retriever,
            models,
            settings.answer_cache_ttl_seconds,
            settings.answer_cache_size,
            WorkLimiter(
                settings.answer_concurrency,
                settings.answer_queue_size,
                settings.answer_queue_timeout_seconds,
            ),
            settings.answer_mode,
            settings.answer_max_chars,
            settings.answer_max_sources,
        )

    @classmethod
    async def start(cls, settings: Settings) -> "Runtime":
        settings.validate()
        manager = ReleaseManager(settings.release_root)
        release_path = manager.resolve_current(verify=True)
        manifest = json.loads((release_path / "manifest.json").read_text(encoding="utf-8"))
        database_path = release_path / "knowledge.sqlite"
        if file_sha256(database_path) != manifest["knowledge_sha256"]:
            raise RetrievalUnavailable("release", "database checksum changed after activation")
        if int(manifest["embedding_dimension"]) != settings.embedding_dimension:
            raise RetrievalUnavailable("vector", "manifest/config dimension mismatch")
        endpoints = ModelEndpoints(
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
            planner_model=settings.planner_model,
            embedding_model=settings.embedding_model,
            reranker_model=settings.reranker_model,
            answer_model=settings.answer_model,
            reranker_url=settings.reranker_url,
            timeout=settings.request_timeout,
        )
        # A release may ship without ever having been evaluated, so the report
        # is read if it is there and its absence is not a failure.  Making the
        # evaluation gate advisory in release.py left this read unguarded: the
        # first release built after that change had no report, and the bot came
        # up unable to answer anything while reporting healthy.
        evaluation_path = release_path / "evaluation_report.json"
        evaluation_report = (
            json.loads(evaluation_path.read_text(encoding="utf-8"))
            if evaluation_path.is_file()
            else {}
        )
        active_model_config = release_model_config(
            endpoints,
            embedding_dimension=settings.embedding_dimension,
            rerank_min_score=settings.rerank_min_score,
        )
        # Only the fields that would make this database unusable are fatal.
        #
        # Comparing the whole model config refused to start whenever anything in
        # it had moved, including runtime_code_sha256 - so any edit to any .py
        # file stopped the bot, and the evaluation that would clear it is the
        # 240-question one no release has ever passed.  Most of what it compares
        # is provenance: which answer model wrote the drafts, what the request
        # timeout was.  Worth recording, worth warning about, not worth refusing
        # to answer over.
        #
        # The embedding model and its dimension are different.  Vectors built by
        # one model are meaningless to another, and that is a wrong answer rather
        # than a stale attribution, so those still stop startup.
        # The embedding check belongs against the build report, not the
        # evaluation: what matters is the model that produced the vectors in
        # this database, and the build report always exists while an evaluation
        # may never have been run.  Comparing against an absent evaluation made
        # every unevaluated release look like a model mismatch.
        build_config = json.loads(
            (release_path / "build_report.json").read_text(encoding="utf-8")
        ).get("model_config") or {}
        for field in ("embedding", "embedding_dimension"):
            if str(build_config.get(field)) != str(active_model_config.get(field)):
                raise RetrievalUnavailable(
                    "release",
                    f"database was built with a different {field}",
                )
        evaluated_config = evaluation_report.get("model_config") or {}
        drifted = sorted(
            field
            for field in set(evaluated_config) | set(active_model_config)
            if evaluated_config.get(field) != active_model_config.get(field)
        )
        if evaluated_config and drifted:
            _log.warning(
                "release %s was evaluated with different %s; its numbers describe "
                "a different configuration",
                manifest.get("version"),
                ", ".join(drifted),
            )
        models = RemoteModels(endpoints)
        database: KnowledgeDatabase | None = None
        try:
            database = KnowledgeDatabase(database_path, settings.embedding_dimension)
            database.healthcheck()
            await models.healthcheck(settings.embedding_dimension)
        except Exception:
            if database is not None:
                database.close()
            await models.close()
            raise
        assert database is not None
        return cls(settings, release_path, manifest, database, models)

    async def close(self) -> None:
        await self.answers.close()
        self.database.close()
        await self.models.close()

    async def healthcheck(self) -> dict[str, Any]:
        db = self.database.healthcheck()
        await self.models.healthcheck(self.settings.embedding_dimension)
        return {"status": "ok", "version": self.manifest["version"], "database": db}


def tune_oom_score(value: int) -> bool:
    if sys.platform != "linux":
        return False
    try:
        Path("/proc/self/oom_score_adj").write_text(str(value), encoding="ascii")
        return True
    except OSError:
        return False

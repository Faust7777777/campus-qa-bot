from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .candidate_allocation import RerankCandidateAllocator
from .contracts import normalized_text
from .errors import ContractError, InsufficientEvidence, RetrievalUnavailable
from .pipeline.build import SCHEMA_VERSION, char_trigrams, lexical_tokens
from .scope_policy import matches_query_scope, parent_scope_covers_child
from .vector import load_sqlite_vec, serialize_float32


class RetrievalModels(Protocol):
    async def plan(self, question: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]: ...
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]: ...


@dataclass(slots=True)
class QueryFilters:
    campus: str = ""
    audience: str = ""
    time_scope: str = "current"


CAMPUS_ALIASES = {
    "": "",
    "全校": "",
    "凌水": "凌水",
    "凌水校区": "凌水",
    "开发区": "开发区",
    "开发区校区": "开发区",
    "盘锦": "盘锦",
    "盘锦校区": "盘锦",
}
UNDERGRADUATE_AUDIENCE = "本科生"
CAMPUS_SCOPE_VALUES = ("凌水", "开发区", "盘锦")
FACET_ALIASES = {
    "资格": ("资格", "适用对象", "对象", "条件", "eligibility", "eligible"),
    "申请": ("申请", "申报", "application", "apply"),
    "材料": ("材料", "证件", "material", "materials", "document"),
    "流程": ("流程", "步骤", "办理", "process", "workflow", "procedure"),
    "时间": ("时间", "时段", "hours", "time", "schedule"),
    "期限": ("截止", "期限", "deadline", "due"),
    "地点": ("地点", "地址", "位置", "location", "place"),
    "联系": ("联系", "咨询", "电话", "contact", "phone", "telephone"),
    "入口": ("入口", "系统", "平台", "portal", "system", "entry"),
    "培训": ("培训", "training"),
    "费用": ("费用", "金额", "收费", "学费", "缴费", "fee", "cost", "tuition", "payment"),
    "工作量": ("工时", "工作时长", "工作量", "workload", "workinghours"),
    "规则": ("规则", "原则", "要求", "rule", "principle", "policy"),
    "审批": ("审批", "审核", "approval", "review"),
    "结果": ("结果", "result", "outcome"),
    "课程": ("课程", "course"),
    "考试": ("考试", "测试", "exam", "test"),
    "成绩": ("成绩", "grade", "score"),
    "账号": ("账号", "account"),
    "密码": ("密码", "password"),
    "设备": ("设备", "绑定", "device", "binding"),
    "报修": ("报修", "故障", "repair", "fault"),
    "医保": ("医保", "insurance"),
    "安全": ("安全", "safety"),
    "退费": ("退费", "退款", "refund"),
    "住宿": ("住宿", "宿舍", "housing", "dormitory"),
}


def canonical_facets(values: list[str]) -> set[str]:
    concepts: set[str] = set()
    for value in values:
        compact = normalized_text(value)
        if not compact:
            continue
        matched_concepts: set[str] = set()
        for concept, aliases in FACET_ALIASES.items():
            if any(normalized_text(alias) in compact for alias in aliases):
                matched_concepts.add(concept)
        if "申请" in matched_concepts and compact not in {
            "申请",
            "申报",
            "application",
            "apply",
        }:
            matched_concepts.remove("申请")
        if (
            "流程" in matched_concepts
            and "流程" not in compact
            and "步骤" not in compact
            and not any(word in compact for word in ("process", "workflow", "procedure"))
        ):
            matched_concepts.remove("流程")
        if matched_concepts:
            concepts.update(matched_concepts)
        else:
            concepts.add(compact)
    return concepts


def missing_required_facets(required: list[str], cards: list["CardEvidence"]) -> list[str]:
    # Required answer facets are an evidence-coverage gate, not a retrieval
    # metadata gate. Luna-authored titles, summaries, facets and retrieval
    # expansions may find a candidate but cannot prove that it contains the
    # requested material, deadline, location, etc.
    evidence_texts = [card.evidence_quote for card in cards if card.evidence_quote]
    covered = canonical_facets(evidence_texts)
    retrieval_text = normalized_text("\n".join(evidence_texts))
    missing: list[str] = []
    for raw in required:
        concepts = canonical_facets([raw])
        if not concepts:
            continue
        if not all(
            concept in covered or normalized_text(concept) in retrieval_text
            for concept in concepts
        ):
            missing.append(raw)
    return missing


@dataclass(slots=True)
class QueryPlan:
    intent: str
    standalone_query: str
    subqueries: list[str]
    entities: list[str]
    required_facets: list[str]
    filters: QueryFilters

    @classmethod
    def from_dict(cls, data: dict[str, Any], original_question: str) -> "QueryPlan":
        try:
            intent = str(data["intent"])
            standalone = str(data["standalone_query"]).strip()
            subqueries = [str(value).strip() for value in data["subqueries"] if str(value).strip()]
            entities = [str(value).strip() for value in data["entities"] if str(value).strip()]
            facets = [str(value).strip() for value in data["required_facets"] if str(value).strip()]
            raw_filters = data["filters"]
            raw_campus = str(raw_filters.get("campus", "")).strip()
            if raw_campus not in CAMPUS_ALIASES:
                raise ValueError(f"invalid campus: {raw_campus}")
            filters = QueryFilters(
                campus=CAMPUS_ALIASES[raw_campus],
                audience=UNDERGRADUATE_AUDIENCE,
                time_scope=str(raw_filters.get("time_scope", "current")),
            )
        except Exception as exc:
            raise RetrievalUnavailable("planner", f"invalid query plan: {exc}") from exc
        if intent not in {"fact", "procedure", "historical", "out_of_scope"}:
            raise RetrievalUnavailable("planner", f"invalid intent: {intent}")
        if not standalone:
            standalone = original_question.strip()
        if not subqueries:
            subqueries = [standalone]
        if len(subqueries) > 3:
            raise RetrievalUnavailable("planner", "more than 3 subqueries")
        if len(standalone) > 1000 or any(len(value) > 1000 for value in subqueries):
            raise RetrievalUnavailable("planner", "query plan text is too long")
        if len(entities) > 20 or any(len(value) > 200 for value in entities):
            raise RetrievalUnavailable("planner", "query plan has too many or oversized entities")
        if len(facets) > 12 or any(len(value) > 100 for value in facets):
            raise RetrievalUnavailable("planner", "query plan has too many or oversized facets")
        if filters.time_scope not in {"current", "historical"}:
            raise RetrievalUnavailable("planner", "invalid time_scope")
        if intent == "historical":
            filters.time_scope = "historical"
        return cls(intent, standalone, subqueries, entities, facets, filters)


@dataclass(slots=True)
class CardEvidence:
    card_id: str
    source_id: str
    parent_card_id: str | None
    title: str
    summary: str
    evidence_quote: str
    source_locator: str
    facts: dict[str, Any]
    facets: list[str]
    campus: str
    audience: str
    validity: str
    card_kind: str
    subject_key: str
    fact_key: str
    retrieval_text: str
    canonical_url: str
    source_title: str
    published_at: str | None
    rerank_score: float = 0.0
    # 1-based position in the fused first-stage pool; 0 means "never recalled",
    # which scores worse than any recalled card.
    first_stage_rank: int = 0
    parent_context: "CardEvidence | None" = None


@dataclass(slots=True)
class RetrievalTrace:
    channel_ids: dict[str, list[str]] = field(default_factory=dict)
    first_stage_ids: list[str] = field(default_factory=list)
    fused_ids: list[str] = field(default_factory=list)
    reranked_ids: list[str] = field(default_factory=list)
    # Order the selector actually used: first-stage rank fused with reranker
    # rank.  ``reranked_ids`` keeps its original meaning (pure reranker order)
    # so existing recall metrics still measure the reranker itself.
    selection_ids: list[str] = field(default_factory=list)
    selected_ids: list[str] = field(default_factory=list)
    # Wall time per remote stage, so a slow answer can be attributed to the
    # stage that caused it instead of being reported as one opaque total.
    stage_seconds: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievalResult:
    plan: QueryPlan
    cards: list[CardEvidence]
    trace: RetrievalTrace


class KnowledgeDatabase:
    def __init__(self, path: Path, expected_dimension: int) -> None:
        self.path = path.resolve()
        self.expected_dimension = expected_dimension
        self._lock = threading.RLock()
        self._channel_connections: dict[str, sqlite3.Connection] = {}
        self._channel_locks = {
            "exact": threading.RLock(),
            "bm25": threading.RLock(),
            "trigram": threading.RLock(),
        }
        self._closed = False
        try:
            self._uri = f"{self.path.as_uri()}?mode=ro&immutable=1"
            self.connection = self._open_connection(load_vector=True)
            stored_schema = int(self.metadata("schema_version"))
            if stored_schema != SCHEMA_VERSION:
                raise RetrievalUnavailable(
                    "sqlite",
                    f"database schema {stored_schema} != runtime schema {SCHEMA_VERSION}",
                )
            stored = int(self.metadata("embedding_dimension"))
            if stored != expected_dimension:
                raise RetrievalUnavailable(
                    "vector", f"database dimension {stored} != configured {expected_dimension}"
                )
            self._channel_connections = {
                channel: self._open_connection(load_vector=False)
                for channel in self._channel_locks
            }
        except RetrievalUnavailable:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise RetrievalUnavailable("sqlite", str(exc)) from exc

    def _open_connection(self, *, load_vector: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(self._uri, uri=True, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-4096")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA mmap_size=67108864")
        if load_vector:
            load_sqlite_vec(connection)
        return connection

    def _ensure_open(self) -> None:
        if self._closed:
            raise RetrievalUnavailable("sqlite", "database is closed")

    def close(self) -> None:
        # Shutdown runs on the event-loop thread while recall work runs in the
        # default thread pool. Taking every connection lock prevents SQLite
        # connections from being closed underneath an in-flight query.
        with (
            self._channel_locks["exact"],
            self._channel_locks["bm25"],
            self._channel_locks["trigram"],
            self._lock,
        ):
            if self._closed:
                return
            self._closed = True
            for connection in self._channel_connections.values():
                connection.close()
            self._channel_connections.clear()
            connection = getattr(self, "connection", None)
            if connection is not None:
                connection.close()

    def metadata(self, key: str) -> str:
        with self._lock:
            self._ensure_open()
            row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row is None:
            raise RetrievalUnavailable("sqlite", f"missing metadata: {key}")
        return str(row[0])

    @staticmethod
    def _matching_campus_scopes(campus: str) -> list[str]:
        if not campus:
            return []
        scopes = ["", "全校"]
        for mask in range(1, 1 << len(CAMPUS_SCOPE_VALUES)):
            values = [
                value
                for index, value in enumerate(CAMPUS_SCOPE_VALUES)
                if mask & (1 << index)
            ]
            if campus in values:
                scopes.append("|".join(values))
        return scopes

    @staticmethod
    def _filter_sql(plan: QueryPlan, alias: str = "c") -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if plan.filters.time_scope == "historical":
            clauses.append(f"{alias}.validity='historical'")
        else:
            clauses.append(f"{alias}.validity!='historical'")
        if plan.filters.campus:
            scopes = KnowledgeDatabase._matching_campus_scopes(plan.filters.campus)
            placeholders = ",".join("?" for _ in scopes)
            clauses.append(f"{alias}.campus IN ({placeholders})")
            params.extend(scopes)
        if plan.filters.audience:
            clauses.append(f"({alias}.audience='' OR {alias}.audience=?)")
            params.append(plan.filters.audience)
        return " AND ".join(clauses), params

    def exact(self, queries: list[str], plan: QueryPlan, limit: int = 10) -> list[str]:
        terms = list(dict.fromkeys(normalized_text(query) for query in queries if normalized_text(query)))
        if not terms:
            return []
        placeholders = ",".join("?" for _ in terms)
        filters, params = self._filter_sql(plan)
        sql = f"""
            SELECT e.card_id, MIN(CASE e.term_type WHEN 'title' THEN 0 WHEN 'question' THEN 1 ELSE 2 END) AS rank
            FROM exact_terms e JOIN cards c ON c.card_id=e.card_id
            WHERE e.term IN ({placeholders}) AND {filters}
            GROUP BY e.card_id ORDER BY rank, e.card_id LIMIT ?
        """
        connection = self._channel_connections.get("exact", self.connection)
        lock = self._channel_locks.get("exact", self._lock)
        with lock:
            self._ensure_open()
            rows = connection.execute(sql, [*terms, *params, limit]).fetchall()
        return [str(row[0]) for row in rows]

    def bm25(self, queries: list[str], plan: QueryPlan, limit: int = 40) -> list[str]:
        best: dict[str, tuple[int, float]] = {}
        filters, filter_params = self._filter_sql(plan)
        for query_index, query in enumerate(queries):
            tokens = list(dict.fromkeys(lexical_tokens(query).split()))
            if not tokens:
                continue
            match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            sql = f"""
                SELECT f.card_id, bm25(card_fts,0.0,12.0,10.0,8.0,5.0,3.0,6.0,1.0) AS score
                FROM card_fts f JOIN cards c ON c.card_id=f.card_id
                WHERE card_fts MATCH ? AND {filters}
                ORDER BY score LIMIT ?
            """
            connection = self._channel_connections.get("bm25", self.connection)
            lock = self._channel_locks.get("bm25", self._lock)
            with lock:
                self._ensure_open()
                rows = connection.execute(sql, [match, *filter_params, limit]).fetchall()
            for rank, row in enumerate(rows):
                key = str(row[0])
                value = (rank, float(row[1]))
                if key not in best or value < best[key]:
                    best[key] = value
        return [key for key, _ in sorted(best.items(), key=lambda item: item[1])[:limit]]

    def trigram(self, queries: list[str], plan: QueryPlan, limit: int = 30) -> list[str]:
        best: dict[str, float] = {}
        filters, params = self._filter_sql(plan)
        for query in queries:
            grams = sorted(char_trigrams(query))
            if not grams:
                continue
            placeholders = ",".join("?" for _ in grams)
            sql = f"""
                SELECT t.card_id, CAST(count(*) AS REAL)/? AS overlap
                FROM trigrams t JOIN cards c ON c.card_id=t.card_id
                WHERE t.gram IN ({placeholders}) AND {filters}
                GROUP BY t.card_id ORDER BY overlap DESC, t.card_id LIMIT ?
            """
            connection = self._channel_connections.get("trigram", self.connection)
            lock = self._channel_locks.get("trigram", self._lock)
            with lock:
                self._ensure_open()
                rows = connection.execute(
                    sql, [len(grams), *grams, *params, limit]
                ).fetchall()
            for row in rows:
                key, score = str(row[0]), float(row[1])
                best[key] = max(score, best.get(key, 0.0))
        return [key for key, _ in sorted(best.items(), key=lambda item: (-item[1], item[0]))[:limit]]

    def vector(self, vectors: list[list[float]], plan: QueryPlan, limit: int = 50) -> list[str]:
        best: dict[str, float] = {}
        for vector in vectors:
            if len(vector) != self.expected_dimension:
                raise RetrievalUnavailable(
                    "embedding", f"query dimension {len(vector)} != {self.expected_dimension}"
                )
            if any(not math.isfinite(float(value)) for value in vector) or not any(
                float(value) != 0 for value in vector
            ):
                raise RetrievalUnavailable("embedding", "query vector is non-finite or all zeros")
            clauses = ["embedding MATCH ?", "k=?"]
            params: list[Any] = [serialize_float32(vector), limit]
            if plan.filters.time_scope == "historical":
                clauses.append("validity='historical'")
            else:
                clauses.append("validity!='historical'")
            if plan.filters.campus:
                scopes = self._matching_campus_scopes(plan.filters.campus)
                placeholders = ",".join("?" for _ in scopes)
                clauses.append(f"campus IN ({placeholders})")
                params.extend(scopes)
            if plan.filters.audience:
                clauses.append("(audience=? OR audience='')")
                params.append(plan.filters.audience)
            with self._lock:
                self._ensure_open()
                nearest = self.connection.execute(
                    "SELECT card_id,distance FROM vec_cards WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY distance",
                    params,
                ).fetchall()
            for row in nearest:
                key, distance = str(row[0]), float(row[1])
                best[key] = min(distance, best.get(key, float("inf")))
        return [key for key, _ in sorted(best.items(), key=lambda item: (item[1], item[0]))[:limit]]

    async def recall_channels(
        self,
        queries: list[str],
        vectors: list[list[float]],
        plan: QueryPlan,
    ) -> dict[str, list[str]]:
        channel_names = ("exact", "bm25", "trigram", "vector")
        results = await asyncio.gather(
            asyncio.to_thread(self.exact, queries, plan, 10),
            asyncio.to_thread(self.bm25, queries, plan, 40),
            asyncio.to_thread(self.trigram, queries, plan, 30),
            asyncio.to_thread(self.vector, vectors, plan, 50),
            return_exceptions=True,
        )
        channels: dict[str, list[str]] = {}
        for name, result in zip(channel_names, results, strict=True):
            if isinstance(result, BaseException):
                # asyncio cannot stop an already-running to_thread call. Wait
                # for every channel above, then fail the whole recall without
                # leaving orphaned SQLite work behind.
                raise result
            channels[name] = result
        return channels

    def load_cards(self, ids: list[str]) -> dict[str, CardEvidence]:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        sql = f"""
            SELECT c.*,s.canonical_url,s.title AS source_title,s.published_at
            FROM cards c JOIN sources s ON s.source_id=c.source_id
            WHERE c.card_id IN ({placeholders})
        """
        with self._lock:
            self._ensure_open()
            rows = self.connection.execute(sql, ids).fetchall()
        cards: dict[str, CardEvidence] = {}
        for row in rows:
            cards[str(row["card_id"])] = CardEvidence(
                card_id=str(row["card_id"]),
                source_id=str(row["source_id"]),
                parent_card_id=row["parent_card_id"],
                title=str(row["title"]),
                summary=str(row["summary"]),
                evidence_quote=str(row["evidence_quote"]),
                source_locator=str(row["source_locator"]),
                facts=json.loads(row["facts"]),
                facets=json.loads(row["facets"]),
                campus=str(row["campus"]),
                audience=str(row["audience"]),
                validity=str(row["validity"]),
                card_kind=str(row["card_kind"]),
                subject_key=str(row["subject_key"]),
                fact_key=str(row["fact_key"]),
                retrieval_text=str(row["retrieval_text"]),
                canonical_url=str(row["canonical_url"]),
                source_title=str(row["source_title"]),
                published_at=row["published_at"],
            )
        return cards

    def healthcheck(self) -> dict[str, Any]:
        try:
            with self._lock:
                self._ensure_open()
                quick = self.connection.execute("PRAGMA quick_check").fetchone()[0]
                fts_count = self.connection.execute("SELECT count(*) FROM card_fts").fetchone()[0]
                card_count = self.connection.execute("SELECT count(*) FROM cards").fetchone()[0]
                vector_count = self.connection.execute("SELECT count(*) FROM vec_cards").fetchone()[0]
                self.connection.execute("SELECT rowid FROM card_fts WHERE card_fts MATCH '健康' LIMIT 1").fetchall()
                vector_row = self.connection.execute("SELECT embedding FROM vec_cards LIMIT 1").fetchone()
            if quick != "ok":
                raise ValueError(f"quick_check: {quick}")
            if card_count == 0 or fts_count != card_count or vector_count != card_count:
                raise ValueError(
                    f"row count mismatch cards={card_count} fts={fts_count} vectors={vector_count}"
                )
            if vector_row is None:
                raise ValueError("empty vector table")
            return {"cards": card_count, "fts": fts_count, "vectors": vector_count}
        except Exception as exc:
            raise RetrievalUnavailable("sqlite", str(exc)) from exc


# How far two reranker scores must separate before that difference counts as a
# real preference instead of banding noise.  The DLUT gateway's
# ``Qwen3-Reranker-8B`` returned 0.911-0.929 across obviously unrelated
# documents, so anything tighter than this collapses into a single tier.  This
# is the knob to revisit if the reranker is ever replaced by a calibrated one.
RERANK_TIE_MARGIN = 0.05

# When a question is answered by official entry points rather than by evidence,
# offer the best few rather than a single arbitrary one.  A topic typically
# spans several official pages, and the program attaches the links itself, so
# there is no model in the loop that could confuse them.
MAX_NAVIGATION_CARDS = 3


class StrongRetriever:
    def __init__(
        self,
        database: KnowledgeDatabase,
        models: RetrievalModels,
        min_rerank_score: float = 0.35,
        fast_path_enabled: bool = False,
    ) -> None:
        if not 0 <= min_rerank_score <= 1:
            raise ValueError("min_rerank_score must be between 0 and 1")
        self.database = database
        self.models = models
        self.min_rerank_score = min_rerank_score
        self.fast_path_enabled = fast_path_enabled
        self.candidate_allocator = RerankCandidateAllocator()

    async def retrieve(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
    ) -> RetrievalResult:
        try:
            stage_seconds: dict[str, float] = {}
            raw_plan: dict[str, Any] | None = None
            if self.fast_path_enabled:
                from .query_fastpath import fast_query_plan

                raw_plan = fast_query_plan(question, history)
            if raw_plan is None:
                started = time.perf_counter()
                raw_plan = await self.models.plan(question, history)
                stage_seconds["planner"] = time.perf_counter() - started
            plan = QueryPlan.from_dict(raw_plan, question)
            if plan.intent == "out_of_scope":
                raise InsufficientEvidence("out of scope")
            queries = list(dict.fromkeys([plan.standalone_query, *plan.subqueries]))
            started = time.perf_counter()
            vectors = await self.models.embed(queries)
            stage_seconds["embedding"] = time.perf_counter() - started
            if len(vectors) != len(queries):
                raise RetrievalUnavailable("embedding", "query embedding count mismatch")
            started = time.perf_counter()
            channels = await self.database.recall_channels(queries, vectors, plan)
            stage_seconds["recall"] = time.perf_counter() - started
            trace = RetrievalTrace(channel_ids=channels, stage_seconds=stage_seconds)
            first_stage = self._rrf(channels, 50)
            trace.first_stage_ids = first_stage
            card_map = self.database.load_cards(first_stage)
            for first_stage_rank, card_id in enumerate(first_stage, 1):
                card = card_map.get(card_id)
                if card is not None:
                    card.first_stage_rank = first_stage_rank
            rerank_ids = self.candidate_allocator.allocate(first_stage, card_map)
            trace.fused_ids = rerank_ids
            if not rerank_ids:
                raise InsufficientEvidence("no candidates")
            rerank_docs = [self._rerank_document(card_map[card_id]) for card_id in rerank_ids]
            started = time.perf_counter()
            ranked = await self.models.rerank(plan.standalone_query, rerank_docs)
            stage_seconds["reranker"] = time.perf_counter() - started
            if len(ranked) != len(rerank_ids):
                raise RetrievalUnavailable("reranker", "returned incomplete ranking")
            ordered: list[CardEvidence] = []
            seen_indexes: set[int] = set()
            for index, score in ranked:
                if index < 0 or index >= len(rerank_ids):
                    raise RetrievalUnavailable("reranker", "returned invalid document index")
                if index in seen_indexes:
                    raise RetrievalUnavailable("reranker", "returned duplicate document index")
                score = float(score)
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise RetrievalUnavailable("reranker", "returned invalid relevance score")
                seen_indexes.add(index)
                card = card_map[rerank_ids[index]]
                card.rerank_score = score
                ordered.append(card)
            trace.reranked_ids = [card.card_id for card in ordered]
            rerank_tiers = self._rerank_tiers(ordered)
            eligible = [
                card for card in ordered if card.rerank_score >= self.min_rerank_score
            ]
            fact_cards = [
                card
                for card in eligible
                if card.card_kind == "fact" and bool(card.evidence_quote)
            ]
            navigation_cards = [
                card
                for card in eligible
                if card.card_kind == "navigation" and not card.evidence_quote
            ]
            # Card kind no longer decides precedence.  Navigation cards are the
            # overwhelming majority of the reachable knowledge base, so a
            # fact-first rule made them selectable only through the "required
            # facets are missing" failure branch.  Both kinds now compete on the
            # same fused rank and the leader decides whether this question is
            # answered from evidence or from an official entry point.
            pool_size = len(first_stage)
            fact_cards = self._rank_fused(fact_cards, rerank_tiers, pool_size)
            navigation_cards = self._rank_fused(navigation_cards, rerank_tiers, pool_size)
            selectable = self._rank_fused(
                [*fact_cards, *navigation_cards], rerank_tiers, pool_size
            )
            trace.selection_ids = [card.card_id for card in selectable]
            if not selectable:
                raise InsufficientEvidence("reranker selected no evidence")
            if selectable[0].card_kind == "fact":
                source_groups: dict[str, list[CardEvidence]] = {}
                for card in fact_cards:
                    source_groups.setdefault(card.source_id, []).append(card)
                selected = []
                last_missing = list(plan.required_facets)
                for same_source_cards in source_groups.values():
                    candidate = same_source_cards[:4]
                    last_missing = missing_required_facets(plan.required_facets, candidate)
                    if not last_missing:
                        selected = candidate
                        break
                if not selected:
                    if navigation_cards:
                        selected = navigation_cards[:MAX_NAVIGATION_CARDS]
                    else:
                        raise InsufficientEvidence(
                            f"required facets missing from any single source: {', '.join(last_missing)}"
                        )
            else:
                selected = navigation_cards[:MAX_NAVIGATION_CARDS]
            parent_ids = list(
                dict.fromkeys(card.parent_card_id for card in selected if card.parent_card_id)
            )
            parents = self.database.load_cards(parent_ids)
            for card in selected:
                if card.parent_card_id:
                    card.parent_context = parents.get(card.parent_card_id)
                    if card.parent_context is None:
                        raise RetrievalUnavailable("sqlite", f"missing parent {card.parent_card_id}")
                    if card.parent_context.source_id != card.source_id:
                        raise RetrievalUnavailable(
                            "sqlite", f"parent source mismatch for {card.card_id}"
                        )
                    parent_matches_query = matches_query_scope(
                        validity=card.parent_context.validity,
                        campus=card.parent_context.campus,
                        audience=card.parent_context.audience,
                        time_scope=plan.filters.time_scope,
                        requested_campus=plan.filters.campus,
                        requested_audience=plan.filters.audience,
                    )
                    parent_covers_child = parent_scope_covers_child(
                        parent_validity=card.parent_context.validity,
                        parent_campus=card.parent_context.campus,
                        parent_audience=card.parent_context.audience,
                        child_validity=card.validity,
                        child_campus=card.campus,
                        child_audience=card.audience,
                    )
                    if not parent_matches_query or not parent_covers_child:
                        raise RetrievalUnavailable(
                            "sqlite", f"parent scope mismatch for {card.card_id}"
                        )
            trace.selected_ids = [card.card_id for card in selected]
            return RetrievalResult(plan, selected, trace)
        except InsufficientEvidence:
            raise
        except RetrievalUnavailable:
            raise
        except sqlite3.Error as exc:
            raise RetrievalUnavailable("sqlite", str(exc)) from exc
        except Exception as exc:
            raise RetrievalUnavailable("retrieval", str(exc)) from exc

    @staticmethod
    def _rerank_tiers(ordered: list[CardEvidence]) -> dict[str, int]:
        """Collapse reranker scores into confidence tiers.

        The reranker earns influence in proportion to how much it actually
        discriminates.  Cards scoring within ``RERANK_TIE_MARGIN`` of their tier
        leader share a rank, so a degenerate band - every candidate within a few
        thousandths, which is what the DLUT gateway returns - becomes a single
        tier that cannot outvote anything, and the first stage decides.  A
        reranker that genuinely separates candidates still splits them into
        tiers and still wins.
        """

        ranks: dict[str, int] = {}
        tier = 0
        leader_score: float | None = None
        for card in sorted(ordered, key=lambda card: -card.rerank_score):
            if leader_score is None or leader_score - card.rerank_score > RERANK_TIE_MARGIN:
                tier += 1
                leader_score = card.rerank_score
            ranks[card.card_id] = tier
        return ranks

    @staticmethod
    def _rank_fused(
        cards: list[CardEvidence],
        rerank_tiers: dict[str, int],
        pool_size: int,
    ) -> list[CardEvidence]:
        """Order cards by reranker confidence tier, then by first-stage rank.

        The reranker leads, but only at the resolution it can justify.  Within a
        single tier it has expressed no preference, so the first stage decides;
        across tiers it overrides the first stage outright.  The rule therefore
        slides between "trust the first stage" for a degenerate band and "trust
        the reranker" for a well-separated one, without a weighting constant to
        tune.

        Ties are never broken by card kind.  Doing so would quietly reintroduce
        the fact-first precedence this replaces, and navigation cards are the
        majority of the reachable knowledge base.

        Cards missing from either ranking sort last rather than being dropped,
        so this can only reorder candidates, never remove one.
        """

        worst_first_stage = pool_size + 1
        worst_tier = len(rerank_tiers) + 1

        def sort_key(card: CardEvidence) -> tuple[int, int, str]:
            first_stage = (
                card.first_stage_rank
                if card.first_stage_rank > 0
                else worst_first_stage
            )
            return (rerank_tiers.get(card.card_id, worst_tier), first_stage, card.card_id)

        return sorted(cards, key=sort_key)

    @staticmethod
    def _rerank_document(card: CardEvidence) -> str:
        return "\n".join(
            value
            for value in (card.source_title, card.title, card.evidence_quote)
            if value
        )

    @staticmethod
    def _rrf(channels: dict[str, list[str]], limit: int, k: int = 60) -> list[str]:
        scores: dict[str, float] = {}
        best_rank: dict[str, int] = {}
        for ids in channels.values():
            for rank, card_id in enumerate(ids, 1):
                scores[card_id] = scores.get(card_id, 0.0) + 1.0 / (k + rank)
                best_rank[card_id] = min(rank, best_rank.get(card_id, rank))
        ordered = sorted(scores, key=lambda card_id: (-scores[card_id], best_rank[card_id], card_id))
        return ordered[:limit]

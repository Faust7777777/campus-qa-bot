from __future__ import annotations

import asyncio
import json
import math
import re
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


class ModelOutputRejected(RetrievalUnavailable):
    """A model returned something the contract forbids, so this question fails.

    A subclass, so production behaviour is unchanged: the bot still declines
    rather than acting on a plan or a draft that broke a rule.

    Evaluation needs the distinction.  A gateway failure must abort the run,
    because scoring it as "no answer" would forge a restraint result, but a
    model breaking a rule is just that question failing.  Conflating them ended
    a 300-question run twice: once on an answer that invented a URL, once on a
    planner that returned fifteen required facets when the ceiling is twelve.

    It lives here rather than in errors.py because errors.py sits inside the
    build scope, where adding a class invalidates a knowledge database that has
    nothing to do with model output.
    """


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
            raise ModelOutputRejected("planner", f"invalid query plan: {exc}") from exc
        if intent not in {"fact", "procedure", "historical", "out_of_scope"}:
            raise ModelOutputRejected("planner", f"invalid intent: {intent}")
        if not standalone:
            standalone = original_question.strip()
        if not subqueries:
            subqueries = [standalone]
        if len(subqueries) > 3:
            raise ModelOutputRejected("planner", "more than 3 subqueries")
        if len(standalone) > 1000 or any(len(value) > 1000 for value in subqueries):
            raise ModelOutputRejected("planner", "query plan text is too long")
        if len(entities) > 20 or any(len(value) > 200 for value in entities):
            raise ModelOutputRejected("planner", "query plan has too many or oversized entities")
        if len(facets) > 12 or any(len(value) > 100 for value in facets):
            raise ModelOutputRejected("planner", "query plan has too many or oversized facets")
        if filters.time_scope not in {"current", "historical"}:
            raise ModelOutputRejected("planner", "invalid time_scope")
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
    # Native score per channel, kept so a caller can judge how good the best hit
    # is rather than only which hit is best.  Scales differ and are documented
    # on each channel: exact and trigram and vector are absolute, bm25 is not.
    channel_scores: dict[str, dict[str, float]] = field(default_factory=dict)
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

    def exact(self, queries: list[str], plan: QueryPlan, limit: int = 10) -> list[tuple[str, float]]:
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
        # Tier 0/1/2 is title / standard question / other, which is absolute:
        # an exact title hit means the same thing regardless of the corpus.
        return [(str(row[0]), {0: 1.0, 1: 0.9}.get(int(row[1]), 0.8)) for row in rows]

    def bm25(self, queries: list[str], plan: QueryPlan, limit: int = 40) -> list[tuple[str, float]]:
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
        # SQLite bm25() is negative and better the lower it goes, and its scale
        # depends on the corpus, so this is a ranking signal only - it cannot
        # answer "is the best hit any good".
        return [
            (key, float(value[1]))
            for key, value in sorted(best.items(), key=lambda item: item[1])[:limit]
        ]

    def trigram(self, queries: list[str], plan: QueryPlan, limit: int = 30) -> list[tuple[str, float]]:
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
        # Overlap fraction in [0, 1], absolute: it is the share of the query's
        # trigrams the card carries, whatever else is in the corpus.
        return sorted(best.items(), key=lambda item: (-item[1], item[0]))[:limit]

    def vector(self, vectors: list[list[float]], plan: QueryPlan, limit: int = 50) -> list[tuple[str, float]]:
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
        # Cosine distance in [0, 2] turned into similarity in [0, 1].  Absolute,
        # and the one signal that can say the nearest neighbour is still far:
        # this channel is k-NN, so it always returns k rows however unrelated.
        return [
            (key, max(0.0, 1.0 - distance / 2.0))
            for key, distance in sorted(best.items(), key=lambda item: (item[1], item[0]))[:limit]
        ]

    async def recall_channels(
        self,
        queries: list[str],
        vectors: list[list[float]],
        plan: QueryPlan,
    ) -> dict[str, list[tuple[str, float]]]:
        channel_names = ("exact", "bm25", "trigram", "vector")
        results = await asyncio.gather(
            asyncio.to_thread(self.exact, queries, plan, 10),
            asyncio.to_thread(self.bm25, queries, plan, 40),
            asyncio.to_thread(self.trigram, queries, plan, 30),
            asyncio.to_thread(self.vector, vectors, plan, 50),
            return_exceptions=True,
        )
        channels: dict[str, list[tuple[str, float]]] = {}
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
# real preference instead of banding noise.
#
# Measured against the DLUT gateway over ten real questions: the full spread
# across 8-16 candidates is 0.08-0.22, but the top of each distribution is
# tight - the leading candidates typically sit within 0.005-0.03 of each other,
# which is where selection actually happens.  So the reranker separates the
# plainly irrelevant tail well and the plausible head barely at all, and this
# margin is set to match: near-equal leaders share a tier and are ordered by
# first-stage rank, while the tail falls into lower tiers and stays there.
#
# Revisit if the reranker is ever replaced by a calibrated one.
RERANK_TIE_MARGIN = 0.05

# How much of the question a candidate must echo before it counts as being
# about the same thing.
#
# Removing fact-first precedence let navigation cards be selected, and some
# navigation card always shares a few characters with any question, so the bot
# began answering everything - including "怎样申请夜间无人机驾驶证", which it
# served with the nearest-looking application page.  Restraint on out-of-scope
# questions fell to zero.
#
# Calibrated on the frozen set rather than guessed.  With boilerplate removed
# the two populations barely overlap: answerable questions sit at a median of
# 0.55, out-of-scope ones at 0.00.
#
# The threshold is set where false refusals are still exactly zero, not where
# refusal is most effective, because the two errors are not symmetric.  Every
# answerable question this would refuse was one the bot had answered correctly:
#
#     0.05    refuses 63% of out-of-scope questions, loses 0 real answers
#     0.10    refuses 77%,  loses 3
#     0.15    refuses 88%,  loses 7
#
# and the seven are questions phrased the way a student actually asks - "大工食堂
# 早餐、午餐和晚餐几点开放" against a card titled 校内食堂餐次与营业时间.  Low
# overlap there means different wording, not a different subject.  A wrong
# answer gets corrected in the group; a refusal on a real question is a failure
# the person sees directly.  Raise this only with evidence that the questions it
# starts refusing were not being answered well anyway.
MIN_TOPIC_OVERLAP = 0.05


# Bigrams that campus questions and campus cards both use regardless of topic.
# Unweighted overlap let boilerplate carry a question over the line on its own:
# "本科生怎样申请夜间无人机驾驶证" scored 0.21 against a scholarship card purely
# on 本科 / 科生 / 申请, while "夜间无人机驾驶证在哪考" scored 0.00.  Phrasing
# decided the outcome instead of subject matter.
BOILERPLATE_TERMS = (
    "本科生", "同学", "学生", "学校", "校区", "我校",
    "怎么", "怎样", "如何", "哪里", "在哪", "什么", "多少", "是否", "可以", "需要",
    "申请", "办理", "手续", "流程", "规定", "要求", "条件", "材料", "时间", "地点",
    "通知", "相关", "有关", "进行", "提供", "开展",
)


# Audiences this bot does not serve.  The knowledge base is undergraduate-only,
# and the topic gate cannot catch these: "研究生国家奖学金评审材料在哪" reads as
# highly on-topic against the undergraduate scholarship cards, and answering it
# from them is worse than declining, because the two schemes differ.
OTHER_AUDIENCES = (
    "研究生", "硕士", "博士", "MBA", "EMBA", "教职工", "教师", "教工", "新教工",
    "留学生", "国际学生", "外籍", "校外社会人员", "导师",
)
# The audience has to be the one doing the thing, not merely mentioned.  Three
# answerable questions name another audience without being about them - a
# 指导教师 is a role on a student's team, an 国际学生助学金 is the name of a fund
# an undergraduate applies for, a 教师岗位 is a job an undergraduate applies to -
# so the marker only counts when an action verb follows it closely.
_AUDIENCE_ACTION = re.compile(
    "(" + "|".join(OTHER_AUDIENCES) + r")(?:[^。？！]{0,30}?)"
    r"(如何|怎[么样]|能否|可否|需要|应该|在哪|去哪|申请|办理|报销|评审|考核|报名|延期|预答辩)"
)
# An explicit scoping phrase settles it on its own: "教职工专属事项「…」的办理
# 流程" puts a quoted title between the audience and the verb, which no
# reasonable window catches, and the phrase already says who it is for.
_AUDIENCE_SCOPED = re.compile("(" + "|".join(OTHER_AUDIENCES) + r")(专属|专用|专场)")
# An explicit undergraduate marker settles it: "推荐优秀应届本科毕业生免试攻读
# 研究生" is an undergraduate service whatever else it names.
_UNDERGRADUATE = re.compile(r"本科生|本科|应届|推免|保研")


def serves_another_audience(question: str) -> str | None:
    """The audience this question is for, when it is plainly not undergraduates."""

    if _UNDERGRADUATE.search(question):
        return None
    scoped = _AUDIENCE_SCOPED.search(question)
    if scoped:
        return scoped.group(1)
    match = _AUDIENCE_ACTION.search(question)
    return match.group(1) if match else None


def _significant_bigrams(text: str) -> set[str]:
    compact = normalized_text(text)
    grams = {compact[i : i + 2] for i in range(len(compact) - 1)}
    boilerplate: set[str] = set()
    for term in BOILERPLATE_TERMS:
        boilerplate |= {term[i : i + 2] for i in range(len(term) - 1)}
    return grams - boilerplate


def topic_overlap(question: str, card: "CardEvidence") -> float:
    """Fraction of the question's *subject-bearing* bigrams the card echoes.

    Deliberately lexical and local: it asks whether this card is about what was
    asked, which is the one thing neither the reranker's banded scores nor the
    first stage's ranking can express - both are relative, and a pool of
    uniformly irrelevant cards still has a best member.

    Boilerplate is removed from the question side only.  A card is free to be
    written in whatever register it likes; what must match is the subject.
    """

    asked = _significant_bigrams(question)
    if not asked:
        return 1.0
    text = normalized_text(f"{card.title}\n{card.source_title}\n{card.evidence_quote}")
    echoed = {text[i : i + 2] for i in range(len(text) - 1)}
    return len(asked & echoed) / len(asked)


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
        vector_recall_enabled: bool = True,
    ) -> None:
        if not 0 <= min_rerank_score <= 1:
            raise ValueError("min_rerank_score must be between 0 and 1")
        self.database = database
        self.models = models
        self.min_rerank_score = min_rerank_score
        self.fast_path_enabled = fast_path_enabled
        # Measured over 44 realistic questions: the vector channel supplied the
        # leading candidate 0 times while adding cards to the pool every time,
        # and those additions are what let an invented question clear the topic
        # gate.  A confidence-gated fallback was the obvious answer but the data
        # does not support one - on realistic phrasing the lexical channels'
        # absolute scores do not separate answerable from out-of-scope
        # (trigram medians 0.20 against 0.18).  So this is a switch to be
        # decided by an A/B on the smoke set, not an automatic rule.
        self.vector_recall_enabled = vector_recall_enabled
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
            # The knowledge base is undergraduate-only, and the topic gate cannot
            # see this: a graduate scholarship question reads as highly on-topic
            # against the undergraduate scholarship cards, and answering it from
            # them states the wrong scheme's rules with full confidence.
            other = serves_another_audience(plan.standalone_query)
            if other:
                raise InsufficientEvidence(f"serves {other}, not undergraduates")
            queries = list(dict.fromkeys([plan.standalone_query, *plan.subqueries]))
            vectors: list[list[float]] = []
            if self.vector_recall_enabled:
                started = time.perf_counter()
                vectors = await self.models.embed(queries)
                stage_seconds["embedding"] = time.perf_counter() - started
                if len(vectors) != len(queries):
                    raise RetrievalUnavailable("embedding", "query embedding count mismatch")
            started = time.perf_counter()
            channels = await self.database.recall_channels(queries, vectors, plan)
            stage_seconds["recall"] = time.perf_counter() - started
            trace = RetrievalTrace(
                channel_ids={
                    name: [card_id for card_id, _ in hits] for name, hits in channels.items()
                },
                channel_scores={
                    name: {card_id: score for card_id, score in hits}
                    for name, hits in channels.items()
                },
                stage_seconds=stage_seconds,
            )
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
            # The whole candidate pool goes to the answer model, which picks.
            #
            # Every stage between recall and the answer used to be a ranker, and
            # rankers cannot say "none of these".  That forced a chain of gates -
            # a rerank-score floor, confidence tiers, a topic-overlap gate, a
            # required-facet gate - each guessing from a different angle at a
            # question only a reader can settle: does this text answer what was
            # asked?  Measured on ten colloquially phrased questions, that chain
            # cited the right card 3 times out of 10 while the card itself was in
            # the lexical pool all 10 times, usually in the top six.  Handing the
            # same pool to the answer model cited it 10 times out of 10, and
            # returned nothing at all for a faculty-privacy question, an
            # off-campus question and another university's question, which is the
            # judgement no ranker could make.  It also costs no extra call, since
            # it replaces the reranker.
            started = time.perf_counter()
            raw_selection = await self.models.select_evidence(
                plan.standalone_query,
                [self._candidate_document(card_map[card_id]) for card_id in rerank_ids],
            )
            stage_seconds["selector"] = time.perf_counter() - started
            # Evidence first, official entry points only when there is none.
            # Navigation cards reach the selector as a bare title, so a selector
            # asked only "whose text answers this" can never pick one - which
            # silently deleted the "here are the official pages" answer for 16 of
            # the 25 questions the knowledge base does not cover yet.  Asking for
            # the two separately keeps that answer while leaving the refusal
            # judgement where it belongs: both lists empty is a valid reply, and
            # it is what comes back for a teacher's phone number.
            ordered = self._picked_cards(raw_selection, "picked", rerank_ids, card_map)
            if not ordered:
                ordered = self._picked_cards(
                    raw_selection, "entry_points", rerank_ids, card_map
                )
            trace.reranked_ids = [card.card_id for card in ordered]
            fact_cards = [
                card
                for card in ordered
                if card.card_kind == "fact" and bool(card.evidence_quote)
            ]
            navigation_cards = [
                card
                for card in ordered
                if card.card_kind == "navigation" and not card.evidence_quote
            ]
            trace.selection_ids = [card.card_id for card in ordered]
            if not ordered:
                raise InsufficientEvidence("no candidate answers this question")
            # The selector's first pick decides what kind of answer this is, and
            # the rest of the pick has to agree with it.  A mixed selection would
            # cite a navigation card carrying no text beside a fact card that
            # does, and mixing sources would attribute one page's rules to
            # another's URL - so the leader's kind, and for evidence answers the
            # leader's source, filter the rest.  Ordering is the model's, which
            # is the point: a ranker cannot be trusted to lead, but a reader can.
            leader = ordered[0]
            if leader.card_kind == "fact" and leader.evidence_quote:
                selected = [
                    card for card in fact_cards if card.source_id == leader.source_id
                ][:4]
            else:
                selected = navigation_cards[:MAX_NAVIGATION_CARDS]
            if not selected:
                raise InsufficientEvidence("no candidate answers this question")
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
    def _candidate_document(card: CardEvidence) -> str:
        """One line per candidate, as the selector sees it.

        Navigation cards carry no evidence and no summary, so they are named
        rather than quoted.  Saying so in the line matters: without it the model
        reads a bare title as a card whose text it simply was not shown, and
        picks it as though the text supported the answer.
        """

        body = card.evidence_quote or card.summary or "（无正文，仅为官方入口链接）"
        return f"【{card.title}】{body}"

    @staticmethod
    def _picked_cards(
        selection: dict[str, Any],
        field: str,
        candidate_ids: list[str],
        card_map: dict[str, CardEvidence],
    ) -> list[CardEvidence]:
        """Resolve the selector's ordinals back to cards.

        Ordinals rather than card ids, so a model that hallucinates produces an
        out-of-range number instead of a plausible-looking id pointing at a card
        it never saw.  Out-of-range and repeated numbers are dropped rather than
        raised on: one bad index should not fail a question whose other picks
        are sound.  A reply that is not a list at all is a broken contract and
        does raise, because then nothing about the pick can be trusted.
        """

        picked = selection.get(field, [])
        if not isinstance(picked, list):
            raise ModelOutputRejected("selector", f"{field} is not a list")
        cards: list[CardEvidence] = []
        seen: set[str] = set()
        for ordinal in picked:
            if not isinstance(ordinal, int) or isinstance(ordinal, bool):
                continue
            if not 1 <= ordinal <= len(candidate_ids):
                continue
            card_id = candidate_ids[ordinal - 1]
            if card_id in seen:
                continue
            seen.add(card_id)
            cards.append(card_map[card_id])
        return cards

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
        """Build the document the reranker scores.

        Navigation cards carry no ``evidence_quote`` and no ``summary`` (0 of
        3068 have one), so this is a bare title for them while a fact card gets
        a whole paragraph.  That asymmetry looks like a handicap and has been
        proposed as a fix more than once.  It was measured against the gateway
        instead, same query and same candidate set, only the document changing:

            navigation  n=18  mean length  34  mean score 0.9270
            fact        n=43  mean length 190  mean score 0.8745
            correlation(length, score) = -0.116

        Navigation cards score *higher* while being far shorter, so the premise
        is false - a bare title is a dense topical signal and a long excerpt
        dilutes it.  Adding ``standard_question`` moved navigation scores by
        -0.0117 on average and changed top-1 in 5 of 6 queries, helping twice
        and hurting twice; adding ``retrieval_text`` was worse (-0.0182) and
        would also require weakening the test that keeps Luna-generated text
        out of ranking.  Neither is worth doing.

        Keep this to audited fields with no generated expansion.
        """

        return "\n".join(
            value
            for value in (card.source_title, card.title, card.evidence_quote)
            if value
        )

    @staticmethod
    def _rrf(
        channels: dict[str, list[tuple[str, float]]], limit: int, k: int = 60
    ) -> list[str]:
        """Fuse the channels by rank.

        Deliberately still rank-based: the native scores now travel alongside so
        that a caller can ask "is the best hit any good", but they are on four
        different scales and mixing them into one number would invent a
        comparison the data does not support.
        """

        scores: dict[str, float] = {}
        best_rank: dict[str, int] = {}
        for hits in channels.values():
            for rank, (card_id, _score) in enumerate(hits, 1):
                scores[card_id] = scores.get(card_id, 0.0) + 1.0 / (k + rank)
                best_rank[card_id] = min(rank, best_rank.get(card_id, rank))
        ordered = sorted(scores, key=lambda card_id: (-scores[card_id], best_rank[card_id], card_id))
        return ordered[:limit]

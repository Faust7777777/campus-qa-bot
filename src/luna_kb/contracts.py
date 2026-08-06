from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .errors import ContractError


class TaskStatus(StrEnum):
    PENDING = "pending"
    FETCHED = "fetched"
    EXTRACTED = "extracted"
    REVIEW_PENDING = "review_pending"
    APPROVED = "approved"
    DOWNGRADED = "downgraded"
    REJECTED = "rejected"
    PUBLISHED = "published"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DOWNGRADED = "downgraded"
    REJECTED = "rejected"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Validity(StrEnum):
    CURRENT = "current"
    HISTORICAL = "historical"
    UNKNOWN = "unknown"


class SourceAuthority(StrEnum):
    FORMAL_POLICY = "formal_policy"
    SERVICE_HALL = "service_hall"
    SCHOOL_NOTICE = "school_notice"
    NEWS = "news"
    OTHER = "other"


ALLOWED_DATASETS = frozenset({"kb_clean", "web_plus_index"})
FORBIDDEN_DATASETS = frozenset({"kb_faculty", "faculty"})
ALLOWED_FETCH_STATUSES = frozenset(
    {
        "success",
        "catalog_only",
        "unresolved",
        "out_of_scope",
        "fetch_failed",
        "search_failed",
    }
)
REQUIRED_LUNA_FIELDS = frozenset(
    {
        "source_id",
        "canonical_url",
        "title",
        "official_domain",
        "published_at",
        "fetched_at",
        "content_hash",
        "clean_text",
        "fetch_status",
        "candidate_cards",
        "unresolved_questions",
    }
)
MOJIBAKE_SEQUENCES = (
    "锟斤拷",
    "鍏充簬",
    "澶ц繛",
    "瀛︾敓",
    "鐢宠",
    "鏈",
    "绗",
)
MOJIBAKE_PUNCTUATION = ("锛", "銆", "鈥", "锝", "鈿")
MAX_SEMANTIC_TEXT_CHARS = 16_000
# Gateway batching limit.  It lives here rather than in clients.py so that the
# offline builder does not have to import the runtime client layer for one
# constant, which would drag the whole query-time module graph into the build
# scope and make every retrieval change invalidate a knowledge build.
MAX_EMBEDDING_BATCH_SIZE = 32


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def canonicalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ContractError(f"invalid public URL: {value!r}")
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host + port, path, parts.query, ""))


def normalized_text(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", (value or "").lower())


def looks_like_mojibake(value: str) -> bool:
    text = value or ""
    if any(sequence in text for sequence in MOJIBAKE_SEQUENCES):
        return True
    punctuation_hits = sum(text.count(marker) for marker in MOJIBAKE_PUNCTUATION)
    broken_han_hits = len(re.findall(r"[鏃鍚鏈涓鐢绔瀛]\?", text))
    return punctuation_hits >= 2 or broken_han_hits >= 2


def stable_id(prefix: str, *values: str) -> str:
    raw = "\x1f".join(values).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:20]}"


def content_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _string_array(data: Mapping[str, Any], field: str, *, required: bool = False) -> list[str]:
    if required and field not in data:
        raise ContractError(f"missing required string array: {field}")
    raw = data.get(field, [])
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise ContractError(f"{field} must be an array of strings")
    return [value.strip() for value in raw if value.strip()]


@dataclass(slots=True)
class KnowledgeTask:
    source_id: str
    dataset: str
    title: str
    canonical_url: str
    seed_description: str
    seed_query: str
    published_at: str | None
    action: str
    priority: int
    status: str = TaskStatus.PENDING

    def validate(self) -> None:
        if self.dataset not in ALLOWED_DATASETS:
            raise ContractError(f"dataset is not allowed in production: {self.dataset}")
        if not self.source_id.startswith(f"{self.dataset}:"):
            raise ContractError("source_id must be namespaced by dataset")
        if self.canonical_url:
            canonicalize_url(self.canonical_url)
        if not self.title.strip():
            raise ContractError("task title is required")


@dataclass(slots=True)
class CandidateCard:
    card_id: str
    title: str
    standard_question: str
    summary: str
    evidence_quote: str
    source_locator: str
    generated_questions: list[str]
    aliases: list[str]
    risk_level: str
    extraction_confidence: float
    retrieval_text: str = ""
    keywords: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    facets: list[str] = field(default_factory=list)
    campus: str = ""
    audience: str = "本科生"
    validity: str = Validity.UNKNOWN
    parent_card_id: str | None = None
    subject_key: str = ""
    fact_key: str = ""
    source_authority: str = SourceAuthority.OTHER
    card_kind: str = "fact"
    embedding: list[float] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateCard":
        unknown = set(data) - {item.name for item in fields(cls)}
        if unknown:
            raise ContractError(
                f"candidate card has unknown fields: {', '.join(sorted(str(value) for value in unknown))}"
            )
        required = {
            "title",
            "evidence_quote",
            "source_locator",
            "generated_questions",
            "aliases",
            "risk_level",
            "extraction_confidence",
        }
        missing = sorted(required - data.keys())
        if missing:
            raise ContractError(f"candidate card missing fields: {', '.join(missing)}")
        raw_facts = data.get("facts", {})
        if not isinstance(raw_facts, Mapping):
            raise ContractError("facts must be an object")
        raw_embedding = data.get("embedding")
        if raw_embedding is not None and not isinstance(raw_embedding, list):
            raise ContractError("embedding must be an array")
        card_id = str(data.get("card_id") or "")
        title = str(data["title"]).strip()
        if not card_id:
            card_id = stable_id("card", title, str(data.get("standard_question", "")))
        card = cls(
            card_id=card_id,
            title=title,
            standard_question=str(data.get("standard_question", "")).strip(),
            summary=str(data.get("summary", "")).strip(),
            evidence_quote=str(data["evidence_quote"]).strip(),
            source_locator=str(data["source_locator"]).strip(),
            generated_questions=_string_array(data, "generated_questions", required=True),
            aliases=_string_array(data, "aliases", required=True),
            risk_level=str(data["risk_level"]),
            extraction_confidence=float(data["extraction_confidence"]),
            retrieval_text=str(data.get("retrieval_text", "")).strip(),
            keywords=_string_array(data, "keywords"),
            facts=dict(raw_facts),
            facets=_string_array(data, "facets"),
            campus=str(data.get("campus", "")).strip(),
            audience=str(data.get("audience", "本科生")).strip(),
            validity=str(data.get("validity", Validity.UNKNOWN)),
            parent_card_id=data.get("parent_card_id"),
            subject_key=str(data.get("subject_key", "")).strip(),
            fact_key=str(data.get("fact_key", "")).strip(),
            source_authority=str(data.get("source_authority", SourceAuthority.OTHER)),
            card_kind=str(data.get("card_kind", "fact")),
            embedding=[float(v) for v in raw_embedding] if raw_embedding is not None else None,
        )
        card.validate()
        return card

    def validate(self) -> None:
        if not self.card_id or not self.title:
            raise ContractError("candidate card_id and title are required")
        if len(self.title) > 500 or len(self.summary) > 2000:
            raise ContractError("candidate card title or summary is too long")
        if len(self.evidence_quote) > 6000 or len(self.retrieval_text) > 6000:
            raise ContractError("candidate card evidence or retrieval text is too long")
        if len(self.generated_questions) > 20 or len(self.aliases) > 30:
            raise ContractError("candidate card has too many questions or aliases")
        if (
            not isinstance(self.generated_questions, list)
            or not isinstance(self.aliases, list)
            or not isinstance(self.keywords, list)
            or not isinstance(self.facets, list)
            or any(
                not isinstance(value, str)
                for value in [
                    *self.generated_questions,
                    *self.aliases,
                    *self.keywords,
                    *self.facets,
                ]
            )
        ):
            raise ContractError("candidate card list fields must contain strings")
        if not isinstance(self.facts, dict):
            raise ContractError("candidate card facts must be an object")
        if len(self.keywords) > 50 or len(self.facets) > 20:
            raise ContractError("candidate card has too many keywords or facets")
        if any(
            len(value) > 500
            for value in [*self.generated_questions, *self.aliases, *self.keywords, *self.facets]
        ):
            raise ContractError("candidate card list value is too long")
        if len(json.dumps(self.facts, ensure_ascii=False, sort_keys=True)) > 16000:
            raise ContractError("candidate card facts payload is too large")
        if len(self.search_text()) > MAX_SEMANTIC_TEXT_CHARS:
            raise ContractError("candidate card semantic ranking text is too large")
        text_payload = "\n".join(
            [
                self.title,
                self.standard_question,
                self.summary,
                self.evidence_quote,
                self.source_locator,
                self.retrieval_text,
                *self.generated_questions,
                *self.aliases,
                *self.keywords,
                *self.facets,
                json.dumps(self.facts, ensure_ascii=False, sort_keys=True),
            ]
        )
        if looks_like_mojibake(text_payload):
            raise ContractError("candidate card contains probable mojibake")
        if self.risk_level not in set(RiskLevel):
            raise ContractError(f"invalid risk_level: {self.risk_level}")
        if self.validity not in set(Validity):
            raise ContractError(f"invalid validity: {self.validity}")
        if self.source_authority not in set(SourceAuthority):
            raise ContractError(f"invalid source_authority: {self.source_authority}")
        if self.card_kind not in {"fact", "navigation"}:
            raise ContractError(f"invalid card_kind: {self.card_kind}")
        if not 0 <= self.extraction_confidence <= 1:
            raise ContractError("extraction_confidence must be between 0 and 1")
        if self.embedding is not None and (
            not isinstance(self.embedding, list) or not self.embedding
        ):
            raise ContractError("embedding must be a non-empty array")

    def search_text(self) -> str:
        # This text is embedded and later shown to the reranker. Keep
        # generated questions for recall, but ground semantic ranking in the
        # literal source excerpt instead of model-authored summaries/facts.
        values = [
            self.title,
            self.standard_question,
            *self.generated_questions,
            *self.aliases,
            *self.keywords,
            *self.facets,
            self.evidence_quote,
            self.retrieval_text,
        ]
        return "\n".join(value for value in values if value)


@dataclass(slots=True)
class LunaSourceResult:
    source_id: str
    dataset: str
    canonical_url: str
    title: str
    official_domain: str
    published_at: str | None
    fetched_at: str | None
    content_hash: str
    clean_text: str
    fetch_status: str
    candidate_cards: list[CandidateCard]
    unresolved_questions: list[str]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LunaSourceResult":
        unknown = set(data) - {item.name for item in fields(cls)}
        if unknown:
            raise ContractError(
                f"Luna result has unknown fields: {', '.join(sorted(str(value) for value in unknown))}"
            )
        missing = sorted(REQUIRED_LUNA_FIELDS - data.keys())
        if missing:
            raise ContractError(f"Luna result missing fields: {', '.join(missing)}")
        raw_cards = data["candidate_cards"]
        if not isinstance(raw_cards, list) or any(
            not isinstance(card, Mapping) for card in raw_cards
        ):
            raise ContractError("candidate_cards must be an array of objects")
        raw_unresolved = data["unresolved_questions"]
        if not isinstance(raw_unresolved, list) or any(
            not isinstance(value, str) for value in raw_unresolved
        ):
            raise ContractError("unresolved_questions must be an array of strings")
        dataset = str(data.get("dataset") or str(data["source_id"]).split(":", 1)[0])
        result = cls(
            source_id=str(data["source_id"]),
            dataset=dataset,
            canonical_url=canonicalize_url(str(data["canonical_url"])) if data["canonical_url"] else "",
            title=str(data["title"]).strip(),
            official_domain=str(data["official_domain"]).lower().strip(),
            published_at=str(data["published_at"]) if data["published_at"] else None,
            fetched_at=str(data["fetched_at"]) if data["fetched_at"] else None,
            content_hash=str(data["content_hash"]),
            clean_text=str(data["clean_text"]),
            fetch_status=str(data["fetch_status"]),
            candidate_cards=[CandidateCard.from_dict(card) for card in raw_cards],
            unresolved_questions=[value.strip() for value in raw_unresolved if value.strip()],
        )
        result.validate()
        return result

    def validate(self) -> None:
        if not isinstance(self.candidate_cards, list) or not isinstance(
            self.unresolved_questions, list
        ):
            raise ContractError("source card and unresolved fields must be arrays")
        if any(not isinstance(value, str) for value in self.unresolved_questions):
            raise ContractError("unresolved_questions must contain strings")
        for card in self.candidate_cards:
            if not isinstance(card, CandidateCard):
                raise ContractError("candidate_cards must contain CandidateCard values")
            card.validate()
        if self.dataset not in ALLOWED_DATASETS or self.dataset in FORBIDDEN_DATASETS:
            raise ContractError(f"forbidden production dataset: {self.dataset}")
        if not self.source_id.startswith(f"{self.dataset}:"):
            raise ContractError("source_id/dataset mismatch")
        if self.canonical_url:
            host = (urlsplit(self.canonical_url).hostname or "").lower()
            if self.official_domain and host != self.official_domain:
                raise ContractError("official_domain does not match canonical_url")
        if self.content_hash != content_digest(self.clean_text):
            raise ContractError("content_hash does not match clean_text")
        if len(self.title) > 500 or len(self.canonical_url) > 4096:
            raise ContractError("source title or URL is too long")
        if len(self.clean_text) > 2_000_000:
            raise ContractError("source clean_text is too large")
        if looks_like_mojibake(
            "\n".join([self.title, self.clean_text, *self.unresolved_questions])
        ):
            raise ContractError("source contains probable mojibake")
        if len(self.candidate_cards) > 4 or len(self.unresolved_questions) > 50:
            raise ContractError("source has too many cards or unresolved questions")
        if any(len(value) > 2000 for value in self.unresolved_questions):
            raise ContractError("unresolved question is too long")
        if self.fetch_status not in ALLOWED_FETCH_STATUSES:
            raise ContractError(f"invalid fetch_status: {self.fetch_status}")
        if self.fetch_status == "success":
            if not self.canonical_url or not self.official_domain or not self.title:
                raise ContractError(
                    "successful fetch must contain canonical_url, official_domain, and title"
                )
            if not self.clean_text:
                raise ContractError("successful fetch must contain clean_text")
        if self.fetch_status == "catalog_only":
            if not self.canonical_url or not self.official_domain or not self.title:
                raise ContractError("catalog source must contain URL, domain, and title")
            if self.clean_text:
                raise ContractError("catalog-only source cannot pretend to contain article text")
            if any(
                card.card_kind != "navigation"
                or card.evidence_quote
                or card.facts
                or card.facets
                for card in self.candidate_cards
            ):
                raise ContractError("catalog-only source can contain only evidence-free navigation cards")
        if self.fetch_status not in {"success", "catalog_only"} and self.candidate_cards:
            raise ContractError("non-successful source cannot contain candidate cards")


@dataclass(slots=True)
class ReviewedCard:
    source: LunaSourceResult
    card: CandidateCard
    review_status: str
    review_reason: str
    reviewer: str
    reviewed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": {
                **asdict(self.source),
                "candidate_cards": [asdict(card) for card in self.source.candidate_cards],
            },
            "card": asdict(self.card),
            "review_status": self.review_status,
            "review_reason": self.review_reason,
            "reviewer": self.reviewer,
            "reviewed_at": self.reviewed_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReviewedCard":
        unknown = set(data) - {item.name for item in fields(cls)}
        if unknown:
            raise ContractError(
                f"reviewed card has unknown fields: {', '.join(sorted(str(value) for value in unknown))}"
            )
        reviewed = cls(
            source=LunaSourceResult.from_dict(data["source"]),
            card=CandidateCard.from_dict(data["card"]),
            review_status=str(data["review_status"]),
            review_reason=str(data["review_reason"]),
            reviewer=str(data["reviewer"]),
            reviewed_at=str(data["reviewed_at"]),
        )
        reviewed.validate()
        return reviewed

    def validate(self) -> None:
        self.source.validate()
        self.card.validate()
        if self.card.card_id not in {
            candidate.card_id for candidate in self.source.candidate_cards
        }:
            raise ContractError("reviewed card is not part of the source candidate lineage")
        if self.review_status not in set(ReviewStatus):
            raise ContractError(f"invalid review_status: {self.review_status}")
        if not self.review_reason.strip() or not self.reviewer.strip() or not self.reviewed_at.strip():
            raise ContractError("review reason, reviewer, and reviewed_at are required")


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

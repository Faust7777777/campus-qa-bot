from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from ..attestation import ArtifactSnapshot
from ..clients import MAX_EMBEDDING_BATCH_SIZE
from ..contracts import ReviewStatus, ReviewedCard, json_dumps, normalized_text, utc_now
from ..errors import BuildError, ContractError
from ..scope_policy import parent_scope_covers_child
from ..vector import load_sqlite_vec, serialize_float32

SCHEMA_VERSION = 2
ALLOWED_CAMPUSES = {
    "",
    "全校",
    "凌水",
    "开发区",
    "盘锦",
    "凌水|开发区",
    "凌水|盘锦",
    "开发区|盘锦",
    "凌水|开发区|盘锦",
}
PRODUCTION_AUDIENCE = "本科生"


def _official_host(host: str) -> bool:
    host = host.lower().strip(".")
    return host == "dlut.edu.cn" or host.endswith(".dlut.edu.cn") or host == "mp.weixin.qq.com"


class OfflineEmbedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def lexical_tokens(text: str) -> str:
    lowered = (text or "").lower()
    tokens: list[str] = re.findall(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", lowered)
    for run in re.findall(r"[\u3400-\u9fff]+", lowered):
        tokens.extend(run)
        tokens.extend(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return " ".join(tokens)


def char_trigrams(text: str) -> set[str]:
    compact = normalized_text(text)
    if not compact:
        return set()
    if len(compact) <= 3:
        return {compact}
    return {compact[index : index + 3] for index in range(len(compact) - 2)}


def load_reviewed_snapshot(snapshot: ArtifactSnapshot) -> list[ReviewedCard]:
    reviewed: list[ReviewedCard] = []
    for line_number, line in enumerate(snapshot.text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            reviewed.append(ReviewedCard.from_dict(json.loads(line)))
        except (json.JSONDecodeError, ContractError, KeyError, TypeError, ValueError) as exc:
            raise BuildError(f"{snapshot.name}:{line_number}: {exc}") from exc
    return reviewed


def load_reviewed(path: Path) -> list[ReviewedCard]:
    return load_reviewed_snapshot(ArtifactSnapshot.from_path(path))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_row(item: ReviewedCard) -> tuple[Any, ...]:
    source = item.source
    return (
        source.source_id,
        source.dataset,
        source.canonical_url,
        source.title,
        source.official_domain,
        source.published_at,
        source.fetched_at,
        source.content_hash,
        # Full cleaned articles belong to the offline review corpus. Runtime
        # needs only approved card excerpts, source metadata and the content
        # digest; retaining full text would increase the immutable DB and can
        # preserve material deliberately removed by a downgrade decision.
        "",
    )


def _create_schema(connection: sqlite3.Connection, dimension: int) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE sources(
            source_id TEXT PRIMARY KEY,
            dataset TEXT NOT NULL CHECK(dataset IN ('kb_clean','web_plus_index')),
            canonical_url TEXT NOT NULL,
            title TEXT NOT NULL,
            official_domain TEXT NOT NULL,
            published_at TEXT,
            fetched_at TEXT,
            content_hash TEXT NOT NULL,
            evidence_text TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE cards(
            card_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources(source_id),
            parent_card_id TEXT REFERENCES cards(card_id),
            title TEXT NOT NULL,
            standard_question TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence_quote TEXT NOT NULL,
            source_locator TEXT NOT NULL,
            generated_questions TEXT NOT NULL,
            aliases TEXT NOT NULL,
            keywords TEXT NOT NULL,
            facts TEXT NOT NULL,
            facets TEXT NOT NULL,
            campus TEXT NOT NULL,
            audience TEXT NOT NULL,
            validity TEXT NOT NULL CHECK(validity IN ('current','historical','unknown')),
            subject_key TEXT NOT NULL,
            fact_key TEXT NOT NULL,
            card_kind TEXT NOT NULL,
            risk_level TEXT NOT NULL,
            extraction_confidence REAL NOT NULL,
            retrieval_text TEXT NOT NULL,
            review_status TEXT NOT NULL CHECK(review_status IN ('approved','downgraded'))
        ) WITHOUT ROWID;
        CREATE INDEX cards_filters ON cards(validity, campus, audience);
        CREATE INDEX cards_parent ON cards(parent_card_id);
        CREATE INDEX cards_fact_key ON cards(fact_key);
        CREATE TABLE exact_terms(
            term TEXT NOT NULL,
            card_id TEXT NOT NULL REFERENCES cards(card_id),
            term_type TEXT NOT NULL,
            PRIMARY KEY(term, card_id, term_type)
        ) WITHOUT ROWID;
        CREATE INDEX exact_terms_card ON exact_terms(card_id);
        CREATE TABLE trigrams(
            gram TEXT NOT NULL,
            card_id TEXT NOT NULL REFERENCES cards(card_id),
            PRIMARY KEY(gram, card_id)
        ) WITHOUT ROWID;
        CREATE INDEX trigrams_card ON trigrams(card_id);
        CREATE VIRTUAL TABLE card_fts USING fts5(
            card_id UNINDEXED,
            title,
            standard_question,
            aliases,
            keywords,
            summary,
            facts,
            retrieval_text,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    connection.execute(
        f"""CREATE VIRTUAL TABLE vec_cards USING vec0(
            card_id TEXT PRIMARY KEY,
            embedding float[{dimension}] distance_metric=cosine,
            validity TEXT,
            campus TEXT,
            audience TEXT
        )"""
    )


async def build_database(
    reviewed: list[ReviewedCard],
    output: Path,
    embedder: OfflineEmbedder | None = None,
    expected_dimension: int | None = None,
) -> dict[str, Any]:
    publishable = [
        item
        for item in reviewed
        if item.review_status in {ReviewStatus.APPROVED, ReviewStatus.DOWNGRADED}
    ]
    if not publishable:
        raise BuildError("no approved or downgraded cards to publish")
    for item in publishable:
        if item.source.dataset not in {"kb_clean", "web_plus_index"}:
            raise BuildError(f"forbidden dataset reached builder: {item.source.dataset}")
        if "faculty" in item.source.source_id.lower():
            raise BuildError("faculty isolation boundary violated")
        try:
            item.validate()
        except ContractError as exc:
            raise BuildError(f"invalid reviewed item {item.card.card_id}: {exc}") from exc
        host = (urlsplit(item.source.canonical_url).hostname or "").lower()
        if not item.source.canonical_url or not _official_host(host):
            raise BuildError(f"non-official source reached builder: {item.source.source_id}")
        if host != item.source.official_domain:
            raise BuildError(f"source domain mismatch reached builder: {item.source.source_id}")
        if item.source.fetch_status not in {"success", "catalog_only"}:
            raise BuildError(f"non-successful source reached builder: {item.source.source_id}")
        if item.source.fetch_status == "catalog_only" and item.card.card_kind != "navigation":
            raise BuildError(f"catalog fact card reached builder: {item.card.card_id}")
        if item.card.audience != PRODUCTION_AUDIENCE:
            raise BuildError(f"non-undergraduate card reached builder: {item.card.card_id}")
        if item.card.campus not in ALLOWED_CAMPUSES:
            raise BuildError(f"unsupported campus reached builder: {item.card.card_id}")
        if (
            item.review_status == ReviewStatus.DOWNGRADED
            and item.card.card_kind != "navigation"
        ):
            raise BuildError(f"downgraded fact card reached builder: {item.card.card_id}")
        if item.card.card_kind == "fact" and (
            len(normalized_text(item.card.evidence_quote)) < 8
            or not item.card.source_locator
            or item.card.evidence_quote not in item.source.clean_text
        ):
            raise BuildError(f"non-literal evidence reached builder: {item.card.card_id}")
        if item.card.card_kind == "fact" and (
            not item.card.subject_key or not item.card.fact_key
        ):
            raise BuildError(f"fact card lacks conflict identity: {item.card.card_id}")
        if item.card.card_kind == "navigation" and (
            item.card.summary
            or item.card.evidence_quote
            or item.card.source_locator
            or item.card.facts
            or item.card.facets
        ):
            raise BuildError(f"unsanitized navigation card reached builder: {item.card.card_id}")

    missing = [item for item in publishable if item.card.embedding is None]
    if missing:
        if embedder is None:
            raise BuildError(f"{len(missing)} cards lack offline embeddings")
        try:
            batch_delay = max(
                0.0,
                min(float(os.getenv("LUNA_BUILD_EMBEDDING_DELAY_SECONDS", "0")), 60.0),
            )
        except ValueError as exc:
            raise BuildError("LUNA_BUILD_EMBEDDING_DELAY_SECONDS must be numeric") from exc
        for offset in range(0, len(missing), MAX_EMBEDDING_BATCH_SIZE):
            if offset and batch_delay:
                await asyncio.sleep(batch_delay)
            batch = missing[offset : offset + MAX_EMBEDDING_BATCH_SIZE]
            vectors = await embedder.embed([item.card.search_text() for item in batch])
            if len(vectors) != len(batch):
                raise BuildError(
                    f"embedding response count mismatch in batch starting at {offset}"
                )
            for item, vector in zip(batch, vectors, strict=True):
                item.card.embedding = [float(value) for value in vector]

    dimensions = {len(item.card.embedding or []) for item in publishable}
    if len(dimensions) != 1 or 0 in dimensions:
        raise BuildError(f"inconsistent embedding dimensions: {sorted(dimensions)}")
    dimension = dimensions.pop()
    if expected_dimension is not None and dimension != expected_dimension:
        raise BuildError(f"embedding dimension {dimension} != configured {expected_dimension}")
    for item in publishable:
        vector = item.card.embedding or []
        if any(not math.isfinite(float(value)) for value in vector) or not any(
            float(value) != 0 for value in vector
        ):
            raise BuildError(f"invalid embedding values for {item.card.card_id}")

    cards_by_id = {item.card.card_id: item for item in publishable}
    card_ids = set(cards_by_id)
    for item in publishable:
        parent = item.card.parent_card_id
        if parent and parent not in card_ids:
            raise BuildError(f"missing approved parent card {parent} for {item.card.card_id}")
        if parent and cards_by_id[parent].source.source_id != item.source.source_id:
            raise BuildError(f"parent source mismatch for {item.card.card_id}")
        if parent:
            parent_card = cards_by_id[parent].card
            if not parent_scope_covers_child(
                parent_validity=parent_card.validity,
                parent_campus=parent_card.campus,
                parent_audience=parent_card.audience,
                child_validity=item.card.validity,
                child_campus=item.card.campus,
                child_audience=item.card.audience,
            ):
                raise BuildError(f"parent scope mismatch for {item.card.card_id}")

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise BuildError(f"refusing to overwrite database: {output}")
    connection = sqlite3.connect(output)
    try:
        load_sqlite_vec(connection, build=True)
        _create_schema(connection, dimension)
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("embedding_dimension", str(dimension)),
                ("built_at", utc_now()),
            ],
        )
        sources = {_source_row(item) for item in publishable}
        connection.executemany(
            "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?)", sorted(sources)
        )
        for item in sorted(publishable, key=lambda value: bool(value.card.parent_card_id)):
            card = item.card
            search_text = card.search_text()
            connection.execute(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    card.card_id,
                    item.source.source_id,
                    card.parent_card_id,
                    card.title,
                    card.standard_question,
                    card.summary,
                    card.evidence_quote,
                    card.source_locator,
                    json_dumps(card.generated_questions),
                    json_dumps(card.aliases),
                    json_dumps(card.keywords),
                    json_dumps(card.facts),
                    json_dumps(card.facets),
                    card.campus,
                    card.audience,
                    card.validity,
                    card.subject_key,
                    card.fact_key,
                    card.card_kind,
                    card.risk_level,
                    card.extraction_confidence,
                    search_text,
                    item.review_status,
                ),
            )
            exact_rows = {(normalized_text(card.title), card.card_id, "title")}
            if card.standard_question:
                exact_rows.add((normalized_text(card.standard_question), card.card_id, "question"))
            exact_rows.update((normalized_text(value), card.card_id, "alias") for value in card.aliases)
            exact_rows.update((normalized_text(value), card.card_id, "question") for value in card.generated_questions)
            connection.executemany(
                "INSERT OR IGNORE INTO exact_terms VALUES (?,?,?)",
                [row for row in exact_rows if row[0]],
            )
            fields = [
                lexical_tokens(card.title),
                lexical_tokens(card.standard_question + " " + " ".join(card.generated_questions)),
                lexical_tokens(" ".join(card.aliases)),
                lexical_tokens(" ".join(card.keywords)),
                lexical_tokens(card.summary),
                lexical_tokens(json_dumps(card.facts)),
                lexical_tokens(card.retrieval_text),
            ]
            connection.execute(
                "INSERT INTO card_fts VALUES (?,?,?,?,?,?,?,?)", (card.card_id, *fields)
            )
            connection.executemany(
                "INSERT INTO trigrams VALUES (?,?)",
                [(gram, card.card_id) for gram in sorted(char_trigrams(search_text))],
            )
            connection.execute(
                "INSERT INTO vec_cards(card_id,embedding,validity,campus,audience) VALUES (?,?,?,?,?)",
                (
                    card.card_id,
                    serialize_float32(card.embedding or []),
                    card.validity,
                    card.campus,
                    card.audience,
                ),
            )
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise BuildError(f"SQLite integrity check failed: {integrity}")
        counts = {
            "sources": connection.execute("SELECT count(*) FROM sources").fetchone()[0],
            "cards": connection.execute("SELECT count(*) FROM cards").fetchone()[0],
            "fts_rows": connection.execute("SELECT count(*) FROM card_fts").fetchone()[0],
            "vector_rows": connection.execute("SELECT count(*) FROM vec_cards").fetchone()[0],
            "trigrams": connection.execute("SELECT count(*) FROM trigrams").fetchone()[0],
        }
    except Exception:
        connection.close()
        if output.exists():
            output.unlink()
        raise
    else:
        connection.close()

    return {
        "generated_at": utc_now(),
        "schema_version": SCHEMA_VERSION,
        "embedding_dimension": dimension,
        "embedding_batch_size": MAX_EMBEDDING_BATCH_SIZE,
        "counts": counts,
        "review_status_counts": dict(Counter(item.review_status for item in reviewed)),
        "database_sha256": _sha256(output),
        "faculty_cards": 0,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def make_manifest(version: str, build_report: dict[str, Any], review_report: dict[str, Any]) -> dict[str, Any]:
    actual_counts = build_report.get("review_status_counts", {})
    review_count_keys = ("approved", "downgraded", "rejected", "pending")
    counts_match = all(
        int(review_report.get(key, -1)) == int(actual_counts.get(key, 0))
        for key in review_count_keys
    )
    return {
        "version": version,
        "created_at": utc_now(),
        "schema_version": build_report["schema_version"],
        "embedding_dimension": build_report["embedding_dimension"],
        "knowledge_sha256": build_report["database_sha256"],
        "card_count": build_report["counts"]["cards"],
        "source_count": build_report["counts"]["sources"],
        "reviewed_sha256": build_report.get("reviewed_sha256", ""),
        "review_gate_passed": counts_match and int(actual_counts.get("pending", 0)) == 0,
        "evaluation_gate_passed": False,
        "read_only": True,
    }

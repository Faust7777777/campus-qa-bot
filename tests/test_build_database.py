from collections.abc import Callable
from pathlib import Path
import json
import sqlite3

import pytest

from luna_kb.contracts import ReviewedCard, content_digest
from luna_kb.errors import BuildError, RetrievalUnavailable
from luna_kb.pipeline.build import build_database, load_reviewed, make_manifest
from luna_kb.retrieval import KnowledgeDatabase


class RecordingEmbedder:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batch_sizes.append(len(texts))
        return [[1.0, 0.0, 0.0] for _ in texts]


@pytest.mark.asyncio
async def test_approved_card_builds_a_read_only_search_database(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"

    report = await build_database(
        [make_approved_card()],
        database_path,
        expected_dimension=3,
    )

    assert report["counts"]["cards"] == 1
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        assert database.healthcheck() == {"cards": 1, "fts": 1, "vectors": 1}
    finally:
        database.close()


@pytest.mark.asyncio
async def test_runtime_database_does_not_retain_full_offline_articles(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    item = make_approved_card()

    await build_database([item], database_path, expected_dimension=3)
    database = KnowledgeDatabase(database_path, expected_dimension=3)
    try:
        source = database.connection.execute(
            "SELECT evidence_text,content_hash FROM sources WHERE source_id=?",
            (item.source.source_id,),
        ).fetchone()
        card = database.connection.execute(
            "SELECT evidence_quote FROM cards WHERE card_id=?",
            (item.card.card_id,),
        ).fetchone()

        assert source["evidence_text"] == ""
        assert source["content_hash"] == item.source.content_hash
        assert card["evidence_quote"] == item.card.evidence_quote
    finally:
        database.close()


@pytest.mark.asyncio
async def test_offline_embeddings_are_built_in_bounded_batches(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    cards = [
        make_approved_card(
            card_id=f"card-{index}",
            source_id=f"kb_clean:source-{index}",
            canonical_url=f"https://example.dlut.edu.cn/{index}",
            fact_key=f"fact-{index}",
            embedding=[1.0, 0.0, 0.0],
        )
        for index in range(65)
    ]
    for item in cards:
        item.card.embedding = None
    embedder = RecordingEmbedder()

    report = await build_database(
        cards,
        tmp_path / "knowledge.sqlite",
        embedder=embedder,
        expected_dimension=3,
    )

    assert embedder.batch_sizes == [32, 32, 1]
    assert report["embedding_batch_size"] == 32


@pytest.mark.asyncio
async def test_faculty_probe_data_can_never_reach_the_production_database(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card()
    item.source.dataset = "kb_faculty"
    item.source.source_id = "kb_faculty:probe"
    database_path = tmp_path / "knowledge.sqlite"

    with pytest.raises(BuildError):
        await build_database([item], database_path, expected_dimension=3)

    assert not database_path.exists()


@pytest.mark.asyncio
async def test_runtime_rejects_an_incompatible_database_schema(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    database_path = tmp_path / "knowledge.sqlite"
    await build_database([make_approved_card()], database_path, expected_dimension=3)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")

    with pytest.raises(RetrievalUnavailable) as error:
        KnowledgeDatabase(database_path, expected_dimension=3)

    assert error.value.component == "sqlite"


@pytest.mark.asyncio
async def test_builder_rejects_an_all_zero_embedding(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card(embedding=[0.0, 0.0, 0.0])

    with pytest.raises(BuildError, match="invalid embedding"):
        await build_database([item], tmp_path / "knowledge.sqlite", expected_dimension=3)


@pytest.mark.asyncio
async def test_builder_rechecks_official_source_invariants(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card(canonical_url="https://evil.example/item")
    item.source.official_domain = "evil.example"

    with pytest.raises(BuildError, match="non-official"):
        await build_database([item], tmp_path / "knowledge.sqlite", expected_dimension=3)


@pytest.mark.asyncio
async def test_builder_rejects_an_unsanitized_navigation_card(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card()
    item.card.card_kind = "navigation"

    with pytest.raises(BuildError, match="unsanitized navigation"):
        await build_database([item], tmp_path / "knowledge.sqlite", expected_dimension=3)


@pytest.mark.asyncio
async def test_catalog_only_navigation_card_builds_without_fake_article_text(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card()
    item.source.fetch_status = "catalog_only"
    item.source.clean_text = ""
    item.source.content_hash = content_digest("")
    item.card.card_kind = "navigation"
    item.card.summary = ""
    item.card.evidence_quote = ""
    item.card.source_locator = ""
    item.card.facts = {}
    item.card.facets = []

    report = await build_database(
        [item], tmp_path / "knowledge.sqlite", expected_dimension=3
    )

    assert report["counts"]["cards"] == 1


@pytest.mark.asyncio
async def test_builder_rejects_navigation_claim_material(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card()
    item.card.card_kind = "navigation"
    item.card.evidence_quote = ""
    item.card.source_locator = ""
    item.card.facts = {}
    item.card.facets = []

    with pytest.raises(BuildError, match="unsanitized navigation"):
        await build_database([item], tmp_path / "knowledge.sqlite", expected_dimension=3)


@pytest.mark.asyncio
async def test_builder_rejects_fact_without_conflict_identity(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card(fact_key="")

    with pytest.raises(BuildError, match="conflict identity"):
        await build_database([item], tmp_path / "knowledge.sqlite", expected_dimension=3)


@pytest.mark.asyncio
async def test_builder_rejects_a_parent_from_another_source(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    parent = make_approved_card(
        card_id="parent",
        source_id="kb_clean:parent",
        fact_key="parent-fact",
    )
    child = make_approved_card(
        card_id="child",
        source_id="kb_clean:child",
        fact_key="child-fact",
    )
    child.card.parent_card_id = parent.card.card_id

    with pytest.raises(BuildError, match="parent source mismatch"):
        await build_database(
            [parent, child], tmp_path / "knowledge.sqlite", expected_dimension=3
        )


@pytest.mark.asyncio
async def test_builder_rejects_parent_evidence_outside_the_child_scope(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    parent = make_approved_card(card_id="parent", fact_key="parent-fact", campus="盘锦")
    child = make_approved_card(card_id="child", fact_key="child-fact", campus="凌水")
    child.source = parent.source
    child.card.parent_card_id = parent.card.card_id
    parent.source.candidate_cards = [parent.card, child.card]

    with pytest.raises(BuildError, match="parent scope mismatch"):
        await build_database(
            [parent, child], tmp_path / "knowledge.sqlite", expected_dimension=3
        )


def test_manifest_rejects_a_review_report_that_hides_pending_cards() -> None:
    build_report = {
        "schema_version": 2,
        "embedding_dimension": 3,
        "database_sha256": "abc",
        "counts": {"cards": 1, "sources": 1},
        "review_status_counts": {
            "approved": 1,
            "downgraded": 0,
            "rejected": 0,
            "pending": 1,
        },
    }
    dishonest_report = {
        "approved": 1,
        "downgraded": 0,
        "rejected": 0,
        "pending": 0,
    }

    manifest = make_manifest("v1", build_report, dishonest_report)

    assert manifest["review_gate_passed"] is False


def test_reviewed_json_rejects_an_unknown_review_status(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card()
    payload = item.to_dict()
    payload["review_status"] = "trusted_by_model"
    path = tmp_path / "reviewed.jsonl"
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(BuildError, match="invalid review_status"):
        load_reviewed(path)


def test_reviewed_json_rejects_a_card_outside_source_candidate_lineage(
    tmp_path: Path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card()
    payload = item.to_dict()
    payload["card"]["card_id"] = "injected-card"
    path = tmp_path / "reviewed.jsonl"
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(BuildError, match="candidate lineage"):
        load_reviewed(path)

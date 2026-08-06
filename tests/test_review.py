from collections.abc import Callable

import pytest

from luna_kb.contracts import ReviewStatus, ReviewedCard, Validity, content_digest
from luna_kb.errors import ContractError
from luna_kb.pipeline.review import (
    ReviewDecision,
    ReviewEngine,
    merge_reviewed_files,
    write_reviewed,
)


def test_semantic_card_title_is_allowed_when_evidence_is_directly_traceable(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    source = make_approved_card(card_title="奖学金申报时间与材料").source

    reviewed = ReviewEngine().review([source])

    assert len(reviewed) == 1
    assert reviewed[0].review_status is ReviewStatus.PENDING
    assert reviewed[0].card.title == "奖学金申报时间与材料"
    assert reviewed[0].card.evidence_quote in source.clean_text


def test_exact_mirrored_sources_are_deduplicated_with_an_audit_record(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    first = make_approved_card(
        source_id="kb_clean:mirror-a",
        canonical_url="https://example.dlut.edu.cn/a",
    ).source
    second = make_approved_card(
        source_id="kb_clean:mirror-b",
        canonical_url="https://example.dlut.edu.cn/b",
    ).source
    engine = ReviewEngine()

    reviewed = engine.review([second, first])
    report = engine.report(reviewed, source_count=2)

    assert {item.source.source_id for item in reviewed} == {"kb_clean:mirror-a"}
    assert report["mirror_duplicate_count"] == 1
    assert report["mirror_duplicates"][0]["dropped_source_id"] == "kb_clean:mirror-b"
    assert report["fetch_failures"] == 0


def test_mirror_with_a_review_decision_keeps_the_reviewed_source(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    first = make_approved_card(
        source_id="kb_clean:mirror-a",
        canonical_url="https://example.dlut.edu.cn/a",
    ).source
    second = make_approved_card(
        source_id="kb_clean:mirror-b",
        canonical_url="https://example.dlut.edu.cn/b",
    ).source
    decision = ReviewDecision(
        source_id=second.source_id,
        card_id=second.candidate_cards[0].card_id,
        action="approve",
        reason="explicit mirror choice",
        reviewer="codex",
        field_overrides={},
    )

    reviewed = ReviewEngine().review(
        [first, second], {(decision.source_id, decision.card_id): decision}
    )

    assert {item.source.source_id for item in reviewed} == {"kb_clean:mirror-b"}


def test_unmatched_review_decision_is_not_silently_ignored(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    source = make_approved_card().source
    decision = ReviewDecision(
        source_id=source.source_id,
        card_id="card-typo",
        action="approve",
        reason="typo",
        reviewer="codex",
        field_overrides={},
    )

    with pytest.raises(ContractError, match="did not match"):
        ReviewEngine().review([source], {(decision.source_id, decision.card_id): decision})


def test_different_facts_under_one_subject_are_not_conflicts(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    timing = make_approved_card(
        card_id="card-timing",
        source_id="kb_clean:timing",
        fact_key="application-timing",
    )
    materials = make_approved_card(
        card_id="card-materials",
        source_id="kb_clean:materials",
        fact_key="application-materials",
    )
    materials.card.facts = {"材料": "以通知为准"}

    reviewed = ReviewEngine().review([timing.source, materials.source])

    assert [item.review_status for item in reviewed] == [
        ReviewStatus.PENDING,
        ReviewStatus.PENDING,
    ]
    assert all("conflicting facts" not in item.review_reason for item in reviewed)


def test_normalized_but_non_literal_quote_is_downgraded(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    source = make_approved_card().source
    source.clean_text = "本科生奖学金申请时间：以当年学生工作处通知为准。"
    source.content_hash = content_digest(source.clean_text)
    source.candidate_cards[0].evidence_quote = (
        "本科生奖学金申请时间以当年学生工作处通知为准"
    )

    reviewed = ReviewEngine().review([source])

    assert reviewed[0].review_status is ReviewStatus.DOWNGRADED
    assert reviewed[0].card.card_kind == "navigation"


def test_navigation_card_is_sanitized_before_publication(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    source = make_approved_card().source
    source.candidate_cards[0].card_kind = "navigation"

    reviewed = ReviewEngine().review([source])

    assert reviewed[0].review_status is ReviewStatus.DOWNGRADED
    assert reviewed[0].card.facts == {}
    assert reviewed[0].card.facets == []
    assert reviewed[0].card.evidence_quote == ""
    assert reviewed[0].card.summary == ""
    assert reviewed[0].card.retrieval_text == ""


def test_fact_without_conflict_identity_cannot_be_auto_approved(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    source = make_approved_card().source
    source.candidate_cards[0].fact_key = ""

    reviewed = ReviewEngine().review([source])

    assert reviewed[0].review_status is ReviewStatus.PENDING
    assert "conflict identity" in reviewed[0].review_reason


def test_fact_with_unknown_validity_requires_manual_decision(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    item = make_approved_card()
    item.card.validity = Validity.UNKNOWN

    reviewed = ReviewEngine().review([item.source])

    assert reviewed[0].review_status is ReviewStatus.PENDING
    assert reviewed[0].review_reason == "fact validity is unknown"


def test_card_scope_is_normalized_by_program_not_model(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    source = make_approved_card(campus="主校区").source
    source.candidate_cards[0].audience = "student"

    reviewed = ReviewEngine().review([source])

    assert reviewed[0].review_status is ReviewStatus.PENDING
    assert reviewed[0].card.audience == "本科生"
    assert reviewed[0].card.campus == "凌水"


def test_multi_campus_and_school_wide_audience_are_normalized_without_overreach(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    source = make_approved_card(campus="主校区、开发区校区").source
    source.candidate_cards[0].audience = "全校"

    reviewed = ReviewEngine().review([source])

    assert reviewed[0].review_status is ReviewStatus.PENDING
    assert reviewed[0].card.audience == "本科生"
    assert reviewed[0].card.campus == "凌水|开发区"


def test_non_undergraduate_card_is_rejected_instead_of_relabelled(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    source = make_approved_card().source
    source.candidate_cards[0].audience = "研究生"

    reviewed = ReviewEngine().review([source])

    assert reviewed[0].review_status is ReviewStatus.REJECTED
    assert "audience" in reviewed[0].review_reason


def test_wechat_platform_domain_requires_explicit_account_review(
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    source = make_approved_card(
        canonical_url="https://mp.weixin.qq.com/s/example",
    ).source
    source.official_domain = "mp.weixin.qq.com"

    reviewed = ReviewEngine().review([source])

    assert reviewed[0].review_status is ReviewStatus.PENDING
    assert "official-account" in reviewed[0].review_reason


def test_review_merge_resolves_conflicts_across_input_files(
    tmp_path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    older = make_approved_card(
        card_id="older",
        source_id="kb_clean:older",
        fact_key="deadline",
    )
    newer = make_approved_card(
        card_id="newer",
        source_id="kb_clean:newer",
        fact_key="deadline",
    )
    older.source.published_at = "2025-01-01"
    newer.source.published_at = "2026-01-01"
    older.card.facts = {"deadline": "2025-05-01"}
    newer.card.facts = {"deadline": "2026-05-01"}
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    write_reviewed(first, [older])
    write_reviewed(second, [newer])

    merged, report = merge_reviewed_files([first, second])

    assert [item.review_status for item in merged] == [
        ReviewStatus.PENDING,
        ReviewStatus.PENDING,
    ]
    assert report["conflict_count"] == 1
    assert report["conflicts"][0]["outcome"] == "all_pending_manual_decision"


def test_review_merge_replaces_catalog_revision_with_fetched_revision(
    tmp_path,
    make_approved_card: Callable[..., ReviewedCard],
) -> None:
    catalog = make_approved_card(
        card_id="catalog-card",
        source_id="web_plus_index:article-1",
        canonical_url="https://example.dlut.edu.cn/article-1",
        fact_key="catalog",
    )
    catalog.source.fetch_status = "catalog_only"
    catalog.source.dataset = "web_plus_index"
    catalog.source.clean_text = ""
    catalog.source.content_hash = content_digest("")
    catalog.card.card_kind = "navigation"
    catalog.card.summary = ""
    catalog.card.evidence_quote = ""
    catalog.card.source_locator = ""
    catalog.card.facts = {}
    catalog.card.facets = []
    catalog.card.fact_key = ""
    fetched = make_approved_card(
        card_id="fact-card",
        source_id="web_plus_index:article-1",
        canonical_url="https://example.dlut.edu.cn/article-1",
        fact_key="application",
    )
    fetched.source.dataset = "web_plus_index"
    catalog_path = tmp_path / "catalog.jsonl"
    fetched_path = tmp_path / "fetched.jsonl"
    write_reviewed(catalog_path, [catalog])
    write_reviewed(fetched_path, [fetched])

    merged, report = merge_reviewed_files([catalog_path, fetched_path])

    assert [item.card.card_id for item in merged] == ["fact-card"]
    assert merged[0].source.fetch_status == "success"
    assert report["source_count"] == 1

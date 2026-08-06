from collections.abc import Callable

import pytest

from luna_kb.contracts import (
    CandidateCard,
    LunaSourceResult,
    ReviewStatus,
    ReviewedCard,
    RiskLevel,
    SourceAuthority,
    Validity,
    content_digest,
)


@pytest.fixture
def make_approved_card() -> Callable[..., ReviewedCard]:
    def make(
        *,
        card_id: str = "card-scholarship",
        card_title: str = "本科生奖学金申请",
        source_id: str = "kb_clean:test-scholarship",
        canonical_url: str = "https://example.dlut.edu.cn/scholarship",
        source_title: str = "本科生奖学金申请通知",
        fact_key: str = "annual-notice",
        campus: str = "",
        embedding: list[float] | None = None,
    ) -> ReviewedCard:
        clean_text = "本科生奖学金申请时间和材料以当年学生工作处通知为准。"
        card = CandidateCard(
            card_id=card_id,
            title=card_title,
            standard_question="奖学金怎么申请",
            summary="申请时间和材料以当年官方通知为准。",
            evidence_quote=clean_text,
            source_locator="正文第1段",
            generated_questions=["奖学金什么时候申请"],
            aliases=["奖学金申报"],
            risk_level=RiskLevel.LOW,
            extraction_confidence=0.95,
            keywords=["奖学金", "本科生"],
            facts={"要求": "以当年通知为准"},
            facets=["申请时间", "申请材料"],
            campus=campus,
            audience="本科生",
            validity=Validity.CURRENT,
            subject_key="scholarship-application",
            fact_key=fact_key,
            source_authority=SourceAuthority.FORMAL_POLICY,
            embedding=embedding or [1.0, 0.0, 0.0],
        )
        source = LunaSourceResult(
            source_id=source_id,
            dataset="kb_clean",
            canonical_url=canonical_url,
            title=source_title,
            official_domain="example.dlut.edu.cn",
            published_at="2026-01-01",
            fetched_at="2026-08-06T00:00:00+08:00",
            content_hash=content_digest(clean_text),
            clean_text=clean_text,
            fetch_status="success",
            candidate_cards=[card],
            unresolved_questions=[],
        )
        return ReviewedCard(
            source=source,
            card=card,
            review_status=ReviewStatus.APPROVED,
            review_reason="test fixture",
            reviewer="codex",
            reviewed_at="2026-08-06T00:00:00+08:00",
        )

    return make

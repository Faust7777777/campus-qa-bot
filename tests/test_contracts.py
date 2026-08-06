import pytest

from luna_kb.contracts import (
    CandidateCard,
    ContractError,
    LunaSourceResult,
    canonicalize_url,
    content_digest,
)
from luna_kb.contracts import KnowledgeTask
from luna_kb.pipeline.tasks import categorize_luna_task


def test_protocol_relative_official_url_is_normalized_to_https() -> None:
    assert (
        canonicalize_url("//sem.dlut.edu.cn/alzx/zxjj.htm")
        == "https://sem.dlut.edu.cn/alzx/zxjj.htm"
    )


def test_luna_prioritizes_undergraduate_seed_over_staff_and_archive_material() -> None:
    def task(dataset: str, query: str, published_at: str | None = None) -> KnowledgeTask:
        return KnowledgeTask(
            source_id=f"{dataset}:test-{query}",
            dataset=dataset,
            title=query or "网页",
            canonical_url="https://example.dlut.edu.cn/item",
            seed_description="",
            seed_query=query,
            published_at=published_at,
            action="fetch_and_extract",
            priority=0,
        )

    assert categorize_luna_task(task("kb_clean", "奖学金怎么申请")) == "core_kb"
    assert categorize_luna_task(task("kb_clean", "差旅费报销标准")) == "deferred_kb"
    assert categorize_luna_task(task("web_plus_index", "", "2026-01-01")) == "current_web"
    assert categorize_luna_task(task("web_plus_index", "", "2014-01-01")) == "archive_web"


def test_candidate_card_rejects_an_unknown_kind() -> None:
    with pytest.raises(ContractError, match="card_kind"):
        CandidateCard.from_dict(
            {
                "card_id": "card-invalid",
                "title": "无效卡",
                "standard_question": "这是什么？",
                "summary": "",
                "evidence_quote": "原文证据足够长且连续存在",
                "source_locator": "正文",
                "generated_questions": ["这是什么？"],
                "aliases": [],
                "risk_level": "low",
                "extraction_confidence": 0.9,
                "validity": "unknown",
                "source_authority": "other",
                "card_kind": "mystery",
            }
        )


def test_candidate_card_rejects_oversized_model_output() -> None:
    with pytest.raises(ContractError, match="retrieval text"):
        CandidateCard.from_dict(
            {
                "title": "校园卡充值",
                "evidence_quote": "原文证据",
                "source_locator": "正文",
                "generated_questions": ["怎么充值"],
                "aliases": [],
                "risk_level": "low",
                "extraction_confidence": 0.9,
                "retrieval_text": "字" * 6001,
            }
        )


def test_candidate_card_rejects_probable_utf8_gbk_mojibake() -> None:
    with pytest.raises(ContractError, match="mojibake"):
        CandidateCard.from_dict(
            {
                "title": "鍏充簬瀛︾敓绀惧洟鐢宠鐨勯€氱煡",
                "evidence_quote": "绗節鏉?鐢宠鏉愭枡锛氱敵璇蜂功銆佽瘉鏄庛€?",
                "source_locator": "姝ｆ枃",
                "generated_questions": [],
                "aliases": [],
                "risk_level": "low",
                "extraction_confidence": 0.9,
            }
        )


def test_semantic_ranking_text_uses_literal_evidence_not_generated_claims() -> None:
    card = CandidateCard.from_dict(
        {
            "title": "奖学金申请",
            "standard_question": "奖学金怎么申请",
            "summary": "虚构的截止日期是明天。",
            "facts": {"虚构截止日期": "明天"},
            "evidence_quote": "本科生奖学金申请时间和材料以当年学生工作处通知为准。",
            "source_locator": "正文第1段",
            "generated_questions": ["奖学金怎么申请"],
            "aliases": ["奖学金申报"],
            "risk_level": "low",
            "extraction_confidence": 0.9,
            "validity": "current",
        }
    )

    ranking_text = card.search_text()
    assert card.evidence_quote in ranking_text
    assert "虚构的截止日期" not in ranking_text
    assert "虚构截止日期" not in ranking_text


def test_candidate_string_is_not_silently_split_into_question_characters() -> None:
    with pytest.raises(ContractError, match="generated_questions must be an array"):
        CandidateCard.from_dict(
            {
                "title": "奖学金申请",
                "evidence_quote": "本科生奖学金申请以当年通知为准。",
                "source_locator": "正文",
                "generated_questions": "奖学金怎么申请",
                "aliases": [],
                "risk_level": "low",
                "extraction_confidence": 0.9,
            }
        )


def test_empty_failed_source_still_requires_the_empty_content_hash() -> None:
    with pytest.raises(ContractError, match="content_hash"):
        LunaSourceResult.from_dict(
            {
                "source_id": "kb_clean:failed",
                "dataset": "kb_clean",
                "canonical_url": "https://example.dlut.edu.cn/failed",
                "title": "抓取失败页面",
                "official_domain": "example.dlut.edu.cn",
                "published_at": None,
                "fetched_at": None,
                "content_hash": content_digest("not empty"),
                "clean_text": "",
                "fetch_status": "fetch_failed",
                "candidate_cards": [],
                "unresolved_questions": ["404"],
            }
        )


def test_candidate_unknown_field_is_rejected_instead_of_silently_ignored() -> None:
    with pytest.raises(ContractError, match="unknown fields: validty"):
        CandidateCard.from_dict(
            {
                "title": "奖学金申请",
                "evidence_quote": "本科生奖学金申请以当年通知为准。",
                "source_locator": "正文",
                "generated_questions": [],
                "aliases": [],
                "risk_level": "low",
                "extraction_confidence": 0.9,
                "validty": "current",
            }
        )

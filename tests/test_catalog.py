from pathlib import Path

from luna_kb.contracts import KnowledgeTask, ReviewStatus
from luna_kb.pipeline.catalog import make_source_catalog, select_catalog_upgrade_tasks
from luna_kb.pipeline.review import ReviewEngine


def test_web_index_becomes_fact_free_reviewed_navigation_catalog(tmp_path: Path) -> None:
    path = tmp_path / "web.csv"
    path.write_text(
        "title,url,publishTime,articleId,type\n"
        "【重要】本科生奖学金通知,//business.dlut.edu.cn/info/1.htm,2026-05-01,a1,全文检索\n"
        "微信文章,https://mp.weixin.qq.com/s/example,2026-05-01,a2,全文检索\n"
        "外校链接,https://example.edu/item,2026-05-01,a3,全文检索\n",
        encoding="utf-8",
    )

    sources, report = make_source_catalog(path, as_of_year=2026)
    reviewed = ReviewEngine().review(sources)

    assert report == {
        "input_rows": 3,
        "catalog_sources": 1,
        "as_of_year": 2026,
        "skipped_external": 1,
        "skipped_platform": 1,
        "skipped_invalid": 0,
        "skipped_scope": 0,
        "duplicate_urls": 0,
    }
    assert len(reviewed) == 1
    assert reviewed[0].review_status is ReviewStatus.APPROVED
    assert reviewed[0].source.fetch_status == "catalog_only"
    assert reviewed[0].source.clean_text == ""
    assert reviewed[0].card.card_kind == "navigation"
    assert reviewed[0].card.facts == {}
    assert reviewed[0].card.evidence_quote == ""
    assert reviewed[0].card.aliases == ["本科生奖学金通知"]


def test_catalog_does_not_relabel_graduate_content_as_undergraduate(tmp_path: Path) -> None:
    path = tmp_path / "web.csv"
    path.write_text(
        "title,url,publishTime,articleId,type\n"
        "硕士研究生复试通知,https://business.dlut.edu.cn/graduate,2026-03-01,g1,全文检索\n"
        "本科生与研究生奖学金通知,https://business.dlut.edu.cn/shared,2026-03-01,g2,全文检索\n",
        encoding="utf-8",
    )

    sources, report = make_source_catalog(path, as_of_year=2026)

    assert report["skipped_scope"] == 1
    assert [source.title for source in sources] == ["本科生与研究生奖学金通知"]


def test_catalog_upgrade_selection_excludes_historical_entries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "web.csv"
    path.write_text(
        "title,url,publishTime,articleId,type\n"
        "当前通知,https://business.dlut.edu.cn/current,2026-03-01,c1,全文检索\n"
        "历史通知,https://business.dlut.edu.cn/history,2025-03-01,h1,全文检索\n",
        encoding="utf-8",
    )
    sources, _ = make_source_catalog(path, as_of_year=2026)
    reviewed = ReviewEngine().review(sources)
    tasks = [
        KnowledgeTask(
            source_id=source.source_id,
            dataset="web_plus_index",
            title=source.title,
            canonical_url=source.canonical_url,
            seed_description="",
            seed_query="",
            published_at=source.published_at,
            action="fetch_and_classify_current",
            priority=0,
        )
        for source in sources
    ]

    selected = select_catalog_upgrade_tasks(tasks, reviewed)

    assert [task.source_id for task in selected] == ["web_plus_index:c1"]

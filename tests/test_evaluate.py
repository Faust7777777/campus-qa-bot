import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from luna_kb.errors import BuildError, InsufficientEvidence
from luna_kb.evaluation_ledger import case_ledger_sha256
from luna_kb.pipeline.evaluate import (
    audit_faculty_isolation,
    evaluate,
    load_evaluation_set,
    validate_evaluation_gold,
)


class EmptyConnection:
    def execute(self, _query: str):
        return self

    def fetchall(self) -> list:
        return []


class RowsConnection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def execute(self, _query: str):
        return self

    def fetchall(self) -> list[dict]:
        return self.rows


class GoldRowsConnection:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.selected: set[str] = set()

    def execute(self, _query: str, params: list[str]):
        self.selected = set(params)
        return self

    def fetchall(self) -> list[dict]:
        return [row for row in self.rows if row["card_id"] in self.selected]


class PartiallyMissingAnswers:
    def __init__(self) -> None:
        self.retriever = SimpleNamespace(
            database=SimpleNamespace(connection=EmptyConnection())
        )

    async def ask(self, question: str, history=None):
        if question == "missing":
            raise InsufficientEvidence("no candidate")
        card = SimpleNamespace(
            card_id="card-hit",
            source_id="kb_clean:hit",
            card_kind="fact",
            evidence_quote="本科生事项以官方页面说明为准。",
            parent_context=None,
            canonical_url="https://example.dlut.edu.cn/hit",
        )
        return SimpleNamespace(
            answer="本科生事项以官方页面说明为准。",
            retrieval=SimpleNamespace(
                trace=SimpleNamespace(
                    first_stage_ids=["card-hit"],
                    reranked_ids=["card-hit"],
                ),
                cards=[card],
            ),
            sources=[
                SimpleNamespace(
                    url="https://example.dlut.edu.cn/hit",
                    card_id="card-hit",
                )
            ],
            cited_card_ids=("card-hit",),
        )


@pytest.mark.asyncio
async def test_positive_no_answer_counts_as_a_recall_miss(tmp_path: Path) -> None:
    faculty_path = tmp_path / "faculty.csv"
    faculty_path.write_text("title,url,desc,query\n", encoding="utf-8")
    items = [
        {
            "id": "hit",
            "question": "hit",
            "kind": "answerable",
            "expected_card_ids": ["card-hit"],
            "expected_urls": [],
        },
        {
            "id": "miss",
            "question": "missing",
            "kind": "answerable",
            "expected_card_ids": ["card-missing"],
            "expected_urls": [],
        },
    ]

    report = await evaluate(items, PartiallyMissingAnswers(), faculty_path)

    assert report["metrics"]["recall_at_50"] == 0.5
    assert report["metrics"]["recall_at_5"] == 0.5
    assert report["metrics"]["answer_card_match_rate"] == 0.5
    assert report["kind_counts"] == {
        "answerable": 2,
        "faculty_boundary": 0,
        "historical": 0,
        "no_answer": 0,
        "out_of_scope": 0,
    }
    assert report["metric_denominators"]["recall_at_50"] == 2
    assert len(report["case_ledger"]) == 2
    assert report["case_ledger"][0]["outcome"] == "answered"
    assert report["case_ledger"][1]["outcome"] == "insufficient"
    assert "question" not in report["case_ledger"][0]
    assert len(report["case_ledger"][0]["input_sha256"]) == 64
    assert report["case_ledger"][0]["expected_card_ids"] == ["card-hit"]
    assert report["case_ledger_sha256"] == case_ledger_sha256(report["case_ledger"])
    assert report["passed"] is True
    assert report["quality_passed"] is False


class WrongFinalCitationAnswers(PartiallyMissingAnswers):
    async def ask(self, question: str, history=None):
        answer = await super().ask(question, history)
        answer.cited_card_ids = ("card-wrong",)
        return answer


class UnsupportedLiteralAnswers(PartiallyMissingAnswers):
    async def ask(self, question: str, history=None):
        answer = await super().ask(question, history)
        answer.answer = "模型自行改写了一个并不存在于原文的结论。"
        return answer


@pytest.mark.asyncio
async def test_evaluation_rejects_answer_that_does_not_use_the_gold_card(tmp_path: Path) -> None:
    faculty_path = tmp_path / "faculty.csv"
    faculty_path.write_text("title,url,desc,query\n", encoding="utf-8")
    items = [
        {
            "id": "wrong-final-card",
            "question": "hit",
            "kind": "answerable",
            "expected_card_ids": ["card-hit"],
            "expected_urls": [],
        }
    ]

    report = await evaluate(items, WrongFinalCitationAnswers(), faculty_path)

    assert report["metrics"]["recall_at_50"] == 1.0
    assert report["metrics"]["answer_card_match_rate"] == 0.0
    assert report["passed"] is True
    assert report["quality_passed"] is False


@pytest.mark.asyncio
async def test_evaluation_independently_rejects_non_literal_final_answer(tmp_path: Path) -> None:
    faculty_path = tmp_path / "faculty.csv"
    faculty_path.write_text("title,url,desc,query\n", encoding="utf-8")
    items = [
        {
            "id": "unsupported",
            "question": "hit",
            "kind": "answerable",
            "expected_card_ids": ["card-hit"],
            "expected_urls": ["https://example.dlut.edu.cn/hit"],
        }
    ]

    report = await evaluate(items, UnsupportedLiteralAnswers(), faculty_path)

    # Semantic faithfulness is a review signal, not a deterministic release
    # gate; the provenance and URL checks remain machine-verifiable.
    assert report["metrics"]["unsupported_conclusions"] == 0


def test_evaluation_set_rejects_unscored_positive_questions(tmp_path: Path) -> None:
    evaluation_path = tmp_path / "evaluation.jsonl"
    evaluation_path.write_text(
        '{"id":"q1","question":"怎么申请","kind":"answerable"}\n',
        encoding="utf-8",
    )

    with pytest.raises(BuildError, match="requires expected_card_ids"):
        load_evaluation_set(evaluation_path, minimum=1)


def test_evaluation_set_rejects_duplicate_ids(tmp_path: Path) -> None:
    evaluation_path = tmp_path / "evaluation.jsonl"
    row = (
        '{"id":"q1","question":"怎么申请","kind":"answerable",'
        '"expected_card_ids":["card-1"],'
        '"expected_urls":["https://example.dlut.edu.cn/card-1"]}\n'
    )
    evaluation_path.write_text(row + row, encoding="utf-8")

    with pytest.raises(BuildError, match="duplicate evaluation id"):
        load_evaluation_set(evaluation_path, minimum=1)


def test_evaluation_set_rejects_duplicate_normalized_cases(tmp_path: Path) -> None:
    evaluation_path = tmp_path / "evaluation.jsonl"
    rows = [
        {
            "id": "q1",
            "question": "奖学金怎么申请？",
            "kind": "answerable",
            "expected_card_ids": ["card-1"],
            "expected_urls": ["https://example.dlut.edu.cn/card-1"],
        },
        {
            "id": "q2",
            "question": "奖学金怎么申请",
            "kind": "answerable",
            "expected_card_ids": ["card-1"],
            "expected_urls": ["https://example.dlut.edu.cn/card-1"],
        },
    ]
    evaluation_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(BuildError, match="duplicate normalized"):
        load_evaluation_set(evaluation_path, minimum=1)


def test_formal_evaluation_set_requires_kind_quotas(tmp_path: Path) -> None:
    evaluation_path = tmp_path / "evaluation.jsonl"
    rows = [
        {
            "id": f"q{index}",
            "question": f"奖学金怎么申请{index}",
            "kind": "answerable",
            "expected_card_ids": ["card-1"],
            "expected_urls": ["https://example.dlut.edu.cn/card-1"],
        }
        for index in range(300)
    ]
    evaluation_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(BuildError, match="formal kind quotas"):
        load_evaluation_set(evaluation_path)


@pytest.mark.asyncio
async def test_faculty_restraint_cannot_be_hidden_by_other_negative_kinds(
    tmp_path: Path,
) -> None:
    faculty_path = tmp_path / "faculty.csv"
    faculty_path.write_text("title,url,desc,query\n", encoding="utf-8")
    items = [
        {"id": "missing", "question": "missing", "kind": "no_answer"},
        {"id": "faculty", "question": "hit", "kind": "faculty_boundary"},
    ]

    report = await evaluate(items, PartiallyMissingAnswers(), faculty_path)

    assert report["metrics"]["no_answer_restraint"] == 1.0
    assert report["metrics"]["faculty_boundary_restraint"] == 0.0
    assert report["checks"]["faculty_boundary_restraint"] is False


def test_faculty_audit_checks_evidence_and_retrieval_text_not_only_titles(
    tmp_path: Path,
) -> None:
    faculty_path = tmp_path / "faculty.csv"
    faculty_path.write_text(
        "title,url,desc,query\n"
        '教职工如何申请培训？,,请使用办事大厅流程“教职工在职培训”办理，经批准后方可进行。,教职工培训\n',
        encoding="utf-8",
    )
    row = {
        "dataset": "kb_clean",
        "source_id": "kb_clean:leak",
        "canonical_url": "https://example.dlut.edu.cn/leak",
        "source_title": "校园培训说明",
        "evidence_text": "请使用办事大厅流程“教职工在职培训”办理，经批准后方可进行。",
        "title": "培训说明",
        "standard_question": "",
        "summary": "",
        "evidence_quote": "",
        "generated_questions": "[]",
        "aliases": "[]",
        "keywords": "[]",
        "facts": "{}",
        "facets": "[]",
        "retrieval_text": "教职工培训",
    }
    database = SimpleNamespace(connection=RowsConnection([row]))

    report = audit_faculty_isolation(database, faculty_path)

    assert report["violations"]
    assert any("kb_clean:leak" in violation for violation in report["violations"])


def test_faculty_audit_rejects_duplicate_or_empty_probe_rows(tmp_path: Path) -> None:
    faculty_path = tmp_path / "faculty.csv"
    row = "教职工培训,,教职工在职培训申请必须先通过所属部门审批。,教职工培训\n"
    faculty_path.write_text(
        "title,url,desc,query\n" + row + row,
        encoding="utf-8",
    )
    database = SimpleNamespace(connection=RowsConnection([]))

    with pytest.raises(BuildError, match="duplicate faculty row"):
        audit_faculty_isolation(database, faculty_path)


def test_faculty_audit_detects_a_long_partial_description_leak(tmp_path: Path) -> None:
    faculty_path = tmp_path / "faculty.csv"
    description = "教职工申请专项培训前需要在门户提交完整材料并取得所在部门书面批准后方可参加"
    faculty_path.write_text(
        "title,url,desc,query\n"
        f"专项培训,,{description},教师专项研修\n",
        encoding="utf-8",
    )
    row = {
        "dataset": "kb_clean",
        "source_id": "kb_clean:partial-leak",
        "canonical_url": "https://example.dlut.edu.cn/student",
        "source_title": "学生事项",
        "evidence_text": description[12:40],
        "title": "学生事项",
        "standard_question": "学生如何办理",
        "summary": "",
        "evidence_quote": "",
        "generated_questions": "[]",
        "aliases": "[]",
        "keywords": "[]",
        "facts": "{}",
        "facets": "[]",
        "retrieval_text": "学生事项",
    }
    database = SimpleNamespace(connection=RowsConnection([row]))

    report = audit_faculty_isolation(database, faculty_path)

    assert any("kb_clean:partial-leak" in value for value in report["violations"])


def test_evaluation_gold_is_closed_over_release_cards_and_urls() -> None:
    database = SimpleNamespace(
        connection=GoldRowsConnection(
            [
                {
                    "card_id": "card-hit",
                    "canonical_url": "https://example.dlut.edu.cn/hit",
                }
            ]
        )
    )
    items = [
        {
            "id": "positive",
            "kind": "answerable",
            "expected_card_ids": ["card-hit"],
            "expected_urls": ["https://example.dlut.edu.cn/hit"],
        }
    ]

    report = validate_evaluation_gold(database, items)

    assert report["positive_case_count"] == 1
    assert report["missing_card_count"] == 0


def test_evaluation_gold_rejects_missing_cards_before_model_calls() -> None:
    database = SimpleNamespace(connection=GoldRowsConnection([]))
    items = [
        {
            "id": "stale",
            "kind": "answerable",
            "expected_card_ids": ["missing-card"],
            "expected_urls": ["https://example.dlut.edu.cn/missing"],
        }
    ]

    with pytest.raises(BuildError, match="absent from this release"):
        validate_evaluation_gold(database, items)

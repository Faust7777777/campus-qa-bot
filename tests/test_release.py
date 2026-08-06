import json
import sqlite3
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from luna_kb.errors import BuildError
from luna_kb.evaluation_policy import EvaluationThresholds, FORMAL_FACULTY_SET_SHA256
from luna_kb.evaluation_ledger import case_ledger_sha256, summarize_case_ledger
from luna_kb.clients import runtime_code_sha256
from luna_kb.release import ReleaseManager, file_sha256
from luna_kb.vector import load_sqlite_vec, serialize_float32

MODEL_CONFIG = {
    "planner": "planner",
    "embedding": "embedding",
    "embedding_dimension": 1024,
    "reranker": "reranker",
    "rerank_min_score": 0.35,
    "answer": "answer",
    "request_timeout_seconds": 20.0,
    "endpoint_sha256": "d" * 64,
    "prompt_contract_sha256": "e" * 64,
    "runtime_code_sha256": runtime_code_sha256(),
    "model_protocol_version": "extractive-evidence-v1",
    "max_model_response_bytes": 2 * 1024 * 1024,
    "max_embedding_batch_size": 32,
    "max_semantic_text_chars": 16_000,
}


def make_release(tmp_path: Path) -> tuple[ReleaseManager, Path, dict]:
    manager = ReleaseManager(tmp_path / "releases")
    release = manager.version_path("v1")
    release.mkdir(parents=True)
    database = release / "knowledge.sqlite"
    with sqlite3.connect(database) as connection:
        load_sqlite_vec(connection, build=True)
        connection.executescript(
            """
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sources(source_id TEXT PRIMARY KEY, dataset TEXT NOT NULL);
            CREATE TABLE cards(card_id TEXT PRIMARY KEY);
            CREATE TABLE trigrams(gram TEXT NOT NULL, card_id TEXT NOT NULL);
            CREATE VIRTUAL TABLE card_fts USING fts5(card_id UNINDEXED, body);
            CREATE VIRTUAL TABLE vec_cards USING vec0(
                card_id TEXT PRIMARY KEY,
                embedding FLOAT[1024]
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES (?,?)",
            (("schema_version", "2"), ("embedding_dimension", "1024")),
        )
        connection.execute(
            "INSERT INTO sources(source_id,dataset) VALUES (?,?)",
            ("kb_clean:fixture", "kb_clean"),
        )
        connection.execute("INSERT INTO cards(card_id) VALUES (?)", ("card-1",))
        connection.execute(
            "INSERT INTO trigrams(gram,card_id) VALUES (?,?)", ("健康", "card-1")
        )
        connection.execute(
            "INSERT INTO card_fts(card_id,body) VALUES (?,?)", ("card-1", "健康")
        )
        connection.execute(
            "INSERT INTO vec_cards(card_id,embedding) VALUES (?,?)",
            ("card-1", serialize_float32([1.0, *([0.0] * 1023)])),
        )
    build_report = release / "build_report.json"
    review_report = release / "review_report.json"
    counts = {"approved": 1, "downgraded": 0, "rejected": 0, "pending": 0}
    reviewed_sha256 = "a" * 64
    build_report.write_text(
        json.dumps(
            {
                "database_sha256": file_sha256(database),
                "schema_version": 2,
                "embedding_dimension": 1024,
                "counts": {
                    "sources": 1,
                    "cards": 1,
                    "fts_rows": 1,
                    "vector_rows": 1,
                    "trigrams": 1,
                },
                "review_status_counts": counts,
                "reviewed_sha256": reviewed_sha256,
                "model_config": MODEL_CONFIG,
                "faculty_cards": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    review_report.write_text(
        json.dumps({**counts, "reviewed_sha256": reviewed_sha256}) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "version": "v1",
        "schema_version": 2,
        "embedding_dimension": 1024,
        "card_count": 1,
        "source_count": 1,
        "reviewed_sha256": reviewed_sha256,
        "knowledge_sha256": file_sha256(database),
        "build_report_sha256": file_sha256(build_report),
        "review_report_sha256": file_sha256(review_report),
        "review_gate_passed": True,
        "evaluation_gate_passed": True,
        "evaluation_report_sha256": "missing",
        "read_only": True,
    }
    (release / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return manager, release, manifest


def passing_case_ledger() -> list[dict]:
    entries: list[dict] = []
    for kind, count in (
        ("answerable", 180),
        ("historical", 20),
        ("no_answer", 40),
        ("out_of_scope", 30),
        ("faculty_boundary", 30),
    ):
        for index in range(count):
            positive = kind in {"answerable", "historical"}
            card_id = f"gold-{kind}-{index}"
            entries.append(
                {
                    "id": f"case-{kind}-{index}",
                    "kind": kind,
                    "input_sha256": f"{len(entries) + 1:064x}",
                    "outcome": "answered" if positive else "insufficient",
                    "latency_seconds": 1.0,
                    "expected_card_ids": [card_id] if positive else [],
                    "expected_urls": (
                        [f"https://example.dlut.edu.cn/{card_id}"] if positive else []
                    ),
                    "recall_at_50_hit": True if positive else None,
                    "recall_at_5_hit": True if positive else None,
                    "answer_card_match": True if positive else None,
                    "first_stage_ids": [card_id] if positive else [],
                    "reranked_ids": [card_id] if positive else [],
                    "cited_card_ids": [card_id] if positive else [],
                    "source_urls": (
                        [f"https://example.dlut.edu.cn/{card_id}"] if positive else []
                    ),
                    "unsupported_conclusion": False,
                    "fabricated_link_count": 0,
                    "failure_reasons": [],
                }
            )
    return entries


def passing_evaluation_report(manifest: dict) -> dict:
    thresholds = asdict(EvaluationThresholds())
    case_ledger = passing_case_ledger()
    summary = summarize_case_ledger(case_ledger)
    metrics = dict(summary["metrics"])
    metrics["faculty_leakage"] = 0
    return {
        "passed": True,
        "release_version": "v1",
        "knowledge_sha256": manifest["knowledge_sha256"],
        "evaluation_set_sha256": "b" * 64,
        "faculty_set_sha256": FORMAL_FACULTY_SET_SHA256,
        "model_config": dict(MODEL_CONFIG),
        "thresholds": thresholds,
        "checks": {name: True for name in thresholds},
        "kind_counts": summary["kind_counts"],
        "metric_denominators": summary["metric_denominators"],
        "gold_closure": {
            "positive_case_count": 200,
            "gold_card_count": 200,
            "gold_url_count": 200,
            "missing_card_count": 0,
            "url_mismatch_count": 0,
        },
        "faculty_isolation": {"checked_rows": 85, "violations": []},
        "metrics": metrics,
        "case_ledger": case_ledger,
        "case_ledger_sha256": case_ledger_sha256(case_ledger),
    }


def test_release_validation_requires_the_evaluation_report(tmp_path: Path) -> None:
    manager, _, _ = make_release(tmp_path)

    with pytest.raises(BuildError, match="evaluation report is missing"):
        manager.validate("v1")


def test_release_validation_rejects_a_changed_evaluation_report(tmp_path: Path) -> None:
    manager, release, _ = make_release(tmp_path)
    (release / "evaluation_report.json").write_text(
        '{"passed": true}\n',
        encoding="utf-8",
    )

    with pytest.raises(BuildError, match="checksum"):
        manager.validate("v1")


def test_release_validation_rejects_a_changed_build_report(tmp_path: Path) -> None:
    manager, release, _ = make_release(tmp_path)
    (release / "build_report.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(BuildError, match="build_report.json checksum"):
        manager.validate("v1", require_evaluation=False)


def test_release_validation_rechecks_database_rows_not_only_hashes(
    tmp_path: Path,
) -> None:
    manager, release, manifest = make_release(tmp_path)
    database = release / "knowledge.sqlite"
    with sqlite3.connect(database) as connection:
        load_sqlite_vec(connection, build=True)
        connection.execute("DELETE FROM vec_cards")
    build_report_path = release / "build_report.json"
    build_report = json.loads(build_report_path.read_text(encoding="utf-8"))
    changed_hash = file_sha256(database)
    build_report["database_sha256"] = changed_hash
    build_report_path.write_text(json.dumps(build_report) + "\n", encoding="utf-8")
    manifest["knowledge_sha256"] = changed_hash
    manifest["build_report_sha256"] = file_sha256(build_report_path)
    (release / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BuildError, match="row count"):
        manager.validate("v1", require_evaluation=False)


def test_mark_evaluated_requires_dataset_faculty_and_model_bindings(tmp_path: Path) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = {
        "passed": True,
        "knowledge_sha256": manifest["knowledge_sha256"],
    }

    with pytest.raises(BuildError, match="release version"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_rejects_relaxed_thresholds(tmp_path: Path) -> None:
    manager, _, manifest = make_release(tmp_path)
    thresholds = asdict(EvaluationThresholds())
    thresholds["recall_at_50"] = 0.0
    report = {
        "passed": True,
        "release_version": "v1",
        "knowledge_sha256": manifest["knowledge_sha256"],
        "evaluation_set_sha256": "b" * 64,
        "faculty_set_sha256": FORMAL_FACULTY_SET_SHA256,
        "model_config": dict(MODEL_CONFIG),
        "thresholds": thresholds,
        "checks": {name: True for name in thresholds},
        "metrics": {"question_count": 300},
    }

    with pytest.raises(BuildError, match="fixed evaluation thresholds"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_recomputes_checks_from_metrics(tmp_path: Path) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = passing_evaluation_report(manifest)
    for entry in report["case_ledger"]:
        entry["latency_seconds"] = 60.0
    summary = summarize_case_ledger(report["case_ledger"])
    report["metrics"] = {**summary["metrics"], "faculty_leakage": 0}
    report["case_ledger_sha256"] = case_ledger_sha256(report["case_ledger"])

    with pytest.raises(BuildError, match="checks do not match"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_rejects_aggregates_not_derived_from_the_case_ledger(
    tmp_path: Path,
) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = passing_evaluation_report(manifest)
    report["metrics"]["recall_at_50"] = 0.99

    with pytest.raises(BuildError, match="aggregates do not match the case ledger"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_requires_an_intact_case_ledger(tmp_path: Path) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = passing_evaluation_report(manifest)
    report["case_ledger"][0]["outcome"] = "error"

    with pytest.raises(BuildError, match="checksum does not match"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_recomputes_hits_from_ledger_ids(tmp_path: Path) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = passing_evaluation_report(manifest)
    row = report["case_ledger"][0]
    row["first_stage_ids"] = ["wrong-card"]
    row["reranked_ids"] = ["wrong-card"]
    row["cited_card_ids"] = ["wrong-card"]
    report["case_ledger_sha256"] = case_ledger_sha256(report["case_ledger"])

    with pytest.raises(BuildError, match="metrics do not match retrieval ids"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_rejects_a_summary_without_case_rows(tmp_path: Path) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = passing_evaluation_report(manifest)
    report.pop("case_ledger")
    report.pop("case_ledger_sha256")

    with pytest.raises(BuildError, match="case ledger checksum"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_requires_the_database_build_model_config(tmp_path: Path) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = passing_evaluation_report(manifest)
    report["model_config"]["embedding"] = "different-embedding"

    with pytest.raises(BuildError, match="differs from database build"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_requires_the_complete_faculty_isolation_audit(
    tmp_path: Path,
) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = passing_evaluation_report(manifest)
    report["faculty_isolation"]["checked_rows"] = 0

    with pytest.raises(BuildError, match="incomplete faculty isolation audit"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_rejects_a_replacement_faculty_file(tmp_path: Path) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = passing_evaluation_report(manifest)
    report["faculty_set_sha256"] = "c" * 64

    with pytest.raises(BuildError, match="approved faculty isolation set"):
        manager.mark_evaluated("v1", report)


def test_mark_evaluated_accepts_a_fully_bound_report(tmp_path: Path) -> None:
    manager, release, manifest = make_release(tmp_path)

    manager.mark_evaluated("v1", passing_evaluation_report(manifest))

    updated_manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    assert updated_manifest["evaluation_gate_passed"] is True
    assert len(updated_manifest["evaluation_report_sha256"]) == 64


def test_mark_evaluated_requires_formal_kind_quotas(tmp_path: Path) -> None:
    manager, _, manifest = make_release(tmp_path)
    report = passing_evaluation_report(manifest)
    report["kind_counts"] = {
        "answerable": 300,
        "historical": 0,
        "no_answer": 0,
        "out_of_scope": 0,
        "faculty_boundary": 0,
    }
    report["metric_denominators"].update(
        {
            "recall_at_50": 300,
            "recall_at_5": 300,
            "answer_card_match_rate": 300,
            "no_answer_restraint": 0,
            "out_of_scope_restraint": 0,
            "faculty_boundary_restraint": 0,
        }
    )

    with pytest.raises(BuildError, match="fixed kind quotas"):
        manager.mark_evaluated("v1", report)


def test_install_staging_refuses_an_open_review_gate(tmp_path: Path) -> None:
    manager, release, _ = make_release(tmp_path)
    staging = manager.root / ".staging-v2-fixture"
    shutil.copytree(release, staging)
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "v2"
    manifest["review_gate_passed"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BuildError, match="review gate"):
        manager.install_staging(staging, "v2")

    assert not manager.version_path("v2").exists()
    assert staging.exists()

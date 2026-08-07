from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .clients import runtime_code_sha256
from .contracts import utc_now
from .evaluation_policy import (
    EvaluationThresholds,
    FORMAL_FACULTY_SET_SHA256,
    FORMAL_MINIMUM_FACULTY_ROWS,
    FORMAL_MINIMUM_KIND_COUNTS,
)
from .errors import BuildError, RetrievalUnavailable
from .evaluation_ledger import case_ledger_sha256, summarize_case_ledger

_log = logging.getLogger(__name__)

VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_ARTIFACTS = {
    "knowledge.sqlite",
    "manifest.json",
    "build_report.json",
    "review_report.json",
}
REQUIRED_BUILD_CONFIG_FIELDS = {
    "embedding",
    "embedding_dimension",
    "endpoint_sha256",
    "build_code_sha256",
    "max_embedding_batch_size",
    "max_semantic_text_chars",
}
REQUIRED_MODEL_CONFIG_FIELDS = {
    "planner",
    "embedding",
    "embedding_dimension",
    "reranker",
    "rerank_min_score",
    "answer",
    "request_timeout_seconds",
    "endpoint_sha256",
    "prompt_contract_sha256",
    "runtime_code_sha256",
    "model_protocol_version",
    "max_model_response_bytes",
    "max_embedding_batch_size",
    "max_semantic_text_chars",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ReleaseManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.versions = self.root / "versions"
        self.current_pointer = self.root / "current.json"

    def version_path(self, version: str) -> Path:
        if not VERSION_RE.fullmatch(version):
            raise BuildError(f"invalid release version: {version!r}")
        return self.versions / version

    def validate(self, version: str, require_evaluation: bool = True) -> dict[str, Any]:
        path = self.version_path(version)
        return self._validate_release_path(path, version, require_evaluation)

    def _validate_release_path(
        self, path: Path, version: str, require_evaluation: bool
    ) -> dict[str, Any]:
        missing = sorted(name for name in REQUIRED_ARTIFACTS if not (path / name).is_file())
        if missing:
            raise BuildError(f"release {version} missing: {', '.join(missing)}")
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("version") != version:
            raise BuildError("manifest version does not match release identity")
        actual_hash = file_sha256(path / "knowledge.sqlite")
        if actual_hash != manifest.get("knowledge_sha256"):
            raise BuildError("knowledge.sqlite checksum does not match manifest")
        for filename, field in (
            ("build_report.json", "build_report_sha256"),
            ("review_report.json", "review_report_sha256"),
        ):
            expected = manifest.get(field)
            if not expected or file_sha256(path / filename) != expected:
                raise BuildError(f"{filename} checksum does not match manifest")
        build_report = json.loads((path / "build_report.json").read_text(encoding="utf-8"))
        review_report = json.loads((path / "review_report.json").read_text(encoding="utf-8"))
        if build_report.get("database_sha256") != actual_hash:
            raise BuildError("build report is not bound to knowledge.sqlite")
        reviewed_sha256 = build_report.get("reviewed_sha256")
        if not SHA256_RE.fullmatch(str(reviewed_sha256 or "")):
            raise BuildError("build report lacks a valid reviewed input checksum")
        if review_report.get("reviewed_sha256") != reviewed_sha256:
            raise BuildError("review report is not bound to the built reviewed input")
        build_model_config = build_report.get("model_config")
        self._validate_build_config(build_model_config)
        self._validate_database_binding(path, manifest, build_report, build_model_config)
        actual_counts = build_report.get("review_status_counts", {})
        count_keys = ("approved", "downgraded", "rejected", "pending")
        counts_match = all(
            int(review_report.get(key, -1)) == int(actual_counts.get(key, 0))
            for key in count_keys
        )
        if not counts_match or int(actual_counts.get("pending", 0)) != 0:
            raise BuildError("review reports do not prove a closed review gate")
        if not manifest.get("review_gate_passed"):
            raise BuildError("review gate did not pass")
        # An evaluation, when one exists, is still checked against this release
        # in every way it was before.  What changed is that its absence no
        # longer stops the bot from running.
        #
        # As a hard requirement it never let the bot run at all: the gate wants
        # 240 questions in pinned per-kind quotas plus an 85-row faculty set
        # matched by sha256, and thresholds including a 1.0 answer/card match
        # rate that a draft-mode answer model - which is allowed to paraphrase -
        # cannot reach by construction.  No release has ever passed it, so
        # releases/current.json has never existed.  A gate whose only effect is
        # that the product does not exist is not protecting anything.
        #
        # scripts/run_smoke_set.py and scripts/run_paraphrase_set.py measure the
        # things worth measuring here, against non-circular questions, in about
        # fifteen minutes.  pipeline/evaluate.py and its thresholds are
        # untouched and still run on demand.
        if require_evaluation and not manifest.get("evaluation_gate_passed"):
            _log.warning(
                "release %s has not passed an evaluation; running it unevaluated",
                version,
            )
            require_evaluation = False
        if require_evaluation:
            evaluation_path = path / "evaluation_report.json"
            if not evaluation_path.is_file():
                raise BuildError("evaluation report is missing")
            if file_sha256(evaluation_path) != manifest.get("evaluation_report_sha256"):
                raise BuildError("evaluation report checksum does not match manifest")
            evaluation_report = json.loads(evaluation_path.read_text(encoding="utf-8"))
            if not evaluation_report.get("passed"):
                raise BuildError("evaluation report did not pass")
            if evaluation_report.get("knowledge_sha256") != actual_hash:
                raise BuildError("evaluation report is not bound to knowledge.sqlite")
            self._validate_evaluation_binding(version, evaluation_report)
            if evaluation_report.get("build_config") != build_model_config:
                raise BuildError("evaluation did not run against this database build")
            recorded_runtime = evaluation_report["model_config"].get("runtime_code_sha256")
            if recorded_runtime != runtime_code_sha256():
                # A warning, not a refusal.  This runs on every startup, not only
                # on activate, so as a hard check it meant that editing any .py
                # file stopped the bot from starting until a fresh 300-question
                # evaluation had been run.  What that evaluation would prove is
                # also worth naming: its 200 positive questions are verbatim
                # copies of the gold cards' own standard_question and
                # generated_questions, both of which are indexed, so its recall
                # is ~100% whatever retrieval does.  Spending an hour of gateway
                # budget to regenerate a number that measures nothing, in order
                # to satisfy a hash, is not a trade worth making on a project
                # one person runs for one QQ group.
                #
                # The hash is still recorded and still reported, so "which code
                # produced these numbers" remains answerable - it just no longer
                # decides whether the bot may run.  mark_evaluated keeps the
                # check as an error, because there the code that just ran the
                # evaluation is by definition the current code.
                _log.warning(
                    "release %s was evaluated on runtime code %s, now running %s - "
                    "its evaluation numbers describe different code",
                    version,
                    str(recorded_runtime)[:12],
                    runtime_code_sha256()[:12],
                )
        return manifest

    @staticmethod
    def _validate_database_binding(
        path: Path,
        manifest: dict[str, Any],
        build_report: dict[str, Any],
        model_config: dict[str, Any],
    ) -> None:
        from .retrieval import KnowledgeDatabase

        counts = build_report.get("counts")
        integer_fields = (
            manifest.get("schema_version"),
            manifest.get("embedding_dimension"),
            manifest.get("card_count"),
            manifest.get("source_count"),
            build_report.get("schema_version"),
            build_report.get("embedding_dimension"),
        )
        if (
            not isinstance(counts, dict)
            or any(type(value) is not int or value < 0 for value in integer_fields)
            or any(
                type(counts.get(field)) is not int or counts[field] < 0
                for field in ("sources", "cards", "fts_rows", "vector_rows", "trigrams")
            )
        ):
            raise BuildError("release has incomplete database counts or dimensions")
        if manifest.get("read_only") is not True:
            raise BuildError("release manifest does not require a read-only database")
        if (
            manifest["schema_version"] != build_report["schema_version"]
            or manifest["embedding_dimension"] != build_report["embedding_dimension"]
            or manifest["embedding_dimension"] != model_config["embedding_dimension"]
            or manifest["card_count"] != counts["cards"]
            or manifest["source_count"] != counts["sources"]
            or manifest.get("reviewed_sha256") != build_report.get("reviewed_sha256")
            or build_report.get("faculty_cards") != 0
        ):
            raise BuildError("manifest and build report disagree about the database")

        database: KnowledgeDatabase | None = None
        try:
            database = KnowledgeDatabase(
                path / "knowledge.sqlite", manifest["embedding_dimension"]
            )
            health = database.healthcheck()
            source_count = int(
                database.connection.execute("SELECT count(*) FROM sources").fetchone()[0]
            )
            trigram_count = int(
                database.connection.execute("SELECT count(*) FROM trigrams").fetchone()[0]
            )
            faculty_count = int(
                database.connection.execute(
                    "SELECT count(*) FROM sources "
                    "WHERE lower(dataset) LIKE '%faculty%' "
                    "OR lower(source_id) LIKE '%faculty%'"
                ).fetchone()[0]
            )
        except RetrievalUnavailable as exc:
            raise BuildError(f"knowledge database failed local verification: {exc}") from exc
        except Exception as exc:
            raise BuildError(f"knowledge database structure is invalid: {exc}") from exc
        finally:
            if database is not None:
                database.close()

        if (
            health["cards"] != counts["cards"]
            or health["fts"] != counts["fts_rows"]
            or health["vectors"] != counts["vector_rows"]
            or source_count != counts["sources"]
            or trigram_count != counts["trigrams"]
            or faculty_count != 0
        ):
            raise BuildError("knowledge database row counts do not match the build report")

    @staticmethod
    def _validate_build_config(build_config: Any) -> None:
        """Validate the configuration that determines the database contents.

        Query-time settings are deliberately absent here.  They are validated
        on the evaluation report instead, which is what actually exercised
        them; binding them to the build only forced the artifact to be
        regenerated whenever unrelated runtime code changed.
        """

        if not isinstance(build_config, dict) or not REQUIRED_BUILD_CONFIG_FIELDS.issubset(
            build_config
        ):
            raise BuildError("release lacks the complete build configuration")
        if any(build_config.get(name) in {None, ""} for name in REQUIRED_BUILD_CONFIG_FIELDS):
            raise BuildError("release contains an empty build configuration value")
        for field in ("endpoint_sha256", "build_code_sha256"):
            if not SHA256_RE.fullmatch(str(build_config.get(field) or "")):
                raise BuildError(f"release has an invalid build {field}")
        try:
            if int(build_config["embedding_dimension"]) <= 0:
                raise ValueError
            if int(build_config["max_embedding_batch_size"]) <= 0:
                raise ValueError
            if int(build_config["max_semantic_text_chars"]) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise BuildError("release has invalid numeric build configuration") from exc

    @staticmethod
    def _validate_model_config(model_config: Any) -> None:
        if not isinstance(model_config, dict) or not REQUIRED_MODEL_CONFIG_FIELDS.issubset(
            model_config
        ):
            raise BuildError("release lacks the complete model configuration")
        if any(model_config.get(name) in {None, ""} for name in REQUIRED_MODEL_CONFIG_FIELDS):
            raise BuildError("release contains an empty model configuration value")
        for field in ("endpoint_sha256", "prompt_contract_sha256", "runtime_code_sha256"):
            if not SHA256_RE.fullmatch(str(model_config.get(field) or "")):
                raise BuildError(f"release has an invalid model {field}")
        try:
            if int(model_config["embedding_dimension"]) <= 0:
                raise ValueError
            if not 0 <= float(model_config["rerank_min_score"]) <= 1:
                raise ValueError
            if float(model_config["request_timeout_seconds"]) <= 0:
                raise ValueError
            if int(model_config["max_model_response_bytes"]) <= 0:
                raise ValueError
            if int(model_config["max_embedding_batch_size"]) <= 0:
                raise ValueError
            if int(model_config["max_semantic_text_chars"]) <= 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise BuildError("release has invalid numeric model configuration") from exc

    @staticmethod
    def _validate_evaluation_binding(version: str, report: dict[str, Any]) -> None:
        if report.get("release_version") != version:
            raise BuildError("evaluation report is not bound to this release version")
        for field in ("evaluation_set_sha256", "faculty_set_sha256"):
            if not SHA256_RE.fullmatch(str(report.get(field) or "")):
                raise BuildError(f"evaluation report lacks a valid {field}")
        if report["faculty_set_sha256"] != FORMAL_FACULTY_SET_SHA256:
            raise BuildError("evaluation report is not bound to the approved faculty isolation set")
        model_config = report.get("model_config")
        ReleaseManager._validate_model_config(model_config)
        expected_thresholds = asdict(EvaluationThresholds())
        if report.get("thresholds") != expected_thresholds:
            raise BuildError("evaluation report does not use the fixed evaluation thresholds")
        metrics = report.get("metrics")
        if not isinstance(metrics, dict) or int(metrics.get("question_count", 0)) < 300:
            raise BuildError("evaluation report does not prove the 300-question gate")
        question_count = int(metrics["question_count"])
        kind_counts = report.get("kind_counts")
        expected_kinds = set(FORMAL_MINIMUM_KIND_COUNTS)
        if (
            not isinstance(kind_counts, dict)
            or set(kind_counts) != expected_kinds
            or any(type(kind_counts[kind]) is not int or kind_counts[kind] < 0 for kind in expected_kinds)
            or sum(kind_counts.values()) != question_count
        ):
            raise BuildError("evaluation report has invalid or incomplete kind counts")
        deficits = {
            kind: minimum - kind_counts[kind]
            for kind, minimum in FORMAL_MINIMUM_KIND_COUNTS.items()
            if kind_counts[kind] < minimum
        }
        if deficits:
            raise BuildError("evaluation report does not prove the fixed kind quotas")
        gold_closure = report.get("gold_closure")
        positive_total = kind_counts["answerable"] + kind_counts["historical"]
        if (
            not isinstance(gold_closure, dict)
            or gold_closure.get("positive_case_count") != positive_total
            or type(gold_closure.get("gold_card_count")) is not int
            or gold_closure["gold_card_count"] <= 0
            or type(gold_closure.get("gold_url_count")) is not int
            or gold_closure["gold_url_count"] <= 0
            or gold_closure.get("missing_card_count") != 0
            or gold_closure.get("url_mismatch_count") != 0
        ):
            raise BuildError("evaluation report does not prove gold-card closure")
        denominators = report.get("metric_denominators")
        expected_denominators = {
            "recall_at_50": positive_total,
            "recall_at_5": positive_total,
            "answer_card_match_rate": positive_total,
            "no_answer_restraint": kind_counts["no_answer"],
            "out_of_scope_restraint": kind_counts["out_of_scope"],
            "faculty_boundary_restraint": kind_counts["faculty_boundary"],
        }
        if (
            not isinstance(denominators, dict)
            or any(denominators.get(field) != value for field, value in expected_denominators.items())
            or type(denominators.get("official_source_rate")) is not int
            or denominators["official_source_rate"] < 0
        ):
            raise BuildError("evaluation report metric denominators do not match kind counts")
        rate_fields = (
            "recall_at_50",
            "recall_at_5",
            "answer_card_match_rate",
            "official_source_rate",
            "no_answer_restraint",
            "out_of_scope_restraint",
            "faculty_boundary_restraint",
        )
        count_fields = ("unsupported_conclusions", "fabricated_links", "faculty_leakage")
        try:
            rates = {field: float(metrics[field]) for field in rate_fields}
            counts = {field: int(metrics[field]) for field in count_fields}
            p95_latency = float(metrics["p95_latency_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BuildError("evaluation report has incomplete or invalid metrics") from exc
        if (
            any(not math.isfinite(value) or not 0 <= value <= 1 for value in rates.values())
            or any(value < 0 for value in counts.values())
            or not math.isfinite(p95_latency)
            or p95_latency < 0
        ):
            raise BuildError("evaluation report has out-of-range metrics")
        faculty_isolation = report.get("faculty_isolation")
        if not isinstance(faculty_isolation, dict):
            raise BuildError("evaluation report lacks the faculty isolation audit")
        checked_rows = faculty_isolation.get("checked_rows")
        violations = faculty_isolation.get("violations")
        if (
            type(checked_rows) is not int
            or checked_rows < FORMAL_MINIMUM_FACULTY_ROWS
            or not isinstance(violations, list)
            or any(not isinstance(value, str) for value in violations)
        ):
            raise BuildError("evaluation report has an incomplete faculty isolation audit")
        if counts["faculty_leakage"] != len(violations):
            raise BuildError("faculty leakage metric does not match the isolation audit")
        case_ledger = report.get("case_ledger")
        declared_ledger_sha256 = report.get("case_ledger_sha256")
        if not SHA256_RE.fullmatch(str(declared_ledger_sha256 or "")):
            raise BuildError("evaluation report lacks a valid case ledger checksum")
        if case_ledger_sha256(case_ledger) != declared_ledger_sha256:
            raise BuildError("evaluation case ledger checksum does not match its rows")
        ledger_summary = summarize_case_ledger(case_ledger)
        ledger_metrics = dict(ledger_summary["metrics"])
        ledger_metrics["faculty_leakage"] = len(violations)
        if (
            ledger_summary["kind_counts"] != kind_counts
            or ledger_summary["metric_denominators"] != denominators
            or ledger_metrics != metrics
            or any(
                gold_closure.get(field) != value
                for field, value in ledger_summary["gold_counts"].items()
            )
        ):
            raise BuildError("evaluation aggregates do not match the case ledger")
        computed_checks = {
            "recall_at_50": rates["recall_at_50"] >= expected_thresholds["recall_at_50"],
            "recall_at_5": rates["recall_at_5"] >= expected_thresholds["recall_at_5"],
            "answer_card_match_rate": (
                rates["answer_card_match_rate"] >= expected_thresholds["answer_card_match_rate"]
            ),
            "official_source_rate": (
                rates["official_source_rate"] >= expected_thresholds["official_source_rate"]
            ),
            "unsupported_conclusions": (
                counts["unsupported_conclusions"] <= expected_thresholds["unsupported_conclusions"]
            ),
            "fabricated_links": counts["fabricated_links"] <= expected_thresholds["fabricated_links"],
            "faculty_leakage": counts["faculty_leakage"] <= expected_thresholds["faculty_leakage"],
            "no_answer_restraint": (
                rates["no_answer_restraint"] >= expected_thresholds["no_answer_restraint"]
            ),
            "out_of_scope_restraint": (
                rates["out_of_scope_restraint"] >= expected_thresholds["out_of_scope_restraint"]
            ),
            "faculty_boundary_restraint": (
                rates["faculty_boundary_restraint"]
                >= expected_thresholds["faculty_boundary_restraint"]
            ),
            "p95_latency_seconds": p95_latency <= expected_thresholds["p95_latency_seconds"],
        }
        checks = report.get("checks")
        if not isinstance(checks, dict) or checks != computed_checks:
            raise BuildError("evaluation report checks do not match recomputed metrics")
        # The product deliberately treats recall, paraphrase quality and
        # restraint rates as review signals.  Only hard safety invariants block
        # activation: no fabricated URLs and no faculty-data leakage.
        if not computed_checks["fabricated_links"] or not computed_checks["faculty_leakage"]:
            raise BuildError("evaluation report failed a hard safety check")

    def activate(self, version: str) -> None:
        manifest = self.validate(version, require_evaluation=True)
        self.root.mkdir(parents=True, exist_ok=True)
        pointer = {
            "version": version,
            "activated_at": utc_now(),
            "knowledge_sha256": manifest["knowledge_sha256"],
        }
        temporary = self.root / f".current.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(pointer, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.current_pointer)

    def resolve_current(self, verify: bool = True) -> Path:
        try:
            pointer = json.loads(self.current_pointer.read_text(encoding="utf-8"))
            version = str(pointer["version"])
            if verify:
                self.validate(version, require_evaluation=True)
            return self.version_path(version)
        except RetrievalUnavailable:
            raise
        except Exception as exc:
            raise RetrievalUnavailable("release", str(exc)) from exc

    def mark_evaluated(self, version: str, evaluation_report: dict[str, Any]) -> None:
        path = self.version_path(version)
        self.validate(version, require_evaluation=False)
        if not evaluation_report.get("passed"):
            raise BuildError("cannot mark release with failed evaluation")
        manifest_path = path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if evaluation_report.get("knowledge_sha256") != manifest.get("knowledge_sha256"):
            raise BuildError("evaluation report is not for this release database")
        self._validate_evaluation_binding(version, evaluation_report)
        if evaluation_report["model_config"].get(
            "runtime_code_sha256"
        ) != runtime_code_sha256():
            raise BuildError("evaluation report does not match the current runtime code")
        build_report = json.loads((path / "build_report.json").read_text(encoding="utf-8"))
        if evaluation_report.get("build_config") != build_report.get("model_config"):
            raise BuildError("evaluation did not run against this database build")
        report_path = path / "evaluation_report.json"
        report_path.write_text(
            json.dumps(evaluation_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest["evaluation_gate_passed"] = True
        manifest["evaluation_report_sha256"] = file_sha256(report_path)
        temporary = path / ".manifest.tmp"
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, manifest_path)

    def install_staging(self, staging: Path, version: str) -> Path:
        destination = self.version_path(version)
        staging = staging.resolve()
        self.versions.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise BuildError(f"release already exists: {version}")
        if staging.parent != self.root:
            raise BuildError("staging directory must be directly under release root")
        self._validate_release_path(staging, version, require_evaluation=False)
        os.replace(staging, destination)
        return destination

    def new_staging(self, version: str) -> Path:
        self.version_path(version)
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / f".staging-{version}-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        return staging

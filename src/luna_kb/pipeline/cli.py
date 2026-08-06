from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

from ..attestation import ArtifactSnapshot
from ..clients import (
    ModelEndpoints,
    RemoteModels,
    release_build_config,
    release_model_config,
)
from ..config import Settings
from ..errors import BuildError, ContractError, KnowledgeError
from ..evaluation_policy import FORMAL_FACULTY_SET_SHA256
from ..release import ReleaseManager, file_sha256
from ..retrieval import KnowledgeDatabase, StrongRetriever
from ..service import AnswerService, Runtime
from .build import (
    build_database,
    load_reviewed,
    load_reviewed_snapshot,
    make_manifest,
    write_json,
)
from .batching import (
    collect_luna_workspace,
    load_task_package,
    prepare_luna_workspace,
    validate_luna_batch,
)
from .catalog import make_source_catalog, select_catalog_upgrade_tasks
from .evaluate import (
    evaluate,
    load_evaluation_set_snapshot,
    validate_evaluation_gold,
)
from .review import (
    ReviewEngine,
    load_decisions,
    load_luna_results,
    merge_reviewed_files,
    write_report,
    write_reviewed,
)
from .tasks import generate_task_package, make_rescue_search_tasks, write_tasks


def _settings_models(settings: Settings) -> RemoteModels:
    return RemoteModels(
        ModelEndpoints(
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
            planner_model=settings.planner_model,
            embedding_model=settings.embedding_model,
            reranker_model=settings.reranker_model,
            answer_model=settings.answer_model,
            reranker_url=settings.reranker_url,
            timeout=settings.request_timeout,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="campus-qa-kb")
    sub = parser.add_subparsers(dest="command", required=True)
    tasks = sub.add_parser("tasks", help="generate Luna one-source task package")
    tasks.add_argument("--kb", type=Path, required=True)
    tasks.add_argument("--web", type=Path, required=True)
    tasks.add_argument("--output", type=Path, required=True)

    rescue_tasks = sub.add_parser(
        "rescue-search-tasks",
        help="convert failed URL fetches into official-search tasks",
    )
    rescue_tasks.add_argument("--tasks", type=Path, required=True)
    rescue_tasks.add_argument("--results", type=Path, required=True)
    rescue_tasks.add_argument("--output", type=Path, required=True)

    luna_prepare = sub.add_parser("luna-prepare", help="prepare isolated Luna worker batches")
    luna_prepare.add_argument("--tasks", type=Path, required=True)
    luna_prepare.add_argument("--workspace", type=Path, required=True)
    luna_prepare.add_argument("--instructions", type=Path, required=True)
    luna_prepare.add_argument("--clean-instructions", type=Path, required=True)
    luna_prepare.add_argument("--mcporter-config", type=Path)
    luna_prepare.add_argument("--batch-size", type=int, default=12)
    luna_prepare.add_argument("--max-batch-chars", type=int)
    luna_prepare.add_argument("--utf8-json", action="store_true")
    luna_prepare.add_argument(
        "--lanes",
        nargs="+",
        default=["core_kb", "secondary_kb", "current_web"],
    )
    luna_prepare.add_argument("--actions", nargs="+")

    luna_validate = sub.add_parser("luna-validate", help="validate one Luna JSONL batch")
    luna_validate.add_argument("--batch", type=Path, required=True)
    luna_validate.add_argument("--output", type=Path, required=True)
    luna_validate.add_argument("--report", type=Path, required=True)

    luna_collect = sub.add_parser("luna-collect", help="revalidate and merge Luna batches")
    luna_collect.add_argument("--workspace", type=Path, required=True)
    luna_collect.add_argument("--output", type=Path, required=True)
    luna_collect.add_argument("--allow-partial", action="store_true")

    review = sub.add_parser("review", help="review Luna JSONL results")
    review.add_argument("--input", type=Path, required=True)
    review.add_argument("--decisions", type=Path)
    review.add_argument("--output", type=Path, required=True)
    review.add_argument("--report", type=Path, required=True)

    catalog = sub.add_parser("catalog", help="build reviewed navigation cards from a web index")
    catalog.add_argument("--web", type=Path, required=True)
    catalog.add_argument("--output", type=Path, required=True)
    catalog.add_argument("--report", type=Path, required=True)
    catalog.add_argument("--as-of-year", type=int)

    review_merge = sub.add_parser(
        "review-merge", help="revalidate and merge reviewed datasets"
    )
    review_merge.add_argument("--inputs", type=Path, nargs="+", required=True)
    review_merge.add_argument("--output", type=Path, required=True)
    review_merge.add_argument("--report", type=Path, required=True)

    catalog_tasks = sub.add_parser(
        "catalog-tasks", help="select catalog entries that should be upgraded by Luna"
    )
    catalog_tasks.add_argument("--tasks", type=Path, required=True)
    catalog_tasks.add_argument("--catalog", type=Path, required=True)
    catalog_tasks.add_argument("--output", type=Path, required=True)
    catalog_tasks.add_argument("--include-historical", action="store_true")

    build = sub.add_parser("build", help="build immutable release artifacts")
    build.add_argument("--reviewed", type=Path, required=True)
    build.add_argument("--review-report", type=Path, required=True)
    build.add_argument("--version", required=True)

    evaluation = sub.add_parser("evaluate", help="run the fixed >=300 question gate")
    evaluation.add_argument("--version", required=True)
    evaluation.add_argument("--set", dest="evaluation_set", type=Path, required=True)
    evaluation.add_argument("--faculty", type=Path, required=True)

    activate = sub.add_parser("activate", help="atomically switch current release")
    activate.add_argument("--version", required=True)

    sub.add_parser("health", help="probe active database, FTS, vectors and remote models")
    return parser


async def _build(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings.from_env()
    settings.validate()
    manager = ReleaseManager(settings.release_root)
    staging = manager.new_staging(args.version)
    models = _settings_models(settings)
    build_model_config = release_build_config(
        models.endpoints, embedding_dimension=settings.embedding_dimension
    )
    try:
        reviewed_snapshot = ArtifactSnapshot.from_path(args.reviewed)
        review_report_snapshot = ArtifactSnapshot.from_path(args.review_report)
        reviewed = load_reviewed_snapshot(reviewed_snapshot)
        review_report_snapshot.write_to(staging / "review_report.json")
        review_report = review_report_snapshot.json()
        if not isinstance(review_report, dict):
            raise BuildError("review report must be a JSON object")
        reviewed_sha256 = reviewed_snapshot.sha256
        if review_report.get("reviewed_sha256") != reviewed_sha256:
            raise BuildError("review report checksum does not match the reviewed JSONL input")
        build_report = await build_database(
            reviewed,
            staging / "knowledge.sqlite",
            models,
            settings.embedding_dimension,
        )
        build_report["reviewed_sha256"] = reviewed_sha256
        if release_build_config(
            models.endpoints, embedding_dimension=settings.embedding_dimension
        ) != build_model_config:
            raise BuildError("build code or embedding configuration changed during database build")
        reviewed_snapshot.assert_path_unchanged(args.reviewed)
        review_report_snapshot.assert_path_unchanged(args.review_report)
        build_report["model_config"] = build_model_config
        write_json(staging / "build_report.json", build_report)
        manifest = make_manifest(args.version, build_report, review_report)
        manifest["build_report_sha256"] = file_sha256(staging / "build_report.json")
        manifest["review_report_sha256"] = file_sha256(staging / "review_report.json")
        write_json(
            staging / "manifest.json",
            manifest,
        )
        destination = manager.install_staging(staging, args.version)
        return {"release": str(destination), "build": build_report}
    except Exception:
        if staging.exists() and staging.parent == manager.root:
            shutil.rmtree(staging)
        raise
    finally:
        await models.close()


async def _evaluate(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings.from_env()
    settings.validate()
    evaluation_snapshot = ArtifactSnapshot.from_path(args.evaluation_set)
    faculty_snapshot = ArtifactSnapshot.from_path(args.faculty)
    if faculty_snapshot.sha256 != FORMAL_FACULTY_SET_SHA256:
        raise BuildError("faculty CSV is not the approved fixed isolation set")
    evaluation_items = load_evaluation_set_snapshot(evaluation_snapshot)
    manager = ReleaseManager(settings.release_root)
    manifest = manager.validate(args.version, require_evaluation=False)
    release_path = manager.version_path(args.version)
    database = KnowledgeDatabase(release_path / "knowledge.sqlite", settings.embedding_dimension)
    models = _settings_models(settings)
    evaluation_model_config = release_model_config(
        models.endpoints,
        embedding_dimension=settings.embedding_dimension,
        rerank_min_score=settings.rerank_min_score,
    )
    evaluation_build_config = release_build_config(
        models.endpoints, embedding_dimension=settings.embedding_dimension
    )
    try:
        build_report = json.loads(
            (release_path / "build_report.json").read_text(encoding="utf-8")
        )
        if build_report.get("model_config") != evaluation_build_config:
            raise BuildError(
                "evaluation embedding or build configuration differs from database build"
            )
        gold_closure = validate_evaluation_gold(database, evaluation_items)
        service = AnswerService(
            StrongRetriever(
                database,
                models,
                settings.rerank_min_score,
                settings.fast_path_enabled,
            ),
            models,
            cache_ttl_seconds=0,
            cache_size=0,
            answer_mode=settings.answer_mode,
            answer_max_chars=settings.answer_max_chars,
            answer_max_sources=settings.answer_max_sources,
        )
        report = await evaluate(
            evaluation_items, service, faculty_snapshot
        )
        if release_model_config(
            models.endpoints,
            embedding_dimension=settings.embedding_dimension,
            rerank_min_score=settings.rerank_min_score,
        ) != evaluation_model_config:
            raise BuildError("runtime code or model configuration changed during evaluation")
        evaluation_snapshot.assert_path_unchanged(args.evaluation_set)
        faculty_snapshot.assert_path_unchanged(args.faculty)
        report.update(
            {
                "release_version": args.version,
                "knowledge_sha256": manifest["knowledge_sha256"],
                "evaluation_set_sha256": evaluation_snapshot.sha256,
                "faculty_set_sha256": faculty_snapshot.sha256,
                "model_config": evaluation_model_config,
                # Proves which build this evaluation ran against, without
                # binding the build to query-time code.
                "build_config": evaluation_build_config,
                "gold_closure": gold_closure,
            }
        )
        if report["passed"]:
            manager.mark_evaluated(args.version, report)
        else:
            write_json(release_path / "evaluation_report.json", report)
        return report
    finally:
        database.close()
        await models.close()


async def _health() -> dict[str, object]:
    runtime = await Runtime.start(Settings.from_env())
    try:
        return await runtime.healthcheck()
    finally:
        await runtime.close()


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        if args.command == "tasks":
            result = generate_task_package(args.kb, args.web, args.output)
        elif args.command == "rescue-search-tasks":
            rescue = make_rescue_search_tasks(
                load_task_package(args.tasks),
                load_luna_results(args.results),
            )
            result = {
                "selected_tasks": len(rescue),
                "actions": write_tasks(rescue, args.output),
            }
        elif args.command == "luna-prepare":
            result = prepare_luna_workspace(
                args.tasks,
                args.workspace,
                args.instructions,
                args.clean_instructions,
                args.mcporter_config,
                batch_size=args.batch_size,
                lanes=args.lanes,
                actions=args.actions,
                max_batch_chars=args.max_batch_chars,
                escape_non_ascii=not args.utf8_json,
            )
        elif args.command == "luna-validate":
            result = validate_luna_batch(args.batch, args.output)
            write_json(args.report, result)
            if not result["passed"]:
                raise BuildError(f"Luna batch validation failed; see {args.report}")
        elif args.command == "luna-collect":
            result = collect_luna_workspace(
                args.workspace,
                args.output,
                allow_partial=args.allow_partial,
            )
        elif args.command == "review":
            sources = load_luna_results(args.input)
            engine = ReviewEngine()
            reviewed = engine.review(sources, load_decisions(args.decisions))
            write_reviewed(args.output, reviewed)
            result = engine.report(reviewed, len(sources))
            result["reviewed_sha256"] = file_sha256(args.output)
            write_report(args.report, result)
        elif args.command == "catalog":
            sources, catalog_report = make_source_catalog(
                args.web, as_of_year=args.as_of_year
            )
            engine = ReviewEngine()
            reviewed = engine.review(sources)
            write_reviewed(args.output, reviewed)
            result = engine.report(reviewed, len(sources))
            result["reviewed_sha256"] = file_sha256(args.output)
            result["catalog"] = catalog_report
            write_report(args.report, result)
        elif args.command == "review-merge":
            reviewed, result = merge_reviewed_files(args.inputs)
            write_reviewed(args.output, reviewed)
            result["reviewed_sha256"] = file_sha256(args.output)
            write_report(args.report, result)
        elif args.command == "catalog-tasks":
            all_tasks = load_task_package(args.tasks)
            catalog_reviewed = load_reviewed(args.catalog)
            selected = select_catalog_upgrade_tasks(
                all_tasks,
                catalog_reviewed,
                include_historical=args.include_historical,
            )
            counts = write_tasks(selected, args.output)
            result = {
                "input_tasks": len(all_tasks),
                "catalog_cards": len(catalog_reviewed),
                "selected_tasks": len(selected),
                "include_historical": args.include_historical,
                "actions": counts,
            }
        elif args.command == "build":
            result = asyncio.run(_build(args))
        elif args.command == "evaluate":
            result = asyncio.run(_evaluate(args))
        elif args.command == "activate":
            settings = Settings.from_env()
            ReleaseManager(settings.release_root).activate(args.version)
            result = {"activated": args.version}
        elif args.command == "health":
            result = asyncio.run(_health())
        else:
            raise AssertionError(args.command)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    except (KnowledgeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

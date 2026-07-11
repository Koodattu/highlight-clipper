from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .attempts import AttemptStore
from .composition import AnalysisSelection, build_analysis_workflow, selection_from_configuration
from .database import Database
from .model_profiles import load_catalog
from .recovery import reconcile_startup
from .runtime import configure_local_caches, write_runtime_manifest
from .settings import Settings
from .setup_assets import download_model_profile, install_llama_cpp_runtime
from .workflows.backup import create_backup, restore_backup, verify_backup
from .workflows.evaluate import evaluate_analysis
from .workflows.import_source import backfill_waveform_caches, import_source


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="highlight-clipper", description="Local highlight retrieval and review")
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", help="Create the local Work Directory and verify prerequisites")
    setup.add_argument("--skip-media-check", action="store_true")
    profiles = load_catalog()["profiles"]
    setup.add_argument("--llama-cpp", action="store_true", help="Install the pinned repo-local llama.cpp bundle")
    setup.add_argument(
        "--model",
        action="append",
        default=[],
        choices=sorted(profiles),
        help="Download one pinned model profile",
    )
    setup.add_argument(
        "--baseline",
        "--baseline-models",
        dest="baseline_models",
        action="store_true",
        help="Install llama.cpp plus Whisper Turbo, Qwen embeddings, and the no-MTP Qwen evaluator",
    )
    setup.add_argument(
        "--all-evaluators",
        action="store_true",
        help="Download all four pinned evaluator profiles (about 69 GB before cache overhead)",
    )
    setup.add_argument("--list-models", action="store_true", help="List catalog profile IDs without downloading")

    source_import = commands.add_parser("import", help="Copy and validate one local Source Recording")
    source_import.add_argument("path", type=Path)
    source_import.add_argument("--video-stream", type=int)
    source_import.add_argument("--audio-stream", type=int)

    serve = commands.add_parser("serve", help="Start the loopback-only review application")
    serve.add_argument("--port", type=int, default=8765)

    backup = commands.add_parser("backup", help="Create and verify a consistent metadata and label backup")
    backup.add_argument("--destination", type=Path)
    backup.add_argument("--verify", type=Path)
    backup.add_argument("--restore", type=Path)

    analyze = commands.add_parser("analyze", help="Create a new real Analysis Run (or an explicit fake smoke run)")
    analyze.add_argument("source_id")
    analyze.add_argument("--fake", action="store_true", default=None, help="Use deterministic offline fake adapters")
    analyze.add_argument("--asr", choices=["whisper-turbo", "whisper-large-v3"])
    analyze.add_argument("--embedding", choices=["qwen3-embedding-0.6b"])
    analyze.add_argument(
        "--evaluator",
        choices=["qwen36-35b-a3b", "qwen36-27b", "gemma4-31b", "gemma4-26b-a4b"],
    )
    analyze.add_argument("--context-size", type=int)
    analyze.add_argument(
        "--mtp",
        action="store_true",
        default=None,
        help="Enable the selected profile's experimental MTP path",
    )
    analyze.add_argument("--retry-run", help="Resume this failed/cancelled Analysis Run instead of creating a new one")
    analyze.add_argument(
        "--request-more-from",
        help="Expand a succeeded default-budget Analysis Run while preserving its Review Queue prefix",
    )
    analyze.add_argument(
        "--fake-transcript",
        default="Wow, this funny story matters because the ending works.",
        help="Text for the explicit offline fake ASR adapter",
    )

    cancel = commands.add_parser("cancel", help="Request cancellation of the currently running stage")
    cancel.add_argument("analysis_run_id")

    evaluate = commands.add_parser("evaluate", help="Score one succeeded Analysis Run against frozen references")
    evaluate.add_argument("analysis_run_id")
    return parser


def _database() -> Database:
    settings = Settings.discover()
    settings.ensure_work_directories()
    configure_local_caches(settings)
    database = Database(settings)
    database.migrate()
    recovery = reconcile_startup(database)
    if recovery.missing_valuable_artifacts:
        print(
            "warning: valuable registered artifacts are missing: " + ", ".join(recovery.missing_valuable_artifacts),
            file=sys.stderr,
        )
    return database


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "backup" and arguments.restore is not None:
            if arguments.destination is not None or arguments.verify is not None:
                raise ValueError("Use only one of --destination, --verify, or --restore")
            settings = Settings.discover()
            settings.ensure_work_directories()
            configure_local_caches(settings)
            result = restore_backup(Database(settings), arguments.restore)
            print(
                json.dumps(
                    {
                        "restored_from": str(result.restored_from),
                        "safety_backup": str(result.safety_backup),
                    },
                    indent=2,
                )
            )
            return 0
        database = _database()
        if arguments.command == "setup":
            database.integrity_check()
            profile_id = database.ensure_default_profile()
            if arguments.list_models:
                print(json.dumps({"profiles": sorted(load_catalog()["profiles"])}, indent=2))
                return 0
            install_runtime = arguments.llama_cpp or arguments.baseline_models or arguments.all_evaluators
            runtime_directory = install_llama_cpp_runtime(database.settings) if install_runtime else None
            selected_models = list(arguments.model)
            if arguments.baseline_models:
                selected_models.extend(["whisper-turbo", "qwen3-embedding-0.6b", "qwen36-35b-a3b"])
            if arguments.all_evaluators:
                selected_models.extend(["qwen36-35b-a3b", "qwen36-27b", "gemma4-31b", "gemma4-26b-a4b"])
            model_directories = {
                model_id: str(download_model_profile(database.settings, model_id))
                for model_id in dict.fromkeys(selected_models)
            }
            manifest = write_runtime_manifest(database.settings, check_media=not arguments.skip_media_check)
            repaired_waveforms = backfill_waveform_caches(database)
            print(
                json.dumps(
                    {
                        "work_dir": str(database.settings.work_dir),
                        "database": str(database.path),
                        "creator_profile_revision_id": profile_id,
                        "runtime_manifest": str(manifest),
                        "llama_cpp_runtime": str(runtime_directory) if runtime_directory else None,
                        "models": model_directories,
                        "repaired_waveform_caches": repaired_waveforms,
                    },
                    indent=2,
                )
            )
            return 0
        if arguments.command == "import":
            result = import_source(
                database,
                arguments.path,
                video_stream=arguments.video_stream,
                audio_stream=arguments.audio_stream,
            )
            print(json.dumps(asdict(result), indent=2))
            return 0
        if arguments.command == "serve":
            import uvicorn

            from .web.app import create_app

            uvicorn.run(
                create_app(database.settings),
                host="127.0.0.1",
                port=arguments.port,
                access_log=False,
                server_header=False,
            )
            return 0
        if arguments.command == "backup":
            if sum(value is not None for value in (arguments.destination, arguments.verify, arguments.restore)) > 1:
                raise ValueError("Use only one of --destination, --verify, or --restore")
            if arguments.verify:
                verify_backup(arguments.verify)
                print(json.dumps({"verified": str(arguments.verify.resolve())}, indent=2))
            else:
                result = create_backup(database, arguments.destination)
                print(
                    json.dumps(
                        {
                            "directory": str(result.directory),
                            "database_snapshot": str(result.database_snapshot),
                            "portable_labels": str(result.portable_labels),
                        },
                        indent=2,
                    )
                )
            return 0
        if arguments.command == "analyze":
            if arguments.retry_run and arguments.request_more_from:
                raise ValueError("--retry-run and --request-more-from cannot be combined")
            source = database.fetch_one(
                "SELECT source_end_us FROM source_recordings WHERE id = ?", (arguments.source_id,)
            )
            if source is None:
                raise KeyError(f"Unknown Source Recording: {arguments.source_id}")
            if arguments.request_more_from:
                if any(
                    value is not None
                    for value in (
                        arguments.fake,
                        arguments.asr,
                        arguments.embedding,
                        arguments.evaluator,
                        arguments.context_size,
                        arguments.mtp,
                    )
                ):
                    raise ValueError("Request More inherits the parent model settings; omit model override flags")
                parent = database.fetch_one(
                    "SELECT configuration_json FROM analysis_runs WHERE id = ?",
                    (arguments.request_more_from,),
                )
                if parent is None:
                    raise KeyError(f"Unknown parent Analysis Run: {arguments.request_more_from}")
                selection = replace(
                    selection_from_configuration(json.loads(str(parent["configuration_json"]))),
                    budget_tier="expanded",
                )
            else:
                selection = AnalysisSelection(
                    mode="fake" if arguments.fake else "real",
                    asr_profile=arguments.asr or "whisper-turbo",
                    embedding_profile=arguments.embedding or "qwen3-embedding-0.6b",
                    evaluator_profile=arguments.evaluator or "qwen36-35b-a3b",
                    evaluator_context_size=(
                        arguments.context_size if arguments.context_size is not None else 32_768
                    ),
                    evaluator_mtp=bool(arguments.mtp),
                    budget_tier="default",
                    fake_transcript=arguments.fake_transcript,
                )
            workflow = build_analysis_workflow(
                database,
                selection,
                source_end_us=int(source["source_end_us"]),
            )
            result = workflow.run(
                arguments.source_id,
                requested_more_from_run_id=arguments.request_more_from,
                resume_run_id=arguments.retry_run,
            )
            print(json.dumps(asdict(result), indent=2))
            return 0
        if arguments.command == "cancel":
            attempt = database.fetch_one(
                "SELECT id FROM stage_attempts WHERE scope_type = 'analysis' AND scope_id = ? "
                "AND state = 'running' ORDER BY attempt_number DESC LIMIT 1",
                (arguments.analysis_run_id,),
            )
            if attempt is None:
                raise RuntimeError("Analysis Run has no running stage to cancel")
            AttemptStore(database).request_cancellation(str(attempt["id"]), "cli")
            print(json.dumps({"cancellation_requested": str(attempt["id"])}, indent=2))
            return 0
        if arguments.command == "evaluate":
            result = evaluate_analysis(database, arguments.analysis_run_id)
            print(
                json.dumps(
                    {
                        "report_path": str(result.path),
                        "sha256": result.sha256,
                        "report": result.report,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
    except (KeyError, RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

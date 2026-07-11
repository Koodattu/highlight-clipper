from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..database import Database

PORTABLE_TABLES = (
    "source_recordings",
    "creator_profile_revisions",
    "analysis_runs",
    "stage_attempts",
    "analysis_stage_reuses",
    "transcript_segments",
    "transcript_words",
    "evidence_items",
    "observations",
    "candidate_moments",
    "candidate_evidence",
    "candidate_clusters",
    "cluster_members",
    "context_envelopes",
    "boundary_anchors",
    "evaluation_attempts",
    "embedding_generations",
    "clip_proposals",
    "proposal_evidence",
    "proposal_candidates",
    "proposal_risks",
    "candidate_outcomes",
    "queue_snapshots",
    "queue_entries",
    "reference_moment_revisions",
    "editorial_decisions",
    "boundary_edits",
    "boundary_reanalysis_targets",
    "review_activity_events",
    "exports",
    "export_requests",
    "artifacts",
)


@dataclass(frozen=True, slots=True)
class BackupResult:
    directory: Path
    database_snapshot: Path
    portable_labels: Path


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored_from: Path
    safety_backup: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: object) -> None:
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    partial.replace(path)


def create_backup(database: Database, destination: Path | None = None) -> BackupResult:
    database.integrity_check()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    local_dir = database.settings.work_dir / "backups" / timestamp
    local_dir.mkdir(parents=True, exist_ok=False)
    snapshot = local_dir / "highlight-clipper.sqlite3"
    labels = local_dir / "creator-labels.json"
    database.backup_to(snapshot)
    exported: dict[str, object] = {"schema_version": 1, "created_at": timestamp, "tables": {}}
    snapshot_connection = sqlite3.connect(snapshot)
    snapshot_connection.row_factory = sqlite3.Row
    try:
        for table in PORTABLE_TABLES:
            exported["tables"][table] = [dict(row) for row in snapshot_connection.execute(f"SELECT * FROM {table}")]
    finally:
        snapshot_connection.close()
    _write_json_atomic(labels, exported)
    manifest = {
        "schema_version": 1,
        "created_at": timestamp,
        "media_included": False,
        "files": {
            snapshot.name: {"size_bytes": snapshot.stat().st_size, "sha256": _sha256(snapshot)},
            labels.name: {"size_bytes": labels.stat().st_size, "sha256": _sha256(labels)},
        },
    }
    _write_json_atomic(local_dir / "manifest.json", manifest)
    verify_backup(local_dir)

    output_dir = local_dir
    if destination is not None:
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        output_dir = destination / f"highlight-clipper-backup-{timestamp}"
        output_dir.mkdir(parents=False, exist_ok=False)
        for source in local_dir.iterdir():
            if source.is_file():
                shutil.copy2(source, output_dir / source.name)
        verify_backup(output_dir)
    return BackupResult(output_dir, output_dir / snapshot.name, output_dir / labels.name)


def verify_backup(directory: Path) -> None:
    root = directory.resolve(strict=True)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        path = root / name
        if not path.is_file():
            raise RuntimeError(f"Backup file is missing: {name}")
        if path.stat().st_size != expected["size_bytes"] or _sha256(path) != expected["sha256"]:
            raise RuntimeError(f"Backup integrity mismatch: {name}")
    connection = sqlite3.connect(root / "highlight-clipper.sqlite3")
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup database integrity check failed: {result}")
    finally:
        connection.close()
    labels = json.loads((root / "creator-labels.json").read_text(encoding="utf-8"))
    if labels.get("schema_version") != 1 or not isinstance(labels.get("tables"), dict):
        raise RuntimeError("Portable label package has an unsupported schema")


def _current_database_is_healthy_and_idle(database: Database) -> bool:
    if not database.path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"{database.path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False
            has_lease_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'active_operation_lease'"
            ).fetchone()
            if has_lease_table and connection.execute(
                "SELECT 1 FROM active_operation_lease WHERE singleton = 1"
            ).fetchone():
                raise RuntimeError("A Source Import or Analysis operation is active")
            return True
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return False


def _preserve_raw_database_files(database: Database) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = database.settings.work_dir / "backups" / f"pre-restore-raw-{timestamp}"
    directory.mkdir(parents=True, exist_ok=False)
    files: dict[str, dict[str, object]] = {}
    for suffix in ("", "-wal", "-shm"):
        source = database.path.with_name(f"{database.path.name}{suffix}")
        if not source.is_file():
            continue
        destination = directory / source.name
        shutil.copy2(source, destination)
        files[destination.name] = {"size_bytes": destination.stat().st_size, "sha256": _sha256(destination)}
    _write_json_atomic(
        directory / "manifest.json",
        {
            "schema_version": 1,
            "created_at": timestamp,
            "consistent": False,
            "purpose": "best-effort pre-restore preservation of an unhealthy or unmigrated database",
            "files": files,
        },
    )
    return directory


def restore_backup(database: Database, directory: Path) -> RestoreResult:
    source = directory.resolve(strict=True)
    verify_backup(source)
    if _current_database_is_healthy_and_idle(database):
        try:
            safety_directory = create_backup(database).directory
        except (sqlite3.DatabaseError, RuntimeError):
            safety_directory = _preserve_raw_database_files(database)
    else:
        safety_directory = _preserve_raw_database_files(database)
    snapshot = source / "highlight-clipper.sqlite3"
    partial = database.path.with_name(f"{database.path.name}.restore.partial")
    partial.unlink(missing_ok=True)
    shutil.copy2(snapshot, partial)
    restored = sqlite3.connect(partial)
    try:
        if restored.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Restore snapshot failed its SQLite integrity check")
    finally:
        restored.close()
    for suffix in ("-wal", "-shm"):
        database.path.with_name(f"{database.path.name}{suffix}").unlink(missing_ok=True)
    partial.replace(database.path)
    database.migrate()
    database.integrity_check()
    return RestoreResult(source, safety_directory)

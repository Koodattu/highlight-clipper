from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..adapters.ffmpeg import FFmpegAdapter, MediaError
from ..artifacts import ArtifactStore, copy_with_sha256, sha256_file
from ..database import Database, utc_now
from ..domain import canonical_json, new_id
from ..recovery import OperationLeaseHeartbeat, current_owner_identity
from ..waveform import build_waveform_peaks

LOCAL_MEDIA_EXTENSIONS = {
    ".avi",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ts",
    ".webm",
}


@dataclass(frozen=True, slots=True)
class ImportResult:
    source_recording_id: str
    sha256: str
    source_end_us: int


def backfill_waveform_caches(database: Database) -> int:
    sources = database.fetch_all(
        "SELECT s.id, s.source_end_us FROM source_recordings s "
        "WHERE NOT EXISTS (SELECT 1 FROM artifacts w WHERE w.source_recording_id = s.id "
        "AND w.kind = 'waveform_peaks' AND w.removed_at IS NULL) ORDER BY s.created_at, s.id"
    )
    repaired = 0
    artifacts = ArtifactStore(database)
    for source in sources:
        audio = database.fetch_one(
            "SELECT * FROM artifacts WHERE source_recording_id = ? AND kind = 'analysis_audio' "
            "AND removed_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (source["id"],),
        )
        if audio is None:
            continue
        audio_path = artifacts.require_intact(audio)
        destination = audio_path.parent / "waveform-peaks.u16le"
        integrity = build_waveform_peaks(audio_path, destination)
        integrity.update(
            {
                "analysis_audio_sha256": audio["sha256"],
                "source_end_us": int(source["source_end_us"]),
            }
        )
        digest = sha256_file(destination)
        with database.transaction(immediate=True) as connection:
            artifacts.register(
                connection,
                path=destination,
                kind="waveform_peaks",
                owner_type="source_import_repair",
                owner_id=str(source["id"]),
                source_recording_id=str(source["id"]),
                configuration={"profile": "pcm-absolute-peak-u16le-100ms-v1"},
                require_hash=True,
                precomputed_sha256=digest,
                precomputed_size=destination.stat().st_size,
                regenerable=True,
                integrity=integrity,
            )
        repaired += 1
    return repaired


def _validated_input(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("Source path must be absolute")
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Network paths are not accepted as local Source Recordings")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("Source path must be a regular local file")
    if resolved.suffix.casefold() not in LOCAL_MEDIA_EXTENSIONS:
        raise ValueError("Source file type is not an accepted local video container")
    return resolved


def _preflight_disk(source: Path, work_dir: Path, duration_seconds: float | None) -> None:
    duration = max(0.0, duration_seconds or 0.0)
    # Conservative bits-per-second allowance for a high-motion 1280-wide CRF proxy,
    # PCM analysis audio, AAC proxy audio, and the compact waveform cache.
    derivative_estimate = int(duration * (12_000_000 + 256_000 + 192_000 + 160) / 8)
    required = source.stat().st_size + derivative_estimate + 512 * 1024 * 1024
    free = shutil.disk_usage(work_dir).free
    if free < required:
        raise RuntimeError(f"Not enough free disk for import: need about {required} bytes, have {free} bytes")


def import_source(
    database: Database,
    source_path: Path,
    *,
    video_stream: int | None = None,
    audio_stream: int | None = None,
    media: FFmpegAdapter | None = None,
) -> ImportResult:
    source = _validated_input(source_path)
    adapter = media or FFmpegAdapter()
    probe = adapter.probe(source)
    selected_video = adapter.select_stream(probe.video_streams, video_stream, "video")
    selected_audio = adapter.select_stream(probe.audio_streams, audio_stream, "audio")
    duration = float(probe.format_duration) if probe.format_duration else None
    _preflight_disk(source, database.settings.work_dir, duration)
    source_stat_before_copy = source.stat()

    attempt_id = new_id("import")
    source_id = new_id("source")
    owner = current_owner_identity()
    with database.transaction(immediate=True) as connection:
        connection.execute(
            "INSERT INTO source_import_attempts "
            "(id, state, input_path, requested_video_stream, requested_audio_stream, "
            "owner_instance, planned_source_recording_id, created_at) "
            "VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)",
            (attempt_id, str(source), video_stream, audio_stream, owner, source_id, utc_now()),
        )
        try:
            connection.execute(
                "INSERT INTO active_operation_lease "
                "(singleton, operation_type, operation_id, owner_instance, heartbeat_at) "
                "VALUES (1, 'source_import', ?, ?, ?)",
                (attempt_id, owner, utc_now()),
            )
        except Exception as exc:
            raise RuntimeError("Another Source Import or Analysis operation is active") from exc
        connection.execute(
            "UPDATE source_import_attempts SET state = 'running', started_at = ? WHERE id = ?",
            (utc_now(), attempt_id),
        )

    heartbeat: OperationLeaseHeartbeat | None = OperationLeaseHeartbeat(database, attempt_id, owner)
    heartbeat.__enter__()
    destination_dir = database.settings.work_dir / "sources" / source_id
    try:
        destination_dir.mkdir(parents=True, exist_ok=False)
        suffix = source.suffix.lower() if source.suffix else ".media"
        canonical_path = destination_dir / f"original{suffix}"
        partial_path = destination_dir / f"original{suffix}.partial"
        source_hash, source_size = copy_with_sha256(source, partial_path)
        source_stat_after_copy = source.stat()
        if (
            source_stat_before_copy.st_size != source_stat_after_copy.st_size
            or source_stat_before_copy.st_mtime_ns != source_stat_after_copy.st_mtime_ns
            or source_size != source_stat_after_copy.st_size
        ):
            raise RuntimeError("Source file changed while it was being copied")
        partial_path.replace(canonical_path)
        imported = adapter.prepare_import(
            canonical_path,
            destination_dir,
            selected_video.index,
            selected_audio.index,
        )
        proxy_hash = sha256_file(imported.proxy_path)
        analysis_audio_hash = sha256_file(imported.analysis_audio_path)
        waveform_path = destination_dir / "waveform-peaks.u16le"
        waveform_integrity = build_waveform_peaks(imported.analysis_audio_path, waveform_path)
        waveform_integrity.update(
            {
                "analysis_audio_sha256": analysis_audio_hash,
                "source_end_us": imported.source_end_us,
            }
        )
        waveform_hash = sha256_file(waveform_path)
        artifacts = ArtifactStore(database)
        configuration = {
            "video_stream": selected_video.index,
            "audio_stream": selected_audio.index,
            "media_adapter": adapter.version_manifest(),
        }
        heartbeat.assert_owned()
        heartbeat.__exit__(None, None, None)
        heartbeat = None
        with database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO source_recordings "
                "(id, import_attempt_id, original_name, original_relpath, sha256, size_bytes, "
                "source_end_us, video_stream_index, audio_stream_index, media_manifest_json, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    attempt_id,
                    source.name,
                    database.settings.relative_to_workdir(canonical_path),
                    source_hash,
                    source_size,
                    imported.source_end_us,
                    imported.video_stream_index,
                    imported.audio_stream_index,
                    canonical_json(imported.manifest),
                    utc_now(),
                ),
            )
            artifacts.register(
                connection,
                path=canonical_path,
                kind="source_original",
                owner_type="source_import",
                owner_id=attempt_id,
                source_recording_id=source_id,
                configuration=configuration,
                require_hash=True,
                precomputed_sha256=source_hash,
                precomputed_size=source_size,
                regenerable=False,
                integrity={"sha256": source_hash, "copy_verified": True},
            )
            artifacts.register(
                connection,
                path=imported.proxy_path,
                kind="review_proxy",
                owner_type="source_import",
                owner_id=attempt_id,
                source_recording_id=source_id,
                configuration={**configuration, "profile": "h264-aac-review-v1"},
                require_hash=True,
                precomputed_sha256=proxy_hash,
                precomputed_size=imported.proxy_path.stat().st_size,
                regenerable=True,
                integrity={"validated": True, "sha256": proxy_hash},
            )
            artifacts.register(
                connection,
                path=imported.analysis_audio_path,
                kind="analysis_audio",
                owner_type="source_import",
                owner_id=attempt_id,
                source_recording_id=source_id,
                configuration={**configuration, "profile": "pcm-s16le-16khz-mono-v1"},
                require_hash=True,
                precomputed_sha256=analysis_audio_hash,
                precomputed_size=imported.analysis_audio_path.stat().st_size,
                regenerable=True,
                integrity={"validated": True, "sha256": analysis_audio_hash},
            )
            artifacts.register(
                connection,
                path=waveform_path,
                kind="waveform_peaks",
                owner_type="source_import",
                owner_id=attempt_id,
                source_recording_id=source_id,
                configuration={**configuration, "profile": "pcm-absolute-peak-u16le-100ms-v1"},
                require_hash=True,
                precomputed_sha256=waveform_hash,
                precomputed_size=waveform_path.stat().st_size,
                regenerable=True,
                integrity=waveform_integrity,
            )
            connection.execute(
                "UPDATE source_import_attempts SET state = 'succeeded', progress = 1, ended_at = ? WHERE id = ?",
                (utc_now(), attempt_id),
            )
            connection.execute(
                "DELETE FROM active_operation_lease WHERE singleton = 1 AND operation_id = ?",
                (attempt_id,),
            )
        return ImportResult(source_id, source_hash, imported.source_end_us)
    except BaseException as exc:
        if heartbeat is not None:
            heartbeat.__exit__(type(exc), exc, exc.__traceback__)
        with database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT state FROM source_import_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if row and row["state"] == "running":
                connection.execute(
                    "UPDATE source_import_attempts SET state = 'failed', retryable = 1, "
                    "error_code = ?, error_summary = ?, ended_at = ? WHERE id = ?",
                    (
                        "media_import_failed" if isinstance(exc, MediaError) else "import_failed",
                        str(exc)[:2000],
                        utc_now(),
                        attempt_id,
                    ),
                )
            connection.execute(
                "DELETE FROM active_operation_lease WHERE singleton = 1 AND operation_id = ?",
                (attempt_id,),
            )
            registered = connection.execute(
                "SELECT 1 FROM source_recordings WHERE id = ?",
                (source_id,),
            ).fetchone()
            if destination_dir.exists() and registered is None:
                connection.execute(
                    "INSERT INTO recovery_items "
                    "(id, item_type, relative_path, state, created_at) "
                    "VALUES (?, 'unregistered_source_tree', ?, 'pending', ?) "
                    "ON CONFLICT(relative_path) DO NOTHING",
                    (new_id("recovery"), f"sources/{source_id}", utc_now()),
                )
        raise

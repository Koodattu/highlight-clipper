from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ..adapters.ffmpeg import FFmpegAdapter
from ..artifacts import ArtifactStore, sha256_file
from ..database import Database, utc_now
from ..domain import canonical_json, fingerprint, new_id
from ..recovery import current_owner_identity, owner_is_live

EXPORT_PROFILE = {
    "version": "source-aspect-h264-aac-v2",
    "container": "mp4",
    "video_codec": "libx264",
    "crf": 18,
    "preset": "medium",
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_sample_rate": 48_000,
    "audio_bitrate": "192k",
    "faststart": True,
    "strip_source_metadata": True,
    "strip_source_chapters": True,
}


@dataclass(frozen=True, slots=True)
class ExportResult:
    export_id: str
    path: Path
    sha256: str


def export_accepted_clip(
    database: Database,
    proposal_id: str,
    *,
    idempotency_key: str,
    confirmed: bool,
    expected_decision_revision: int,
    confirmed_risk: bool = False,
    confirmed_stale_coverage: bool = False,
    media: FFmpegAdapter | None = None,
) -> ExportResult:
    if not confirmed:
        raise ValueError("Every Export requires explicit human confirmation")
    adapter = media or FFmpegAdapter()
    owner = current_owner_identity()
    reserved = False
    reservation_attempt = 0
    request_fingerprint = ""
    existing_export = database.fetch_one(
        "SELECT e.*, a.relative_path, a.sha256 AS artifact_sha256, d.revision_number AS decision_revision "
        "FROM exports e JOIN artifacts a ON a.id = e.artifact_id "
        "JOIN editorial_decisions d ON d.id = e.editorial_decision_id WHERE e.idempotency_key = ?",
        (idempotency_key,),
    )
    if existing_export is not None:
        confirmation = json.loads(str(existing_export["confirmation_json"]))
        same_request = (
            str(existing_export["clip_proposal_id"]) == proposal_id
            and int(existing_export["decision_revision"]) == expected_decision_revision
            and bool(confirmation.get("confirmed"))
            and bool(confirmation.get("confirmed_risk", False)) == confirmed_risk
            and bool(confirmation.get("confirmed_stale_coverage", False)) == confirmed_stale_coverage
            and json.loads(str(existing_export["export_profile_json"])) == EXPORT_PROFILE
        )
        if not same_request:
            raise ValueError("Idempotency key was already used for a different Export request")
        reconstructed = fingerprint(
            {
                "proposal_id": proposal_id,
                "decision_id": existing_export["editorial_decision_id"],
                "decision_revision": existing_export["decision_revision"],
                "start_us": existing_export["start_us"],
                "end_us": existing_export["end_us"],
                "confirmed": True,
                "confirmed_risk": confirmed_risk,
                "confirmed_stale_coverage": confirmed_stale_coverage,
                "export_profile": EXPORT_PROFILE,
            }
        )
        if existing_export["request_fingerprint"] not in (None, reconstructed):
            raise RuntimeError("Stored Export request fingerprint is inconsistent")
        existing_path = database.settings.resolve_work_path(str(existing_export["relative_path"]))
        if not existing_path.is_file() or sha256_file(existing_path) != existing_export["artifact_sha256"]:
            raise RuntimeError("Completed Export artifact is missing or has changed")
        return ExportResult(str(existing_export["id"]), existing_path, str(existing_export["artifact_sha256"]))

    with database.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT p.*, d.id AS decision_id, d.decision, d.revision_number, "
            "b.start_us AS edited_start_us, b.end_us AS edited_end_us, "
            "COALESCE(b.outside_evaluated_context, 0) AS outside_evaluated_context, "
            "s.id AS source_id, s.sha256 AS source_sha256, s.original_relpath, s.source_end_us, "
            "s.video_stream_index, s.audio_stream_index, s.media_manifest_json, "
            "(SELECT COUNT(*) FROM proposal_risks pr WHERE pr.clip_proposal_id = p.id) AS risk_count "
            "FROM clip_proposals p "
            "JOIN analysis_runs ar ON ar.id = p.analysis_run_id "
            "JOIN source_recordings s ON s.id = ar.source_recording_id "
            "JOIN editorial_decisions d ON d.clip_proposal_id = p.id "
            "LEFT JOIN boundary_edits b ON b.editorial_decision_id = d.id "
            "WHERE p.id = ? AND d.revision_number = "
            "(SELECT MAX(revision_number) FROM editorial_decisions WHERE clip_proposal_id = p.id)",
            (proposal_id,),
        ).fetchone()
        if row is None or row["decision"] != "accept":
            raise RuntimeError("The latest Editorial Decision must be accept before Export")
        if int(row["revision_number"]) != expected_decision_revision:
            raise RuntimeError("The Editorial Decision changed; refresh the Review Queue before Export")
        if int(row["risk_count"]) and not confirmed_risk:
            raise ValueError("Risk-flagged proposals require an additional confirmation")
        if int(row["outside_evaluated_context"]) and not confirmed_stale_coverage:
            raise ValueError("A boundary outside evaluated context requires stale-coverage confirmation")
        start_us = int(row["edited_start_us"] if row["edited_start_us"] is not None else row["start_us"])
        end_us = int(row["edited_end_us"] if row["edited_end_us"] is not None else row["end_us"])
        if not 0 <= start_us < end_us <= int(row["source_end_us"]):
            raise RuntimeError("Accepted interval is outside the Source Recording")
        request_fingerprint = fingerprint(
            {
                "proposal_id": proposal_id,
                "decision_id": row["decision_id"],
                "decision_revision": row["revision_number"],
                "start_us": start_us,
                "end_us": end_us,
                "confirmed": confirmed,
                "confirmed_risk": confirmed_risk,
                "confirmed_stale_coverage": confirmed_stale_coverage,
                "export_profile": EXPORT_PROFILE,
            }
        )
        request = connection.execute(
            "SELECT * FROM export_requests WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if request:
            if request["request_fingerprint"] != request_fingerprint:
                raise ValueError("Idempotency key was already used for a different Export request")
            if request["state"] == "succeeded":
                existing = connection.execute(
                    "SELECT e.id, a.relative_path, a.sha256 FROM exports e "
                    "JOIN artifacts a ON a.id = e.artifact_id WHERE e.id = ?",
                    (request["export_id"],),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("Completed Export registration is inconsistent")
                path = database.settings.resolve_work_path(str(existing["relative_path"]))
                if not path.is_file() or sha256_file(path) != existing["sha256"]:
                    raise RuntimeError("Completed Export artifact is missing or has changed")
                return ExportResult(str(existing["id"]), path, str(existing["sha256"]))
            if request["state"] == "running" and owner_is_live(str(request["owner_instance"])):
                raise RuntimeError("This Export request is already running")
            export_id = str(request["export_id"])
            final_path = database.settings.resolve_work_path(str(request["output_relpath"]))
            connection.execute(
                "UPDATE export_requests SET state = 'running', owner_instance = ?, "
                "attempt_number = attempt_number + 1, error_summary = NULL, updated_at = ? "
                "WHERE idempotency_key = ?",
                (owner, utc_now(), idempotency_key),
            )
            reservation_attempt = int(request["attempt_number"]) + 1
        else:
            export_id = new_id("export")
            final_path = (
                database.settings.work_dir / "exports" / str(row["source_id"]) / proposal_id / f"{export_id}.mp4"
            )
            connection.execute(
                "INSERT INTO export_requests "
                "(idempotency_key, request_fingerprint, export_id, state, output_relpath, "
                "owner_instance, created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)",
                (
                    idempotency_key,
                    request_fingerprint,
                    export_id,
                    database.settings.relative_to_workdir(final_path),
                    owner,
                    utc_now(),
                    utc_now(),
                ),
            )
            reservation_attempt = 1
        reserved = True

    partial_path: Path | None = None
    try:
        source_path = database.settings.resolve_work_path(str(row["original_relpath"]))
        if not source_path.is_file() or sha256_file(source_path) != row["source_sha256"]:
            raise RuntimeError("The immutable Source Recording is missing or has changed")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        partial_path = final_path.with_name(f"{final_path.name}.partial-{reservation_attempt}-{new_id('render')}")
        timeline = json.loads(str(row["media_manifest_json"]))
        video_origin = str(timeline["source_time_origin_seconds"])
        audio_start = str(Decimal(video_origin) + Decimal(str(timeline["audio_video_start_offset_seconds"])))
        if final_path.exists():
            adapter._validate_derivative(final_path, end_us - start_us, require_video=True)
            digest = sha256_file(final_path)
        else:
            adapter.render_export(
                source_path,
                partial_path,
                start_us,
                end_us,
                source_end_us=int(row["source_end_us"]),
                video_stream_index=int(row["video_stream_index"]),
                audio_stream_index=int(row["audio_stream_index"]),
                video_origin_seconds=video_origin,
                audio_start_seconds=audio_start,
            )
            adapter._validate_derivative(partial_path, end_us - start_us, require_video=True)
            digest = sha256_file(partial_path)
        confirmation = {
            "confirmed": True,
            "confirmed_risk": confirmed_risk,
            "confirmed_stale_coverage": confirmed_stale_coverage,
            "decision_revision": int(row["revision_number"]),
            "source_sha256": row["source_sha256"],
            "ffmpeg": adapter.version_manifest(),
            "interval": {"start_us": start_us, "end_us": end_us},
        }
        artifacts = ArtifactStore(database)
        with database.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT state, request_fingerprint, owner_instance, attempt_number "
                "FROM export_requests WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if (
                current is None
                or current["request_fingerprint"] != request_fingerprint
                or current["state"] != "running"
                or current["owner_instance"] != owner
                or int(current["attempt_number"]) != reservation_attempt
            ):
                raise RuntimeError("Export request reservation was lost")
            latest = connection.execute(
                "SELECT id, decision FROM editorial_decisions WHERE clip_proposal_id = ? "
                "ORDER BY revision_number DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
            if latest is None or latest["id"] != row["decision_id"] or latest["decision"] != "accept":
                raise RuntimeError("The accepted Editorial Decision changed while the Export was rendering")
            if not final_path.exists():
                if partial_path is None or not partial_path.is_file():
                    raise RuntimeError("Rendered Export partial is missing")
                partial_path.replace(final_path)
            artifact_id = artifacts.register(
                connection,
                path=final_path,
                kind="export",
                owner_type="export",
                owner_id=export_id,
                source_recording_id=str(row["source_id"]),
                configuration=EXPORT_PROFILE,
                require_hash=True,
                precomputed_sha256=digest,
                precomputed_size=final_path.stat().st_size,
                regenerable=False,
                integrity={"validated": True, "sha256": digest},
            )
            connection.execute(
                "INSERT INTO exports "
                "(id, source_recording_id, clip_proposal_id, editorial_decision_id, artifact_id, "
                "start_us, end_us, export_profile_json, confirmation_json, idempotency_key, "
                "request_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    export_id,
                    row["source_id"],
                    proposal_id,
                    row["decision_id"],
                    artifact_id,
                    start_us,
                    end_us,
                    canonical_json(EXPORT_PROFILE),
                    canonical_json(confirmation),
                    idempotency_key,
                    request_fingerprint,
                    utc_now(),
                ),
            )
            cursor = connection.execute(
                "UPDATE export_requests SET state = 'succeeded', updated_at = ? "
                "WHERE idempotency_key = ? AND state = 'running' AND owner_instance = ? "
                "AND attempt_number = ?",
                (utc_now(), idempotency_key, owner, reservation_attempt),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Export request fencing token changed before commit")
        return ExportResult(export_id, final_path, digest)
    except BaseException as exc:
        if reserved:
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE export_requests SET state = 'failed', error_summary = ?, updated_at = ? "
                    "WHERE idempotency_key = ? AND state = 'running' AND owner_instance = ? "
                    "AND attempt_number = ?",
                    (str(exc)[:2000], utc_now(), idempotency_key, owner, reservation_attempt),
                )
        if partial_path is not None:
            partial_path.unlink(missing_ok=True)
        raise

from __future__ import annotations

import json
import mimetypes
import secrets
import threading
import uuid
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..artifacts import ArtifactStore
from ..attempts import AttemptStore
from ..composition import AnalysisSelection, build_analysis_workflow, selection_from_configuration
from ..database import Database, utc_now
from ..domain import DecisionValue, ProposalCategory, RejectionReason, new_id
from ..recovery import reconcile_startup
from ..settings import Settings
from ..timebase import SourceInterval, seconds_to_us
from ..waveform import read_waveform_peaks
from ..workflows.analyze import MAX_FAILED_STAGE_ATTEMPTS, AnalysisCancelled
from ..workflows.export import export_accepted_clip
from ..workflows.review import record_decision, save_creator_profile

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_RANGE_BUFFER = 1024 * 1024
WEB_MEDIA_KINDS = frozenset({"review_proxy", "export"})


class ProfileRequest(BaseModel):
    languages: list[str]
    category_priorities: dict[str, int]
    desired_content: str = ""
    avoided_content: str = ""
    preferred_durations: dict[str, list[int]]

    @field_validator("category_priorities")
    @classmethod
    def valid_priorities(cls, value: dict[str, int]) -> dict[str, int]:
        categories = {category.value for category in ProposalCategory}
        if set(value) != categories or any(not 0 <= priority <= 4 for priority in value.values()):
            raise ValueError("Every category priority must be an integer from 0 through 4")
        return value

    @field_validator("preferred_durations")
    @classmethod
    def valid_durations(cls, value: dict[str, list[int]]) -> dict[str, list[int]]:
        categories = {category.value for category in ProposalCategory}
        if set(value) != categories or any(
            len(duration) != 2 or not 1 <= duration[0] < duration[1] <= 240
            for duration in value.values()
        ):
            raise ValueError("Every category duration must be 1-240 seconds with start < end")
        return value


class AnalysisRequest(BaseModel):
    mode: Literal["real", "fake"] = "real"
    asr_profile: Literal["whisper-turbo", "whisper-large-v3"] = "whisper-turbo"
    asr_language: Literal["fi", "en"] | None = None
    embedding_profile: Literal["qwen3-embedding-0.6b"] = "qwen3-embedding-0.6b"
    evaluator_profile: Literal["qwen36-35b-a3b", "qwen36-27b", "gemma4-31b", "gemma4-26b-a4b"] = "qwen36-35b-a3b"
    evaluator_context_size: int = Field(default=32_768, ge=8_192, le=262_144)
    evaluator_mtp: bool = False
    fake_transcript: str = "Wow, this funny story matters because the ending works."


class DecisionRequest(BaseModel):
    decision: DecisionValue
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_prior_revision: int = Field(ge=0)
    rejection_reason: RejectionReason | None = None
    note: str = Field(default="", max_length=2000)
    boundary_start_seconds: float | None = Field(default=None, ge=0)
    boundary_end_seconds: float | None = Field(default=None, gt=0)

    @field_validator("boundary_end_seconds")
    @classmethod
    def boundary_pair(cls, value: float | None, info):
        start = info.data.get("boundary_start_seconds")
        if (start is None) != (value is None):
            raise ValueError("Boundary start and end must be supplied together")
        if start is not None and value is not None and value <= start:
            raise ValueError("Boundary end must follow boundary start")
        return value


class ExportRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    confirmed: bool
    expected_decision_revision: int = Field(ge=1)
    confirmed_risk: bool = False
    confirmed_stale_coverage: bool = False


class ReviewActivityRequest(BaseModel):
    queue_snapshot_id: str
    clip_proposal_id: str
    session_id: str = Field(min_length=8, max_length=128)
    sequence_number: int = Field(ge=0)
    active_milliseconds: int = Field(ge=1, le=15_000)
    activity_kind: Literal["playback", "interaction"]


class ReferenceRequest(BaseModel):
    annotation_set_id: str | None = None
    expected_prior_revision: int = Field(default=0, ge=0)
    certainty: str
    language_slice: Literal["fi", "en", "code_switched", "language_neutral", "unknown"] = "unknown"
    category: ProposalCategory
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    event_seconds: float = Field(ge=0)
    short_form_suitability: int = Field(ge=0, le=4)
    rationale: str = Field(min_length=1, max_length=2000)

    @field_validator("certainty")
    @classmethod
    def valid_certainty(cls, value: str) -> str:
        if value not in {"definite", "possible"}:
            raise ValueError("Certainty must be definite or possible")
        return value


def _hostname(value: str) -> str | None:
    try:
        return urlsplit(f"//{value}").hostname
    except ValueError:
        return None


def _json_row(row) -> dict[str, object]:
    result = dict(row)
    for key in tuple(result):
        if key.endswith("_json") and isinstance(result[key], str):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
    return result


def create_app(
    settings: Settings | None = None,
    *,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    local_settings = settings or Settings.discover()
    local_settings.ensure_work_directories()
    database = Database(local_settings)
    database.migrate()
    recovery_report = reconcile_startup(database)
    database.integrity_check()
    database.ensure_default_profile()
    session_token = secrets.token_urlsafe(32)
    host_allowlist = allowed_hosts or {"127.0.0.1", "localhost", "::1"}
    static_dir = Path(__file__).with_name("static")

    app = FastAPI(
        title="Highlight Clipper",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.database = database
    app.state.settings = local_settings
    app.state.session_token = session_token
    app.state.analysis_tasks: dict[str, dict[str, object]] = {}
    app.state.task_lock = threading.Lock()
    app.state.recovery_report = recovery_report
    app.state.media_integrity_cache: dict[str, tuple[int, int, str]] = {}
    app.state.media_integrity_lock = threading.Lock()

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = _hostname(request.headers.get("host", ""))
        if host not in host_allowlist:
            return JSONResponse({"detail": "Invalid local host"}, status_code=400)
        if request.method in MUTATING_METHODS:
            origin = request.headers.get("origin")
            if not origin or urlsplit(origin).hostname not in host_allowlist:
                return JSONResponse({"detail": "Invalid request origin"}, status_code=403)
            if request.headers.get("x-highlight-clipper-token") != session_token:
                return JSONResponse({"detail": "Invalid session token"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        template = (static_dir / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(template.replace("__SESSION_TOKEN__", session_token))

    @app.get("/static/{filename}")
    def static_file(filename: str):
        if filename not in {"app.css", "app.js"}:
            raise HTTPException(404, "Static asset not found")
        return FileResponse(static_dir / filename)

    def launch_analysis_task(
        source_id: str,
        selection: AnalysisSelection,
        *,
        resume_run_id: str | None = None,
        creator_profile_revision_id: str | None = None,
        requested_more_from_run_id: str | None = None,
        boundary_reanalysis_queue_id: str | None = None,
        boundary_reanalysis_proposal_id: str | None = None,
    ) -> str:
        source = database.fetch_one("SELECT source_end_us FROM source_recordings WHERE id = ?", (source_id,))
        if source is None:
            raise HTTPException(404, "Source Recording not found")
        task_id = f"task_{uuid.uuid4().hex}"
        task: dict[str, object] = {
            "id": task_id,
            "source_recording_id": source_id,
            "analysis_run_id": resume_run_id,
            "state": "pending",
            "error": None,
            "cancel_requested": False,
            "mode": selection.mode,
        }
        with app.state.task_lock:
            app.state.analysis_tasks[task_id] = task

        def cancellation_requested() -> bool:
            with app.state.task_lock:
                return bool(task["cancel_requested"])

        def run_started(run_id: str) -> None:
            with app.state.task_lock:
                task["analysis_run_id"] = run_id

        def execute() -> None:
            with app.state.task_lock:
                task["state"] = "running"
            try:
                workflow = build_analysis_workflow(
                    database,
                    selection,
                    source_end_us=int(source["source_end_us"]),
                    cancellation_requested=cancellation_requested,
                )
                result = workflow.run(
                    source_id,
                    creator_profile_revision_id=creator_profile_revision_id,
                    requested_more_from_run_id=requested_more_from_run_id,
                    boundary_reanalysis_queue_id=boundary_reanalysis_queue_id,
                    boundary_reanalysis_proposal_id=boundary_reanalysis_proposal_id,
                    resume_run_id=resume_run_id,
                    run_started=run_started,
                )
                with app.state.task_lock:
                    task.update(
                        {
                            "state": "succeeded",
                            "analysis_run_id": result.analysis_run_id,
                            "queue_snapshot_id": result.queue_snapshot_id,
                        }
                    )
            except AnalysisCancelled as exc:
                with app.state.task_lock:
                    task.update({"state": "cancelled", "error": str(exc)[:1000]})
            except BaseException as exc:
                with app.state.task_lock:
                    task.update({"state": "failed", "error": str(exc)[:1000]})

        threading.Thread(target=execute, name=f"analysis-{task_id}", daemon=True).start()
        return task_id

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, object]:
        stage_order = ("asr", "embeddings", "discovery", "envelopes", "evaluation", "ranking")
        profiles = [
            _json_row(row)
            for row in database.fetch_all("SELECT * FROM creator_profile_revisions ORDER BY revision_number DESC")
        ]
        sources = [
            _json_row(row)
            for row in database.fetch_all(
                "SELECT s.*, a.id AS proxy_artifact_id FROM source_recordings s "
                "LEFT JOIN artifacts a ON a.source_recording_id = s.id AND a.kind = 'review_proxy' "
                "AND a.removed_at IS NULL ORDER BY s.created_at DESC"
            )
        ]
        runs = [
            _json_row(row)
            for row in database.fetch_all("SELECT * FROM analysis_runs ORDER BY created_at DESC LIMIT 50")
        ]
        for run in runs:
            if run["state"] not in {"failed", "cancelled"}:
                continue
            latest = database.fetch_one(
                "SELECT stage_name, state, progress, attempt_number, retryable, error_code, error_summary "
                "FROM stage_attempts WHERE scope_type = 'analysis' AND scope_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (run["id"],),
            )
            run["latest_stage"] = _json_row(latest) if latest is not None else None
        queues = [
            _json_row(row)
            for row in database.fetch_all(
                "SELECT q.*, r.source_recording_id, r.configuration_json, r.requested_more_from_run_id, "
                "COUNT(e.clip_proposal_id) AS proposal_count "
                "FROM queue_snapshots q JOIN analysis_runs r ON r.id = q.analysis_run_id "
                "LEFT JOIN queue_entries e ON e.queue_snapshot_id = q.id "
                "GROUP BY q.id ORDER BY q.created_at DESC"
            )
        ]
        with app.state.task_lock:
            tasks = [dict(task) for task in app.state.analysis_tasks.values()]
        for task in tasks:
            run_id = task.get("analysis_run_id")
            if not run_id:
                continue
            stages = database.fetch_all(
                "SELECT s.id, s.stage_name, s.state, s.progress, s.attempt_number, s.worker_pid, "
                "s.retryable, s.error_code, s.error_summary FROM stage_attempts s "
                "WHERE s.scope_type = 'analysis' AND s.scope_id = ? AND s.attempt_number = "
                "(SELECT MAX(s2.attempt_number) FROM stage_attempts s2 WHERE s2.scope_type = s.scope_type "
                "AND s2.scope_id = s.scope_id AND s2.stage_name = s.stage_name) "
                "ORDER BY s.created_at",
                (run_id,),
            )
            if stages:
                by_name = {str(stage["stage_name"]): stage for stage in stages}
                current = next((stage for stage in reversed(stages) if stage["state"] == "running"), stages[-1])
                completed = sum(
                    name in by_name and by_name[name]["state"] == "succeeded" for name in stage_order
                )
                running_progress = float(current["progress"]) if current["state"] == "running" else 0.0
                task["stage"] = _json_row(current)
                task["overall_progress"] = min(1.0, (completed + running_progress) / len(stage_order))
        return {
            "profiles": profiles,
            "sources": sources,
            "runs": runs,
            "queues": queues,
            "tasks": tasks,
        }

    @app.post("/api/profiles", status_code=201)
    def create_profile(payload: ProfileRequest) -> dict[str, str]:
        try:
            profile_id = save_creator_profile(database, **payload.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"id": profile_id}

    @app.post("/api/sources/{source_id}/analyses", status_code=202)
    def start_analysis(source_id: str, payload: AnalysisRequest) -> dict[str, str]:
        selection = AnalysisSelection(
            mode=payload.mode,
            asr_profile=payload.asr_profile,
            asr_language=payload.asr_language,
            embedding_profile=payload.embedding_profile,
            evaluator_profile=payload.evaluator_profile,
            evaluator_context_size=payload.evaluator_context_size,
            evaluator_mtp=payload.evaluator_mtp,
            fake_transcript=payload.fake_transcript,
        )
        return {"task_id": launch_analysis_task(source_id, selection)}

    @app.post("/api/analysis-tasks/{task_id}/cancel", status_code=202)
    def cancel_analysis(task_id: str) -> dict[str, bool]:
        with app.state.task_lock:
            task = app.state.analysis_tasks.get(task_id)
            if task is None:
                raise HTTPException(404, "Analysis task not found")
            if task["state"] not in {"pending", "running"}:
                raise HTTPException(409, "Analysis task is no longer running")
            task["cancel_requested"] = True
            run_id = task.get("analysis_run_id")
        if run_id:
            attempt = database.fetch_one(
                "SELECT id FROM stage_attempts WHERE scope_type = 'analysis' AND scope_id = ? "
                "AND state = 'running' ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            )
            if attempt is not None:
                with suppress(ValueError):
                    AttemptStore(database).request_cancellation(str(attempt["id"]), "local_web")
        return {"cancellation_requested": True}

    @app.post("/api/analysis-runs/{run_id}/retry", status_code=202)
    def retry_analysis(run_id: str) -> dict[str, str]:
        run = database.fetch_one("SELECT * FROM analysis_runs WHERE id = ?", (run_id,))
        if run is None:
            raise HTTPException(404, "Analysis Run not found")
        if run["state"] not in {"failed", "cancelled"}:
            raise HTTPException(409, "Only a failed or cancelled Analysis Run can be retried")
        if run["state"] == "failed":
            latest = database.fetch_one(
                "SELECT stage_name, retryable FROM stage_attempts WHERE scope_type = 'analysis' "
                "AND scope_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            )
            if latest is None or not int(latest["retryable"]):
                raise HTTPException(409, "The failed stage requires changed inputs or configuration")
            failed_count = int(
                database.fetch_one(
                    "SELECT COUNT(*) AS count FROM stage_attempts WHERE scope_type = 'analysis' "
                    "AND scope_id = ? AND stage_name = ? AND state = 'failed'",
                    (run_id, latest["stage_name"]),
                )["count"]
            )
            if failed_count >= MAX_FAILED_STAGE_ATTEMPTS:
                raise HTTPException(409, "The failed stage reached its retry limit")
        selection = selection_from_configuration(json.loads(str(run["configuration_json"])))
        task_id = launch_analysis_task(str(run["source_recording_id"]), selection, resume_run_id=run_id)
        return {"task_id": task_id}

    @app.post("/api/queues/{queue_id}/more", status_code=202)
    def request_more(queue_id: str) -> dict[str, str]:
        parent = database.fetch_one(
            "SELECT q.id AS queue_snapshot_id, r.* FROM queue_snapshots q "
            "JOIN analysis_runs r ON r.id = q.analysis_run_id WHERE q.id = ?",
            (queue_id,),
        )
        if parent is None:
            raise HTTPException(404, "Review Queue not found")
        if parent["state"] != "succeeded":
            raise HTTPException(409, "Request More requires a completed Analysis Run")
        configuration = json.loads(str(parent["configuration_json"]))
        if configuration.get("budget_tier", "default") != "default":
            raise HTTPException(409, "This Review Queue already uses the expanded budget")
        proposal_count = int(
            database.fetch_one(
                "SELECT COUNT(*) AS count FROM queue_entries WHERE queue_snapshot_id = ?",
                (queue_id,),
            )["count"]
        )
        if proposal_count >= int(configuration.get("max_queue_size", 30)):
            raise HTTPException(409, "This Review Queue is already at its proposal cap")
        existing = database.fetch_one(
            "SELECT id FROM analysis_runs WHERE requested_more_from_run_id = ? "
            "AND state IN ('pending', 'running', 'succeeded') ORDER BY created_at DESC LIMIT 1",
            (parent["id"],),
        )
        if existing is not None:
            raise HTTPException(409, "An expanded Analysis Run already exists for this Review Queue")
        selection = replace(selection_from_configuration(configuration), budget_tier="expanded")
        task_id = launch_analysis_task(
            str(parent["source_recording_id"]),
            selection,
            creator_profile_revision_id=str(parent["creator_profile_revision_id"]),
            requested_more_from_run_id=str(parent["id"]),
        )
        return {"task_id": task_id}

    @app.post("/api/queues/{queue_id}/proposals/{proposal_id}/reanalyze-boundary", status_code=202)
    def reanalyze_boundary(queue_id: str, proposal_id: str) -> dict[str, str]:
        target = database.fetch_one(
            "SELECT q.analysis_run_id, r.source_recording_id, r.creator_profile_revision_id, "
            "r.configuration_json, d.id AS decision_id, b.id AS boundary_edit_id, "
            "b.start_us, b.end_us, b.outside_evaluated_context "
            "FROM queue_snapshots q JOIN analysis_runs r ON r.id = q.analysis_run_id "
            "JOIN queue_entries e ON e.queue_snapshot_id = q.id AND e.clip_proposal_id = ? "
            "JOIN editorial_decisions d ON d.clip_proposal_id = e.clip_proposal_id "
            "AND d.revision_number = (SELECT MAX(d2.revision_number) FROM editorial_decisions d2 "
            "WHERE d2.clip_proposal_id = e.clip_proposal_id) "
            "JOIN boundary_edits b ON b.editorial_decision_id = d.id "
            "WHERE q.id = ? AND d.decision <> 'withdrawn'",
            (proposal_id, queue_id),
        )
        if target is None:
            raise HTTPException(409, "Boundary reanalysis requires a current Boundary Edit")
        if not int(target["outside_evaluated_context"]):
            raise HTTPException(409, "The edited interval is already covered by evaluator evidence")
        if int(target["end_us"]) - int(target["start_us"]) > 240_000_000:
            raise HTTPException(409, "Boundary reanalysis supports edited clips up to 240 seconds")
        existing = database.fetch_one(
            "SELECT t.analysis_run_id FROM boundary_reanalysis_targets t "
            "JOIN analysis_runs r ON r.id = t.analysis_run_id "
            "WHERE t.parent_queue_snapshot_id = ? AND t.boundary_edit_id = ? "
            "AND r.state IN ('pending', 'running', 'succeeded')",
            (queue_id, target["boundary_edit_id"]),
        )
        if existing is not None:
            raise HTTPException(409, "This Boundary Edit already has a reanalysis run")
        selection = selection_from_configuration(json.loads(str(target["configuration_json"])))
        task_id = launch_analysis_task(
            str(target["source_recording_id"]),
            selection,
            creator_profile_revision_id=str(target["creator_profile_revision_id"]),
            boundary_reanalysis_queue_id=queue_id,
            boundary_reanalysis_proposal_id=proposal_id,
        )
        return {"task_id": task_id}

    @app.get("/api/queues/{queue_id}")
    def queue(queue_id: str) -> dict[str, object]:
        snapshot = database.fetch_one(
            "SELECT q.*, r.source_recording_id, r.configuration_json, r.requested_more_from_run_id, "
            "a.id AS proxy_artifact_id "
            "FROM queue_snapshots q JOIN analysis_runs r ON r.id = q.analysis_run_id "
            "LEFT JOIN artifacts a ON a.source_recording_id = r.source_recording_id "
            "AND a.kind = 'review_proxy' AND a.removed_at IS NULL WHERE q.id = ?",
            (queue_id,),
        )
        if snapshot is None:
            raise HTTPException(404, "Queue Snapshot not found")
        proposals: list[dict[str, object]] = []
        rows = database.fetch_all(
            "SELECT p.*, q.rank, q.baseline_score, e.start_us AS envelope_start_us, "
            "e.end_us AS envelope_end_us FROM queue_entries q "
            "JOIN clip_proposals p ON p.id = q.clip_proposal_id "
            "JOIN context_envelopes e ON e.id = p.context_envelope_id "
            "WHERE q.queue_snapshot_id = ? ORDER BY q.rank",
            (queue_id,),
        )
        for row in rows:
            proposal = _json_row(row)
            proposal["evidence"] = [
                _json_row(item)
                for item in database.fetch_all(
                    "SELECT e.* FROM evidence_items e JOIN proposal_evidence p "
                    "ON p.evidence_item_id = e.id WHERE p.clip_proposal_id = ? ORDER BY e.start_us",
                    (row["id"],),
                )
            ]
            proposal["risks"] = [
                _json_row(item)
                for item in database.fetch_all(
                    "SELECT risk_kind, reason FROM proposal_risks WHERE clip_proposal_id = ?",
                    (row["id"],),
                )
            ]
            decision = database.fetch_one(
                "SELECT d.*, b.start_us AS boundary_start_us, b.end_us AS boundary_end_us, "
                "b.outside_evaluated_context FROM editorial_decisions d "
                "LEFT JOIN boundary_edits b ON b.editorial_decision_id = d.id "
                "WHERE d.clip_proposal_id = ? ORDER BY d.revision_number DESC LIMIT 1",
                (row["id"],),
            )
            proposal["current_decision"] = _json_row(decision) if decision else None
            proposals.append(proposal)
        return {"snapshot": _json_row(snapshot), "proposals": proposals}

    @app.post("/api/proposals/{proposal_id}/decisions", status_code=201)
    def decide(proposal_id: str, payload: DecisionRequest) -> dict[str, object]:
        boundary = None
        if payload.boundary_start_seconds is not None and payload.boundary_end_seconds is not None:
            boundary = SourceInterval(
                seconds_to_us(payload.boundary_start_seconds),
                seconds_to_us(payload.boundary_end_seconds),
            )
        try:
            result = record_decision(
                database,
                proposal_id,
                payload.decision,
                idempotency_key=payload.idempotency_key,
                expected_prior_revision=payload.expected_prior_revision,
                rejection_reason=payload.rejection_reason,
                note=payload.note,
                boundary=boundary,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "decision_id": result.decision_id,
            "revision_number": result.revision_number,
            "decision": result.decision.value,
        }

    @app.post("/api/proposals/{proposal_id}/exports", status_code=201)
    def export(proposal_id: str, payload: ExportRequest) -> dict[str, object]:
        try:
            result = export_accepted_clip(
                database,
                proposal_id,
                idempotency_key=payload.idempotency_key,
                confirmed=payload.confirmed,
                expected_decision_revision=payload.expected_decision_revision,
                confirmed_risk=payload.confirmed_risk,
                confirmed_stale_coverage=payload.confirmed_stale_coverage,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"export_id": result.export_id, "sha256": result.sha256}

    @app.post("/api/review-activity", status_code=201)
    def record_review_activity(payload: ReviewActivityRequest) -> dict[str, object]:
        with database.transaction(immediate=True) as connection:
            membership = connection.execute(
                "SELECT 1 FROM queue_entries WHERE queue_snapshot_id = ? AND clip_proposal_id = ?",
                (payload.queue_snapshot_id, payload.clip_proposal_id),
            ).fetchone()
            if membership is None:
                raise HTTPException(409, "Proposal does not belong to the selected Review Queue")
            existing = connection.execute(
                "SELECT * FROM review_activity_events WHERE session_id = ? AND sequence_number = ?",
                (payload.session_id, payload.sequence_number),
            ).fetchone()
            if existing is not None:
                matches = (
                    existing["queue_snapshot_id"] == payload.queue_snapshot_id
                    and existing["clip_proposal_id"] == payload.clip_proposal_id
                    and int(existing["active_milliseconds"]) == payload.active_milliseconds
                    and existing["activity_kind"] == payload.activity_kind
                )
                if not matches:
                    raise HTTPException(409, "Review activity sequence was reused for different data")
                return {"id": existing["id"], "recorded": False}
            activity_id = new_id("review_activity")
            connection.execute(
                "INSERT INTO review_activity_events "
                "(id, queue_snapshot_id, clip_proposal_id, session_id, sequence_number, "
                "active_milliseconds, activity_kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    activity_id,
                    payload.queue_snapshot_id,
                    payload.clip_proposal_id,
                    payload.session_id,
                    payload.sequence_number,
                    payload.active_milliseconds,
                    payload.activity_kind,
                    utc_now(),
                ),
            )
        return {"id": activity_id, "recorded": True}

    @app.get("/api/sources/{source_id}/references")
    def references(source_id: str) -> dict[str, object]:
        source = database.fetch_one(
            "SELECT id, original_name, source_end_us FROM source_recordings WHERE id = ?",
            (source_id,),
        )
        if source is None:
            raise HTTPException(404, "Source Recording not found")
        rows = database.fetch_all(
            "SELECT * FROM reference_moment_revisions WHERE source_recording_id = ? "
            "ORDER BY annotation_set_id, revision_number DESC",
            (source_id,),
        )
        latest: dict[str, dict[str, object]] = {}
        for row in rows:
            latest.setdefault(str(row["annotation_set_id"]), _json_row(row))
        return {"source": _json_row(source), "references": list(latest.values())}

    @app.post("/api/sources/{source_id}/references", status_code=201)
    def create_reference(source_id: str, payload: ReferenceRequest) -> dict[str, object]:
        source = database.fetch_one("SELECT source_end_us FROM source_recordings WHERE id = ?", (source_id,))
        if source is None:
            raise HTTPException(404, "Source Recording not found")
        try:
            interval = SourceInterval(
                seconds_to_us(payload.start_seconds), seconds_to_us(payload.end_seconds)
            ).validate_within(int(source["source_end_us"]))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        event_us = seconds_to_us(payload.event_seconds)
        if not interval.contains_point(event_us):
            raise HTTPException(422, "Reference Event must be inside its interval")
        annotation_set_id = payload.annotation_set_id or new_id("reference_set")
        with database.transaction(immediate=True) as connection:
            current = connection.execute(
                "SELECT revision_number, frozen, source_recording_id FROM reference_moment_revisions "
                "WHERE annotation_set_id = ? ORDER BY revision_number DESC LIMIT 1",
                (annotation_set_id,),
            ).fetchone()
            current_revision = int(current["revision_number"]) if current else 0
            if current and current["source_recording_id"] != source_id:
                raise HTTPException(409, "Reference Moment belongs to a different Source Recording")
            if current and int(current["frozen"]):
                raise HTTPException(409, "Frozen Reference Moment revisions cannot be changed")
            if current_revision != payload.expected_prior_revision:
                raise HTTPException(409, "Reference Moment changed concurrently")
            reference_id = new_id("reference")
            revision = current_revision + 1
            connection.execute(
                "INSERT INTO reference_moment_revisions "
                "(id, source_recording_id, annotation_set_id, revision_number, certainty, category, "
                "start_us, end_us, event_us, short_form_suitability, rationale, language_slice, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reference_id,
                    source_id,
                    annotation_set_id,
                    revision,
                    payload.certainty,
                    payload.category.value,
                    interval.start_us,
                    interval.end_us,
                    event_us,
                    payload.short_form_suitability,
                    payload.rationale,
                    payload.language_slice,
                    utc_now(),
                ),
            )
        return {
            "id": reference_id,
            "annotation_set_id": annotation_set_id,
            "revision_number": revision,
        }

    @app.post("/api/references/{annotation_set_id}/freeze")
    def freeze_reference(annotation_set_id: str) -> dict[str, bool]:
        with database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT id FROM reference_moment_revisions WHERE annotation_set_id = ? "
                "ORDER BY revision_number DESC LIMIT 1",
                (annotation_set_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Reference Moment not found")
            connection.execute("UPDATE reference_moment_revisions SET frozen = 1 WHERE id = ?", (row["id"],))
        return {"frozen": True}

    def artifact_path(artifact_id: str) -> tuple[Path, str]:
        artifact = database.fetch_one(
            "SELECT * FROM artifacts WHERE id = ? AND removed_at IS NULL",
            (artifact_id,),
        )
        if artifact is None:
            raise HTTPException(404, "Media artifact not found")
        kind = str(artifact["kind"])
        if kind not in WEB_MEDIA_KINDS:
            raise HTTPException(404, "Media artifact not found")
        try:
            path = local_settings.resolve_work_path(str(artifact["relative_path"]))
        except ValueError as exc:
            raise HTTPException(404, "Media artifact is unavailable") from exc
        if not path.is_file():
            raise HTTPException(404, "Media artifact is unavailable")
        stat = path.stat()
        cache_key = (stat.st_size, stat.st_mtime_ns, str(artifact["sha256"] or ""))
        with app.state.media_integrity_lock:
            cached = app.state.media_integrity_cache.get(artifact_id)
        if cached != cache_key:
            try:
                ArtifactStore(database).require_intact(artifact)
            except RuntimeError as exc:
                with app.state.media_integrity_lock:
                    app.state.media_integrity_cache.pop(artifact_id, None)
                raise HTTPException(409, "Media artifact failed its integrity check") from exc
            with app.state.media_integrity_lock:
                app.state.media_integrity_cache[artifact_id] = cache_key
        return path, kind

    def ranged_response(request: Request, artifact_id: str, *, head: bool = False):
        path, _ = artifact_path(artifact_id)
        size = path.stat().st_size
        range_header = request.headers.get("range")
        start, end, status = 0, size - 1, 200
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            value = range_header.removeprefix("bytes=")
            left, separator, right = value.partition("-")
            if not separator:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            try:
                if left:
                    start = int(left)
                    end = min(int(right) if right else size - 1, size - 1)
                else:
                    suffix = int(right)
                    start = max(0, size - suffix)
                    end = size - 1
            except ValueError:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            if start < 0 or end < start or start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            status = 206
        content_length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if head:
            return Response(status_code=status, headers=headers)

        def body():
            remaining = content_length
            with path.open("rb") as handle:
                handle.seek(start)
                while remaining:
                    chunk = handle.read(min(MAX_RANGE_BUFFER, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(body(), status_code=status, headers=headers)

    @app.get("/api/media/{artifact_id}")
    def media_get(request: Request, artifact_id: str):
        return ranged_response(request, artifact_id)

    @app.head("/api/media/{artifact_id}")
    def media_head(request: Request, artifact_id: str):
        return ranged_response(request, artifact_id, head=True)

    @app.get("/api/sources/{source_id}/waveform")
    def waveform(
        source_id: str,
        bins: int = Query(default=800, ge=100, le=2000),
        start_us: int = Query(default=0, ge=0),
        end_us: int | None = Query(default=None, gt=0),
    ) -> dict[str, object]:
        artifact = database.fetch_one(
            "SELECT a.*, s.source_end_us FROM artifacts a JOIN source_recordings s ON s.id = a.source_recording_id "
            "WHERE a.source_recording_id = ? AND a.kind = 'waveform_peaks' AND a.removed_at IS NULL "
            "ORDER BY a.created_at DESC LIMIT 1",
            (source_id,),
        )
        if artifact is None:
            raise HTTPException(404, "Waveform cache not found; run setup to rebuild derived caches")
        source_end_us = int(artifact["source_end_us"])
        requested_end = source_end_us if end_us is None else min(end_us, source_end_us)
        if start_us >= requested_end:
            raise HTTPException(416, "Waveform range is outside the Source Recording")
        try:
            path = ArtifactStore(database).require_intact(artifact)
            integrity = json.loads(str(artifact["integrity_json"]))
            peaks, actual_start, actual_end = read_waveform_peaks(
                path,
                integrity,
                start_us=start_us,
                end_us=requested_end,
                max_bins=bins,
            )
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, "Waveform cache failed validation") from exc
        return {
            "bins": peaks,
            "start_us": actual_start,
            "end_us": min(actual_end, source_end_us),
            "source_end_us": source_end_us,
            "native_bin_us": int(integrity["native_bin_us"]),
        }

    return app

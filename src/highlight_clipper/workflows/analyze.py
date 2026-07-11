from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import TypeVar

from ..analysis.ranking import (
    SEMANTIC_DUPLICATE_THRESHOLD,
    TEMPORAL_DUPLICATE_THRESHOLD,
    default_queue_size,
    rank_proposals,
)
from ..analysis.retrieval import (
    CATEGORY_EMBEDDING_QUERIES,
    CandidateDraft,
    audio_peak_candidates,
    build_transcript_windows,
    embedding_candidates,
    lexical_candidates,
    normalize_text,
    novelty_candidates,
    speech_activity_candidates,
    transcript_window_key,
)
from ..artifacts import ArtifactStore, sha256_file
from ..attempts import AttemptStore
from ..database import Database, utc_now
from ..domain import (
    AttemptState,
    canonical_json,
    fingerprint,
    new_id,
)
from ..ports import (
    CANDIDATE_OUTCOMES,
    AsrAdapter,
    CandidateEvaluationOutcome,
    EmbeddingAdapter,
    EmbeddingItem,
    EvaluationOutcome,
    EvaluatorAdapter,
    EvaluatorExecutionError,
)
from ..recovery import OperationLeaseHeartbeat, current_owner_identity
from ..workers.supervisor import WorkerCancelled, WorkerError


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    asr_profile: str = "fake-v1"
    asr_language: str | None = None
    embedding_profile: str = "none"
    evaluator_profile: str = "fake-v1"
    evaluator_context_size: int = 32_768
    evaluator_mtp: bool = False
    evaluator_prompt_version: str = "anchored-json-v7"
    asr_execution_identity: str = "fake-asr-v1"
    embedding_execution_identity: str = "none"
    evaluator_execution_identity: str = "fake-evaluator-v1"
    retrieval_version: str = "text-audio-speech-section-balanced-v3"
    ranking_version: str = "fixed-diverse-profile-v3"
    budget_tier: str = "default"
    max_queue_size: int = 30
    max_prompt_tokens_per_source_hour: int = 100_000
    max_prompt_tokens_per_run: int = 1_000_000

    def persisted(self) -> dict[str, object]:
        configuration = asdict(self)
        if self.asr_language is None:
            configuration.pop("asr_language")
        return configuration


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    analysis_run_id: str
    queue_snapshot_id: str
    proposal_count: int


T = TypeVar("T")


class AnalysisCancelled(RuntimeError):
    pass


MAX_FAILED_STAGE_ATTEMPTS = 3


def _stage_failure_policy(name: str, error: BaseException) -> tuple[bool, str]:
    if isinstance(error, EvaluatorExecutionError):
        return True, f"{name}_transport_failed"
    if isinstance(error, WorkerError):
        return True, f"{name}_worker_failed"
    if isinstance(error, (sqlite3.OperationalError, TimeoutError)):
        return True, f"{name}_transient_io"
    if isinstance(error, OSError) and (
        getattr(error, "winerror", None) in {32, 33, 10053, 10054, 10060}
        or getattr(error, "errno", None) in {11, 16, 26}
    ):
        return True, f"{name}_transient_io"
    if isinstance(error, ValueError):
        return False, f"{name}_invalid_output"
    return False, f"{name}_precondition_failed"


def _union_duration_us(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


SECTION_DURATION_US = 15 * 60 * 1_000_000
MAX_CONTEXT_ENVELOPE_US = 5 * 60 * 1_000_000


def _context_envelope_bounds(
    candidate_start_us: int,
    candidate_end_us: int,
    source_end_us: int,
) -> tuple[int, int]:
    candidate_start_us = max(0, min(candidate_start_us, source_end_us - 1))
    candidate_end_us = max(candidate_start_us + 1, min(candidate_end_us, source_end_us))
    start_us = max(0, candidate_start_us - 90_000_000)
    end_us = min(source_end_us, candidate_end_us + 150_000_000)
    if end_us - start_us <= MAX_CONTEXT_ENVELOPE_US:
        return start_us, end_us

    candidate_span_us = candidate_end_us - candidate_start_us
    if candidate_span_us >= MAX_CONTEXT_ENVELOPE_US:
        midpoint_us = (candidate_start_us + candidate_end_us) // 2
        start_us = max(0, midpoint_us - MAX_CONTEXT_ENVELOPE_US // 2)
    else:
        padding_us = MAX_CONTEXT_ENVELOPE_US - candidate_span_us
        before_us = min(90_000_000, padding_us * 3 // 8)
        start_us = max(0, candidate_start_us - before_us)
        if start_us + MAX_CONTEXT_ENVELOPE_US < candidate_end_us:
            start_us = candidate_end_us - MAX_CONTEXT_ENVELOPE_US
    end_us = min(source_end_us, start_us + MAX_CONTEXT_ENVELOPE_US)
    start_us = max(0, end_us - MAX_CONTEXT_ENVELOPE_US)
    return start_us, end_us


def _cluster_traits(members: list) -> tuple[int, int, frozenset[str], frozenset[str], tuple[str, ...]]:
    anchors = [int(member["anchor_us"]) for member in members]
    anchor = (min(anchors) + max(anchors)) // 2
    categories = frozenset(str(member["category_hint"]) for member in members if member["category_hint"])
    generators = frozenset(str(member["generator_name"]) for member in members)
    member_ids = tuple(sorted(str(member["id"]) for member in members))
    return anchor, anchor // SECTION_DURATION_US, categories, generators, member_ids


def _spread_section_order(sections: list[int]) -> list[int]:
    remaining = set(sections)
    ordered: list[int] = []
    while remaining:
        if not ordered:
            selected = min(remaining)
        else:
            selected = max(
                remaining,
                key=lambda section: (min(abs(section - prior) for prior in ordered), -section),
            )
        ordered.append(selected)
        remaining.remove(selected)
    return ordered


def _balanced_cluster_order(clusters: list[list]) -> list[list]:
    if not clusters:
        return []
    local_rank: dict[str, int] = {}
    by_generator_category: dict[tuple[str, str], list] = {}
    for members in clusters:
        for member in members:
            key = (str(member["generator_name"]), str(member["category_hint"] or ""))
            by_generator_category.setdefault(key, []).append(member)
    for values in by_generator_category.values():
        values.sort(
            key=lambda member: (
                -float(member["local_confidence"]),
                int(member["anchor_us"]),
                str(member["id"]),
            )
        )
        for rank, member in enumerate(values):
            local_rank[str(member["id"])] = rank

    buckets: dict[int, list[list]] = {}
    for members in clusters:
        section = _cluster_traits(members)[1]
        buckets.setdefault(section, []).append(members)
    section_order = _spread_section_order(sorted(buckets))
    seen_categories: set[str] = set()
    seen_generators: set[str] = set()
    ordered: list[list] = []
    while any(buckets.values()):
        for section in section_order:
            values = buckets[section]
            if not values:
                continue

            def priority(members: list) -> tuple:
                anchor, _, categories, generators, member_ids = _cluster_traits(members)
                best_local_rank = min(local_rank[member_id] for member_id in member_ids)
                return (
                    -len(categories - seen_categories),
                    -len(generators - seen_generators),
                    best_local_rank,
                    -len(categories),
                    -len(generators),
                    anchor,
                    member_ids,
                )

            selected = min(values, key=priority)
            values.remove(selected)
            ordered.append(selected)
            _, _, categories, generators, _ = _cluster_traits(selected)
            seen_categories.update(categories)
            seen_generators.update(generators)
    return ordered


class AnalysisWorkflow:
    def __init__(
        self,
        database: Database,
        asr: AsrAdapter,
        evaluator: EvaluatorAdapter,
        embedding: EmbeddingAdapter | None = None,
        configuration: AnalysisConfig | None = None,
        external_cancellation_requested: Callable[[], bool] | None = None,
    ):
        self.database = database
        self.asr = asr
        self.evaluator = evaluator
        self.embedding = embedding
        self.configuration = configuration or AnalysisConfig()
        if self.configuration.budget_tier not in {"default", "expanded"}:
            raise ValueError("Analysis budget tier must be default or expanded")
        self.external_cancellation_requested = external_cancellation_requested
        self.attempts = AttemptStore(database)
        self.owner = current_owner_identity()
        self._active_attempt_id: str | None = None

    def _report_stage_progress(self, progress: float, *, worker_pid: int | None = None) -> None:
        if self._active_attempt_id is not None:
            self.attempts.update_running(
                self._active_attempt_id,
                progress=progress,
                worker_pid=worker_pid,
            )

    def _worker_started(self, worker_pid: int) -> None:
        self._report_stage_progress(0.02, worker_pid=worker_pid)

    def run(
        self,
        source_recording_id: str,
        *,
        creator_profile_revision_id: str | None = None,
        requested_more_from_run_id: str | None = None,
        boundary_reanalysis_queue_id: str | None = None,
        boundary_reanalysis_proposal_id: str | None = None,
        resume_run_id: str | None = None,
        run_started: Callable[[str], None] | None = None,
    ) -> AnalysisResult:
        source = self.database.fetch_one("SELECT * FROM source_recordings WHERE id = ?", (source_recording_id,))
        if source is None:
            raise KeyError(f"Unknown Source Recording: {source_recording_id}")
        if (boundary_reanalysis_queue_id is None) != (boundary_reanalysis_proposal_id is None):
            raise ValueError("Boundary reanalysis requires both a Review Queue and Clip Proposal")
        if requested_more_from_run_id is not None and boundary_reanalysis_queue_id is not None:
            raise ValueError("Request More and boundary reanalysis are separate Analysis Runs")
        resumed_run = None
        requested_more_run = None
        boundary_target = None
        boundary_parent_run = None
        if resume_run_id is not None:
            resumed_run = self.database.fetch_one("SELECT * FROM analysis_runs WHERE id = ?", (resume_run_id,))
            if resumed_run is None:
                raise KeyError(f"Unknown Analysis Run: {resume_run_id}")
            if resumed_run["source_recording_id"] != source_recording_id:
                raise ValueError("Analysis Run does not belong to the requested Source Recording")
            creator_profile_revision_id = str(resumed_run["creator_profile_revision_id"])
            requested_more_from_run_id = resumed_run["requested_more_from_run_id"]
            boundary_target = self.database.fetch_one(
                "SELECT * FROM boundary_reanalysis_targets WHERE analysis_run_id = ?",
                (resume_run_id,),
            )
            if boundary_target is not None:
                boundary_reanalysis_queue_id = str(boundary_target["parent_queue_snapshot_id"])
                boundary_reanalysis_proposal_id = str(boundary_target["superseded_proposal_id"])
        if requested_more_from_run_id is not None:
            requested_more_run = self.database.fetch_one(
                "SELECT * FROM analysis_runs WHERE id = ?",
                (requested_more_from_run_id,),
            )
            if requested_more_run is None:
                raise KeyError(f"Unknown parent Analysis Run: {requested_more_from_run_id}")
            if requested_more_run["state"] != AttemptState.SUCCEEDED.value:
                raise ValueError("Request More requires a succeeded parent Analysis Run")
            if requested_more_run["source_recording_id"] != source_recording_id:
                raise ValueError("Request More parent belongs to a different Source Recording")
            if creator_profile_revision_id is None:
                creator_profile_revision_id = str(requested_more_run["creator_profile_revision_id"])
            elif creator_profile_revision_id != requested_more_run["creator_profile_revision_id"]:
                raise ValueError("Request More must preserve the parent Creator Profile revision")
        if boundary_reanalysis_queue_id is not None and boundary_target is None:
            boundary_target = self.database.fetch_one(
                "SELECT q.analysis_run_id AS parent_analysis_run_id, q.id AS parent_queue_snapshot_id, "
                "r.source_recording_id, r.creator_profile_revision_id, r.configuration_json, "
                "p.id AS superseded_proposal_id, d.id AS source_editorial_decision_id, "
                "b.id AS boundary_edit_id, b.start_us AS requested_start_us, b.end_us AS requested_end_us "
                "FROM queue_snapshots q JOIN analysis_runs r ON r.id = q.analysis_run_id "
                "JOIN queue_entries qe ON qe.queue_snapshot_id = q.id "
                "JOIN clip_proposals p ON p.id = qe.clip_proposal_id "
                "JOIN editorial_decisions d ON d.clip_proposal_id = p.id AND d.revision_number = "
                "(SELECT MAX(d2.revision_number) FROM editorial_decisions d2 WHERE d2.clip_proposal_id = p.id) "
                "JOIN boundary_edits b ON b.editorial_decision_id = d.id "
                "WHERE q.id = ? AND p.id = ? AND b.outside_evaluated_context = 1 "
                "AND d.decision <> 'withdrawn'",
                (boundary_reanalysis_queue_id, boundary_reanalysis_proposal_id),
            )
            if boundary_target is None:
                raise ValueError("Boundary reanalysis requires the latest outside-context Boundary Edit")
            if boundary_target["source_recording_id"] != source_recording_id:
                raise ValueError("Boundary reanalysis target belongs to a different Source Recording")
            if creator_profile_revision_id is None:
                creator_profile_revision_id = str(boundary_target["creator_profile_revision_id"])
            elif creator_profile_revision_id != boundary_target["creator_profile_revision_id"]:
                raise ValueError("Boundary reanalysis must preserve the parent Creator Profile revision")
        if boundary_target is not None:
            boundary_parent_run = self.database.fetch_one(
                "SELECT r.* FROM queue_snapshots q JOIN analysis_runs r ON r.id = q.analysis_run_id "
                "WHERE q.id = ?",
                (boundary_target["parent_queue_snapshot_id"],),
            )
            if boundary_parent_run is None or boundary_parent_run["state"] != AttemptState.SUCCEEDED.value:
                raise ValueError("Boundary reanalysis requires a succeeded parent Analysis Run")
        if creator_profile_revision_id is None:
            profile = self.database.fetch_one(
                "SELECT * FROM creator_profile_revisions ORDER BY revision_number DESC LIMIT 1"
            )
        else:
            profile = self.database.fetch_one(
                "SELECT * FROM creator_profile_revisions WHERE id = ?",
                (creator_profile_revision_id,),
            )
        if profile is None:
            raise RuntimeError("A Creator Profile revision is required before analysis")

        run_id = resume_run_id or new_id("analysis")
        config_json = self.configuration.persisted()
        if requested_more_run is not None:
            parent_configuration = json.loads(str(requested_more_run["configuration_json"]))
            if parent_configuration.get("budget_tier", "default") != "default":
                raise ValueError("Only a default-budget Analysis Run can be expanded")
            if self.configuration.budget_tier != "expanded":
                raise ValueError("Request More must use the expanded budget tier")
            for key, value in config_json.items():
                if key != "budget_tier" and parent_configuration.get(key) != value:
                    raise ValueError(f"Request More cannot change analysis setting: {key}")
            parent_count = int(
                self.database.fetch_one(
                    "SELECT COUNT(*) AS count FROM queue_entries WHERE queue_snapshot_id = ?",
                    (requested_more_run["queue_snapshot_id"],),
                )["count"]
            )
            if parent_count >= self.configuration.max_queue_size:
                raise ValueError("The parent Review Queue is already at its proposal cap")
        if boundary_parent_run is not None:
            parent_configuration = json.loads(str(boundary_parent_run["configuration_json"]))
            for key, value in config_json.items():
                if parent_configuration.get(key) != value:
                    raise ValueError(f"Boundary reanalysis cannot change analysis setting: {key}")
        input_fingerprint = fingerprint(
            {
                "source_id": source_recording_id,
                "source_sha256": source["sha256"],
                "profile": {
                    "id": profile["id"],
                    "languages_json": profile["languages_json"],
                    "category_priorities_json": profile["category_priorities_json"],
                    "desired_content": profile["desired_content"],
                    "avoided_content": profile["avoided_content"],
                    "preferred_durations_json": profile["preferred_durations_json"],
                },
                "request_more": (
                    {
                        "parent_analysis_run_id": requested_more_run["id"],
                        "parent_queue_snapshot_id": requested_more_run["queue_snapshot_id"],
                        "parent_input_fingerprint": requested_more_run["input_fingerprint"],
                    }
                    if requested_more_run is not None
                    else None
                ),
                "boundary_reanalysis": (
                    {
                        "parent_queue_snapshot_id": boundary_target["parent_queue_snapshot_id"],
                        "superseded_proposal_id": boundary_target["superseded_proposal_id"],
                        "source_editorial_decision_id": boundary_target["source_editorial_decision_id"],
                        "boundary_edit_id": boundary_target["boundary_edit_id"],
                        "requested_start_us": boundary_target["requested_start_us"],
                        "requested_end_us": boundary_target["requested_end_us"],
                    }
                    if boundary_target is not None
                    else None
                ),
            }
        )
        config_fingerprint = fingerprint(config_json)
        with self.database.transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM active_operation_lease").fetchone():
                raise RuntimeError("Another Source Import or Analysis operation is active")
            if resumed_run is None:
                connection.execute(
                    "INSERT INTO analysis_runs "
                    "(id, source_recording_id, creator_profile_revision_id, state, input_fingerprint, "
                    "configuration_fingerprint, configuration_json, requested_more_from_run_id, created_at) "
                    "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        source_recording_id,
                        profile["id"],
                        input_fingerprint,
                        config_fingerprint,
                        canonical_json(config_json),
                        requested_more_from_run_id,
                        utc_now(),
                    ),
                )
                if boundary_target is not None:
                    connection.execute(
                        "INSERT INTO boundary_reanalysis_targets "
                        "(analysis_run_id, parent_queue_snapshot_id, superseded_proposal_id, "
                        "source_editorial_decision_id, boundary_edit_id, requested_start_us, "
                        "requested_end_us, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            boundary_target["parent_queue_snapshot_id"],
                            boundary_target["superseded_proposal_id"],
                            boundary_target["source_editorial_decision_id"],
                            boundary_target["boundary_edit_id"],
                            boundary_target["requested_start_us"],
                            boundary_target["requested_end_us"],
                            utc_now(),
                        ),
                    )
            else:
                if resumed_run["state"] not in {"failed", "cancelled"}:
                    raise RuntimeError("Only a failed or cancelled Analysis Run can be retried")
                if resumed_run["configuration_fingerprint"] != config_fingerprint:
                    raise ValueError("Retry must use the Analysis Run's original configuration")
                connection.execute(
                    "UPDATE analysis_runs SET state = 'running', completed_at = NULL WHERE id = ?",
                    (run_id,),
                )
            connection.execute(
                "INSERT INTO active_operation_lease "
                "(singleton, operation_type, operation_id, owner_instance, heartbeat_at) "
                "VALUES (1, 'analysis', ?, ?, ?)",
                (run_id, self.owner, utc_now()),
            )

        if run_started is not None:
            run_started(run_id)
        heartbeat: OperationLeaseHeartbeat | None = OperationLeaseHeartbeat(self.database, run_id, self.owner)
        heartbeat.__enter__()
        try:
            reuse_parent_run_id = (
                str(requested_more_run["id"])
                if requested_more_run is not None
                else (
                    str(boundary_parent_run["id"])
                    if boundary_parent_run is not None
                    else None
                )
            )
            if reuse_parent_run_id is None:
                self._stage(run_id, "asr", lambda cancel: self._transcribe(run_id, source, cancel))
                self._stage(run_id, "embeddings", lambda cancel: self._embed(run_id, source, cancel))
            else:
                self._reuse_stage(run_id, "asr", reuse_parent_run_id)
                self._reuse_stage(run_id, "embeddings", reuse_parent_run_id)
            self._stage(run_id, "discovery", lambda cancel: self._discover(run_id, source, cancel))
            self._stage(run_id, "envelopes", lambda cancel: self._create_envelopes(run_id, source, cancel))
            self._stage(run_id, "evaluation", lambda cancel: self._evaluate(run_id, source, cancel))
            queue_id = self._stage(
                run_id,
                "ranking",
                lambda cancel: self._rank(run_id, cancel),
                completed_value=lambda: str(
                    self.database.fetch_one("SELECT id FROM queue_snapshots WHERE analysis_run_id = ?", (run_id,))["id"]
                ),
            )
            proposal_count = int(
                self.database.fetch_one(
                    "SELECT COUNT(*) AS count FROM queue_entries WHERE queue_snapshot_id = ?",
                    (queue_id,),
                )["count"]
            )
            heartbeat.assert_owned()
            heartbeat.__exit__(None, None, None)
            heartbeat = None
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE analysis_runs SET state = 'succeeded', queue_snapshot_id = ?, "
                    "completed_at = ? WHERE id = ?",
                    (queue_id, utc_now(), run_id),
                )
                connection.execute(
                    "DELETE FROM active_operation_lease WHERE singleton = 1 AND operation_id = ?",
                    (run_id,),
                )
            return AnalysisResult(run_id, queue_id, proposal_count)
        except (AnalysisCancelled, WorkerCancelled) as exc:
            if heartbeat is not None:
                heartbeat.__exit__(type(exc), exc, exc.__traceback__)
                heartbeat = None
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE analysis_runs SET state = 'cancelled', completed_at = ? WHERE id = ? AND state = 'running'",
                    (utc_now(), run_id),
                )
                connection.execute(
                    "DELETE FROM active_operation_lease WHERE singleton = 1 AND operation_id = ? "
                    "AND owner_instance = ?",
                    (run_id, self.owner),
                )
            raise AnalysisCancelled(str(exc)) from exc
        except BaseException as exc:
            if heartbeat is not None:
                heartbeat.__exit__(type(exc), exc, exc.__traceback__)
                heartbeat = None
            with self.database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE analysis_runs SET state = 'failed', completed_at = ? WHERE id = ? AND state = 'running'",
                    (utc_now(), run_id),
                )
                connection.execute(
                    "DELETE FROM active_operation_lease WHERE singleton = 1 AND operation_id = ? "
                    "AND owner_instance = ?",
                    (run_id, self.owner),
                )
            raise

    def _stage(
        self,
        run_id: str,
        name: str,
        operation: Callable[[Callable[[], bool]], T],
        *,
        completed_value: Callable[[], T] | None = None,
    ) -> T:
        input_fingerprint = self._stage_input_fingerprint(run_id, name)
        configuration_fingerprint = fingerprint(
            {"stage": name, "configuration": self.configuration.persisted()}
        )
        prior = self.database.fetch_one(
            "SELECT * FROM stage_attempts WHERE scope_type = 'analysis' AND scope_id = ? "
            "AND stage_name = ? ORDER BY attempt_number DESC LIMIT 1",
            (run_id, name),
        )
        succeeded = self.database.fetch_one(
            "SELECT id FROM stage_attempts WHERE scope_type = 'analysis' AND scope_id = ? "
            "AND stage_name = ? AND state = 'succeeded' AND input_fingerprint = ? "
            "AND configuration_fingerprint = ? ORDER BY attempt_number DESC LIMIT 1",
            (run_id, name, input_fingerprint, configuration_fingerprint),
        )
        if succeeded is not None:
            if name in {"asr", "embeddings"} and not self._stage_output_complete(run_id, name):
                raise RuntimeError(
                    f"Completed {name} output is missing or failed integrity; start a new Analysis Run"
                )
            return completed_value() if completed_value is not None else None  # type: ignore[return-value]
        if (
            prior is not None
            and prior["input_fingerprint"] == input_fingerprint
            and prior["configuration_fingerprint"] == configuration_fingerprint
            and self._stage_output_complete(run_id, name)
        ):
            recovered = self.attempts.create(
                scope_type="analysis",
                scope_id=run_id,
                stage_name=name,
                input_fingerprint=input_fingerprint,
                configuration_fingerprint=configuration_fingerprint,
                prior_attempt_id=str(prior["id"]),
                checkpoint={"recovered_committed_output_from": str(prior["id"])},
            )
            self.attempts.transition(recovered.id, AttemptState.RUNNING, owner_instance=self.owner)
            self.attempts.transition(recovered.id, AttemptState.SUCCEEDED)
            return completed_value() if completed_value is not None else None  # type: ignore[return-value]
        if prior is not None and prior["state"] == AttemptState.FAILED.value:
            if not int(prior["retryable"]):
                raise RuntimeError(
                    f"The failed {name} stage is not retryable without changing its inputs or configuration"
                )
            failed_count = int(
                self.database.fetch_one(
                    "SELECT COUNT(*) AS count FROM stage_attempts WHERE scope_type = 'analysis' "
                    "AND scope_id = ? AND stage_name = ? AND state = 'failed'",
                    (run_id, name),
                )["count"]
            )
            if failed_count >= MAX_FAILED_STAGE_ATTEMPTS:
                raise RuntimeError(
                    f"The {name} stage reached its {MAX_FAILED_STAGE_ATTEMPTS}-attempt retry limit"
                )
        attempt = self.attempts.create(
            scope_type="analysis",
            scope_id=run_id,
            stage_name=name,
            input_fingerprint=input_fingerprint,
            configuration_fingerprint=configuration_fingerprint,
            prior_attempt_id=str(prior["id"]) if prior is not None else None,
        )
        self.attempts.transition(attempt.id, AttemptState.RUNNING, owner_instance=self.owner)
        self._active_attempt_id = attempt.id

        def cancellation_requested() -> bool:
            externally_requested = (
                self.external_cancellation_requested is not None and self.external_cancellation_requested()
            )
            return externally_requested or self.attempts.cancellation_requested(attempt.id)

        try:
            if cancellation_requested():
                raise AnalysisCancelled("Analysis cancellation was requested")
            result = operation(cancellation_requested)
        except (AnalysisCancelled, WorkerCancelled) as exc:
            self.attempts.transition(
                attempt.id,
                AttemptState.CANCELLED,
                retryable=True,
                error_code=f"{name}_cancelled",
                error_summary=str(exc)[:2000],
            )
            raise
        except BaseException as exc:
            retryable, error_code = _stage_failure_policy(name, exc)
            self.attempts.transition(
                attempt.id,
                AttemptState.FAILED,
                retryable=retryable,
                error_code=error_code,
                error_summary=str(exc)[:2000],
            )
            raise
        else:
            self.attempts.transition(attempt.id, AttemptState.SUCCEEDED)
            return result
        finally:
            self._active_attempt_id = None

    def _stage_output_complete(self, run_id: str, name: str) -> bool:
        if name == "asr":
            artifact = self.database.fetch_one(
                "SELECT * FROM artifacts WHERE owner_type = 'analysis' AND owner_id = ? "
                "AND kind = 'asr_raw_output' AND removed_at IS NULL",
                (run_id,),
            )
            if artifact is None:
                return False
            ArtifactStore(self.database).require_intact(artifact)
            run = self.database.fetch_one("SELECT source_recording_id FROM analysis_runs WHERE id = ?", (run_id,))
            if run is None:
                return False
            self._analysis_audio_path(str(run["source_recording_id"]))
            return True
        if name == "embeddings":
            if self.embedding is None:
                return True
            generation = self.database.fetch_one(
                "SELECT vector_artifact_id, manifest_artifact_id FROM embedding_generations "
                "WHERE analysis_run_id = ?",
                (run_id,),
            )
            if generation is None:
                transcript_count = int(
                    self.database.fetch_one(
                        "SELECT COUNT(*) AS count FROM transcript_segments WHERE analysis_run_id = ?",
                        (run_id,),
                    )["count"]
                )
                return transcript_count == 0
            artifacts = ArtifactStore(self.database)
            for artifact_id in (generation["vector_artifact_id"], generation["manifest_artifact_id"]):
                artifact = self.database.fetch_one(
                    "SELECT * FROM artifacts WHERE id = ? AND removed_at IS NULL",
                    (artifact_id,),
                )
                if artifact is None:
                    return False
                artifacts.require_intact(artifact)
            return True
        if name == "discovery":
            row = self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM candidate_moments WHERE analysis_run_id = ?",
                (run_id,),
            )
            run = self.database.fetch_one(
                "SELECT requested_more_from_run_id FROM analysis_runs WHERE id = ?",
                (run_id,),
            )
            return int(row["count"]) > 0 or (run is not None and run["requested_more_from_run_id"] is not None)
        if name == "envelopes":
            row = self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM context_envelopes WHERE analysis_run_id = ?",
                (run_id,),
            )
            run = self.database.fetch_one(
                "SELECT requested_more_from_run_id FROM analysis_runs WHERE id = ?",
                (run_id,),
            )
            return int(row["count"]) > 0 or (run is not None and run["requested_more_from_run_id"] is not None)
        if name == "evaluation":
            row = self.database.fetch_one(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN disposition IS NOT NULL THEN 1 ELSE 0 END) AS done "
                "FROM context_envelopes WHERE analysis_run_id = ?",
                (run_id,),
            )
            return int(row["total"]) == int(row["done"] or 0)
        if name == "ranking":
            return (
                self.database.fetch_one("SELECT 1 FROM queue_snapshots WHERE analysis_run_id = ?", (run_id,))
                is not None
            )
        return False

    def _producer_run_id(self, run_id: str, stage_name: str) -> str:
        reuse = self.database.fetch_one(
            "SELECT producer_analysis_run_id FROM analysis_stage_reuses "
            "WHERE analysis_run_id = ? AND stage_name = ?",
            (run_id, stage_name),
        )
        return str(reuse["producer_analysis_run_id"]) if reuse is not None else run_id

    def _stage_output_fingerprint(self, run_id: str, stage_name: str) -> str:
        producer_run_id = self._producer_run_id(run_id, stage_name)
        artifacts = ArtifactStore(self.database)
        if stage_name == "asr":
            artifact = self.database.fetch_one(
                "SELECT * FROM artifacts WHERE owner_type = 'analysis' AND owner_id = ? "
                "AND kind = 'asr_raw_output' AND removed_at IS NULL",
                (producer_run_id,),
            )
            if artifact is None:
                raise RuntimeError("Reusable ASR output has no registered raw artifact")
            artifacts.require_intact(artifact)
            transcript = [
                tuple(row)
                for row in self.database.fetch_all(
                    "SELECT sequence_number, start_us, end_us, normalized_text, language, "
                    "producer_fingerprint FROM transcript_segments WHERE analysis_run_id = ? "
                    "ORDER BY sequence_number",
                    (producer_run_id,),
                )
            ]
            words = [
                tuple(row)
                for row in self.database.fetch_all(
                    "SELECT s.sequence_number, w.sequence_number, w.start_us, w.end_us, w.word "
                    "FROM transcript_words w JOIN transcript_segments s ON s.id = w.transcript_segment_id "
                    "WHERE s.analysis_run_id = ? ORDER BY s.sequence_number, w.sequence_number",
                    (producer_run_id,),
                )
            ]
            return fingerprint(
                {
                    "artifact_sha256": artifact["sha256"],
                    "transcript": transcript,
                    "words": words,
                }
            )
        if stage_name == "embeddings":
            if self.embedding is None:
                return fingerprint({"embedding_profile": "none"})
            generation = self.database.fetch_one(
                "SELECT * FROM embedding_generations WHERE analysis_run_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (producer_run_id,),
            )
            if generation is None:
                transcript_count = int(
                    self.database.fetch_one(
                        "SELECT COUNT(*) AS count FROM transcript_segments WHERE analysis_run_id = ?",
                        (self._producer_run_id(run_id, "asr"),),
                    )["count"]
                )
                if transcript_count == 0:
                    return fingerprint({"empty_transcript": True})
                raise RuntimeError("Reusable embedding stage has no registered generation")
            artifact_rows = []
            for artifact_id in (generation["vector_artifact_id"], generation["manifest_artifact_id"]):
                artifact = self.database.fetch_one(
                    "SELECT * FROM artifacts WHERE id = ? AND removed_at IS NULL",
                    (artifact_id,),
                )
                if artifact is None:
                    raise RuntimeError("Reusable embedding artifact is missing")
                artifacts.require_intact(artifact)
                artifact_rows.append((artifact["kind"], artifact["size_bytes"], artifact["sha256"]))
            return fingerprint(
                {
                    "model_profile": generation["model_profile"],
                    "input_fingerprint": generation["input_fingerprint"],
                    "configuration_fingerprint": generation["configuration_fingerprint"],
                    "artifacts": artifact_rows,
                }
            )
        raise ValueError(f"Stage {stage_name} cannot be reused")

    def _reuse_stage(self, run_id: str, stage_name: str, parent_run_id: str) -> None:
        existing = self.database.fetch_one(
            "SELECT 1 FROM analysis_stage_reuses WHERE analysis_run_id = ? AND stage_name = ?",
            (run_id, stage_name),
        )
        if existing is not None:
            self._stage_output_fingerprint(run_id, stage_name)
            return
        producer_run_id = self._producer_run_id(parent_run_id, stage_name)
        if not self._stage_output_complete(producer_run_id, stage_name):
            raise RuntimeError(f"Parent Analysis Run has no intact reusable {stage_name} output")
        producer_attempt = self.database.fetch_one(
            "SELECT id FROM stage_attempts WHERE scope_type = 'analysis' AND scope_id = ? "
            "AND stage_name = ? AND state = 'succeeded' ORDER BY attempt_number DESC LIMIT 1",
            (producer_run_id, stage_name),
        )
        if producer_attempt is None:
            raise RuntimeError(f"Parent Analysis Run has no successful {stage_name} attempt")
        output_fingerprint = self._stage_output_fingerprint(producer_run_id, stage_name)
        configuration = (
            {
                "profile": self.configuration.asr_profile,
                "execution_identity": self.configuration.asr_execution_identity,
            }
            if stage_name == "asr"
            else {
                "profile": self.configuration.embedding_profile,
                "execution_identity": self.configuration.embedding_execution_identity,
                "retrieval_version": self.configuration.retrieval_version,
            }
        )
        attempt = self.attempts.create(
            scope_type="analysis",
            scope_id=run_id,
            stage_name=stage_name,
            input_fingerprint=fingerprint(
                {
                    "producer_analysis_run_id": producer_run_id,
                    "output_fingerprint": output_fingerprint,
                }
            ),
            configuration_fingerprint=fingerprint(configuration),
            prior_attempt_id=None,
            checkpoint={
                "reused_from_analysis_run_id": producer_run_id,
                "reused_from_stage_attempt_id": str(producer_attempt["id"]),
                "output_fingerprint": output_fingerprint,
            },
        )
        self.attempts.transition(attempt.id, AttemptState.RUNNING, owner_instance=self.owner)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO analysis_stage_reuses "
                "(analysis_run_id, stage_name, producer_analysis_run_id, producer_stage_attempt_id, "
                "output_fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    stage_name,
                    producer_run_id,
                    producer_attempt["id"],
                    output_fingerprint,
                    utc_now(),
                ),
            )
        self.attempts.transition(attempt.id, AttemptState.SUCCEEDED)

    def _stage_input_fingerprint(self, run_id: str, name: str) -> str:
        run = self.database.fetch_one(
            "SELECT r.*, s.sha256 AS source_sha256, s.source_end_us "
            "FROM analysis_runs r JOIN source_recordings s ON s.id = r.source_recording_id "
            "WHERE r.id = ?",
            (run_id,),
        )
        if run is None:
            raise KeyError(f"Unknown Analysis Run: {run_id}")
        payload: dict[str, object] = {
            "stage": name,
            "source_sha256": run["source_sha256"],
            "source_end_us": run["source_end_us"],
            "creator_profile_revision_id": run["creator_profile_revision_id"],
        }
        if name == "asr":
            audio = self.database.fetch_one(
                "SELECT relative_path, size_bytes, sha256, configuration_fingerprint "
                "FROM artifacts WHERE source_recording_id = ? AND kind = 'analysis_audio' "
                "AND removed_at IS NULL ORDER BY created_at DESC LIMIT 1",
                (run["source_recording_id"],),
            )
            payload["analysis_audio"] = dict(audio) if audio is not None else None
            payload["asr_profile"] = self.configuration.asr_profile
        elif name == "embeddings":
            payload["transcript"] = [
                tuple(row)
                for row in self.database.fetch_all(
                    "SELECT sequence_number, start_us, end_us, normalized_text, producer_fingerprint "
                    "FROM transcript_segments WHERE analysis_run_id = ? ORDER BY sequence_number",
                    (run_id,),
                )
            ]
            payload["embedding_profile"] = self.configuration.embedding_profile
        elif name == "discovery":
            evidence_run_id = self._producer_run_id(run_id, "asr")
            embedding_run_id = self._producer_run_id(run_id, "embeddings")
            payload["evidence"] = [
                tuple(row)
                for row in self.database.fetch_all(
                    "SELECT evidence_type, start_us, end_us, content_hash, producer_generation "
                    "FROM evidence_items WHERE analysis_run_id = ? AND evidence_type = 'transcript' "
                    "ORDER BY start_us, id",
                    (evidence_run_id,),
                )
            ]
            payload["embedding_generations"] = [
                tuple(row)
                for row in self.database.fetch_all(
                    "SELECT model_profile, input_fingerprint, configuration_fingerprint "
                    "FROM embedding_generations WHERE analysis_run_id = ? ORDER BY id",
                    (embedding_run_id,),
                )
            ]
            payload["retrieval_version"] = self.configuration.retrieval_version
        elif name == "envelopes":
            payload["candidates"] = [
                tuple(row)
                for row in self.database.fetch_all(
                    "SELECT generator_name, generator_version, anchor_us, start_us, end_us, "
                    "category_hint, idempotency_key FROM candidate_moments "
                    "WHERE analysis_run_id = ? ORDER BY anchor_us, id",
                    (run_id,),
                )
            ]
        elif name == "evaluation":
            payload["envelopes"] = [
                tuple(row)
                for row in self.database.fetch_all(
                    "SELECT id, start_us, end_us, package_fingerprint FROM context_envelopes "
                    "WHERE analysis_run_id = ? ORDER BY start_us, id",
                    (run_id,),
                )
            ]
            payload["evaluator_profile"] = self.configuration.evaluator_profile
        elif name == "ranking":
            payload["proposals"] = [
                tuple(row)
                for row in self.database.fetch_all(
                    "SELECT id, category, start_us, end_us, event_us, judgments_json "
                    "FROM clip_proposals WHERE analysis_run_id = ? ORDER BY id",
                    (run_id,),
                )
            ]
            if run["requested_more_from_run_id"] is not None:
                payload["parent_queue"] = [
                    tuple(row)
                    for row in self.database.fetch_all(
                        "SELECT e.rank, e.clip_proposal_id, e.baseline_score, e.diversity_json "
                        "FROM analysis_runs p JOIN queue_entries e ON e.queue_snapshot_id = p.queue_snapshot_id "
                        "WHERE p.id = ? ORDER BY e.rank",
                        (run["requested_more_from_run_id"],),
                    )
                ]
            boundary_target = self.database.fetch_one(
                "SELECT parent_queue_snapshot_id, superseded_proposal_id, boundary_edit_id "
                "FROM boundary_reanalysis_targets WHERE analysis_run_id = ?",
                (run_id,),
            )
            if boundary_target is not None:
                payload["boundary_reanalysis"] = dict(boundary_target)
                payload["parent_queue"] = [
                    tuple(row)
                    for row in self.database.fetch_all(
                        "SELECT rank, clip_proposal_id, baseline_score, diversity_json "
                        "FROM queue_entries WHERE queue_snapshot_id = ? ORDER BY rank",
                        (boundary_target["parent_queue_snapshot_id"],),
                    )
                ]
            payload["ranking_version"] = self.configuration.ranking_version
        else:
            raise ValueError(f"Unknown analysis stage: {name}")
        return fingerprint(payload)

    def _analysis_audio_path(self, source_id: str) -> Path:
        artifact = self.database.fetch_one(
            "SELECT * FROM artifacts WHERE source_recording_id = ? "
            "AND kind = 'analysis_audio' AND removed_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (source_id,),
        )
        if artifact is None:
            raise RuntimeError("Source Recording has no validated analysis audio")
        return ArtifactStore(self.database).require_intact(artifact)

    def _transcribe(self, run_id, source, cancellation_requested: Callable[[], bool]) -> None:
        transcription = self.asr.transcribe(
            self._analysis_audio_path(source["id"]),
            cancellation_requested=cancellation_requested,
            worker_started=self._worker_started,
        )
        worker_pid = (transcription.metadata or {}).get("worker_pid")
        self._report_stage_progress(
            0.70,
            worker_pid=int(worker_pid) if isinstance(worker_pid, int) else None,
        )
        segments = transcription.segments
        previous_end = 0
        producer_configuration = {
            "asr_profile": self.configuration.asr_profile,
            "source_sha256": source["sha256"],
        }
        if self.configuration.asr_language is not None:
            producer_configuration["asr_language"] = self.configuration.asr_language
        producer_fingerprint = fingerprint(producer_configuration)
        raw_directory = self.database.settings.work_dir / "artifacts" / "asr" / run_id
        raw_directory.mkdir(parents=True, exist_ok=True)
        raw_path = raw_directory / "raw-transcription.json"
        raw_partial = raw_path.with_name(f"{raw_path.name}.partial")
        raw_payload = {
            "schema_version": 1,
            "producer_fingerprint": producer_fingerprint,
            "metadata": transcription.metadata or {},
            "raw": transcription.raw or {},
        }
        raw_partial.write_text(
            json.dumps(raw_payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        raw_partial.replace(raw_path)
        raw_digest = sha256_file(raw_path)
        with self.database.transaction(immediate=True) as connection:
            segment_ids: list[str] = []
            for sequence, segment in enumerate(segments):
                if not 0 <= segment.start_us < segment.end_us <= int(source["source_end_us"]):
                    raise ValueError("ASR segment is outside Source Time")
                if segment.start_us < previous_end:
                    raise ValueError("ASR segments must be monotonic and non-overlapping")
                previous_end = segment.end_us
                segment_id = new_id("segment")
                segment_ids.append(segment_id)
                evidence_id = new_id("evidence")
                normalized = normalize_text(segment.text)
                connection.execute(
                    "INSERT INTO transcript_segments "
                    "(id, analysis_run_id, sequence_number, start_us, end_us, raw_text, "
                    "normalized_text, language, producer_fingerprint, avg_log_probability, "
                    "no_speech_probability) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        segment_id,
                        run_id,
                        sequence,
                        segment.start_us,
                        segment.end_us,
                        segment.text,
                        normalized,
                        segment.language,
                        producer_fingerprint,
                        segment.avg_log_probability,
                        segment.no_speech_probability,
                    ),
                )
                content_hash = sha256(normalized.encode("utf-8")).hexdigest()
                connection.execute(
                    "INSERT INTO evidence_items "
                    "(id, source_recording_id, analysis_run_id, producer_generation, "
                    "evidence_type, start_us, end_us, content, content_hash, locator_json) "
                    "VALUES (?, ?, ?, ?, 'transcript', ?, ?, ?, ?, ?)",
                    (
                        evidence_id,
                        source["id"],
                        run_id,
                        producer_fingerprint,
                        segment.start_us,
                        segment.end_us,
                        segment.text,
                        content_hash,
                        canonical_json({"transcript_segment_id": segment_id}),
                    ),
                )
            word_sequence_by_segment: dict[int, int] = {}
            previous_word_end_by_segment: dict[int, int] = {}
            for word in transcription.words:
                if not 0 <= word.segment_index < len(segment_ids):
                    raise ValueError("ASR word references an unknown segment")
                if not 0 <= word.start_us < word.end_us <= int(source["source_end_us"]):
                    raise ValueError("ASR word is outside Source Time")
                parent = segments[word.segment_index]
                if word.start_us < parent.start_us or word.end_us > parent.end_us:
                    raise ValueError("ASR word is outside its referenced transcript segment")
                if word.start_us < previous_word_end_by_segment.get(word.segment_index, parent.start_us):
                    raise ValueError("ASR words must be monotonic and non-overlapping within a segment")
                previous_word_end_by_segment[word.segment_index] = word.end_us
                sequence = word_sequence_by_segment.get(word.segment_index, 0)
                word_sequence_by_segment[word.segment_index] = sequence + 1
                connection.execute(
                    "INSERT INTO transcript_words "
                    "(id, transcript_segment_id, sequence_number, start_us, end_us, word, probability) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("word"),
                        segment_ids[word.segment_index],
                        sequence,
                        word.start_us,
                        word.end_us,
                        word.text,
                        word.probability,
                    ),
                )
            ArtifactStore(self.database).register(
                connection,
                path=raw_path,
                kind="asr_raw_output",
                owner_type="analysis",
                owner_id=run_id,
                source_recording_id=str(source["id"]),
                configuration={
                    "asr_profile": self.configuration.asr_profile,
                    **(
                        {"asr_language": self.configuration.asr_language}
                        if self.configuration.asr_language is not None
                        else {}
                    ),
                },
                require_hash=True,
                precomputed_sha256=raw_digest,
                precomputed_size=raw_path.stat().st_size,
                regenerable=True,
                integrity={"validated": True, "sha256": raw_digest},
            )
        self._report_stage_progress(0.95)

    def _embed(self, run_id, source, cancellation_requested: Callable[[], bool]) -> None:
        if self.embedding is None:
            return
        transcript_rows = [
            dict(row)
            for row in self.database.fetch_all(
                "SELECT id, start_us, end_us, content FROM evidence_items "
                "WHERE analysis_run_id = ? AND evidence_type = 'transcript' ORDER BY start_us, id",
                (run_id,),
            )
        ]
        windows = build_transcript_windows(transcript_rows)
        if not windows:
            return
        documents = tuple(EmbeddingItem(key=transcript_window_key(window), text=window.text) for window in windows)
        queries = tuple(
            EmbeddingItem(key=f"category:{category.value}", text=text)
            for category, text in CATEGORY_EMBEDDING_QUERIES.items()
        )
        generation_fingerprint = fingerprint(
            {
                "analysis_run_id": run_id,
                "embedding_profile": self.configuration.embedding_profile,
                "documents": [(item.key, fingerprint(item.text)) for item in documents],
                "queries": [(item.key, fingerprint(item.text)) for item in queries],
            }
        )
        output_directory = (
            self.database.settings.work_dir / "artifacts" / "embeddings" / run_id / generation_fingerprint
        )
        result = self.embedding.embed(
            documents,
            queries,
            output_directory,
            cancellation_requested=cancellation_requested,
            worker_started=self._worker_started,
        )
        worker_pid = result.metadata.get("worker_pid")
        self._report_stage_progress(
            0.75,
            worker_pid=int(worker_pid) if isinstance(worker_pid, int) else None,
        )
        vector_digest = sha256_file(result.vector_path)
        manifest_digest = sha256_file(result.manifest_path)
        configuration = {
            "embedding_profile": self.configuration.embedding_profile,
            "retrieval_version": self.configuration.retrieval_version,
        }
        configuration_fingerprint = fingerprint(configuration)
        artifacts = ArtifactStore(self.database)
        with self.database.transaction(immediate=True) as connection:
            vector_artifact_id = artifacts.register(
                connection,
                path=result.vector_path,
                kind="embedding_vectors",
                owner_type="analysis",
                owner_id=run_id,
                source_recording_id=str(source["id"]),
                configuration=configuration,
                require_hash=True,
                precomputed_sha256=vector_digest,
                precomputed_size=result.vector_path.stat().st_size,
                regenerable=True,
                integrity={
                    "validated": True,
                    "sha256": vector_digest,
                    "dimension": result.dimension,
                    "dtype": result.dtype,
                },
            )
            manifest_artifact_id = artifacts.register(
                connection,
                path=result.manifest_path,
                kind="embedding_manifest",
                owner_type="analysis",
                owner_id=run_id,
                source_recording_id=str(source["id"]),
                configuration=configuration,
                require_hash=True,
                precomputed_sha256=manifest_digest,
                precomputed_size=result.manifest_path.stat().st_size,
                regenerable=True,
                integrity={"validated": True, "sha256": manifest_digest},
            )
            connection.execute(
                "INSERT INTO embedding_generations "
                "(id, analysis_run_id, model_profile, input_fingerprint, configuration_fingerprint, "
                "vector_artifact_id, manifest_artifact_id, dimension, dtype, document_count, "
                "query_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("embedding"),
                    run_id,
                    self.configuration.embedding_profile,
                    generation_fingerprint,
                    configuration_fingerprint,
                    vector_artifact_id,
                    manifest_artifact_id,
                    result.dimension,
                    result.dtype,
                    len(result.document_keys),
                    len(result.query_keys),
                    utc_now(),
                ),
            )
        self._report_stage_progress(0.95)

    @staticmethod
    def _fair_cap(candidates: list[CandidateDraft], limit: int) -> list[CandidateDraft]:
        buckets: dict[tuple[str, str], list[CandidateDraft]] = {}
        for candidate in candidates:
            key = (
                candidate.generator_name,
                candidate.category_hint.value if candidate.category_hint else "uncategorized",
            )
            buckets.setdefault(key, []).append(candidate)
        for bucket in buckets.values():
            bucket.sort(key=lambda item: (-item.local_confidence, item.anchor_us, item.idempotency_key))
        selected: list[CandidateDraft] = []
        keys = sorted(buckets)
        while keys and len(selected) < limit:
            remaining: list[tuple[str, str]] = []
            for key in keys:
                if buckets[key] and len(selected) < limit:
                    selected.append(buckets[key].pop(0))
                if buckets[key]:
                    remaining.append(key)
            keys = remaining
        return selected

    def _discover_boundary_reanalysis(self, run_id: str, source: object) -> None:
        target = self.database.fetch_one(
            "SELECT t.*, p.event_us FROM boundary_reanalysis_targets t "
            "JOIN clip_proposals p ON p.id = t.superseded_proposal_id WHERE t.analysis_run_id = ?",
            (run_id,),
        )
        if target is None:
            raise RuntimeError("Boundary reanalysis target is missing")
        start_us = int(target["requested_start_us"])
        end_us = int(target["requested_end_us"])
        if end_us - start_us > 240_000_000:
            raise ValueError("Boundary reanalysis supports edited clips up to 240 seconds")
        if not 0 <= start_us < end_us <= int(source["source_end_us"]):
            raise ValueError("Boundary reanalysis target is outside Source Time")
        original_event_us = int(target["event_us"])
        anchor_us = original_event_us if start_us <= original_event_us < end_us else (start_us + end_us) // 2
        candidate_id = new_id("candidate")
        idempotency_key = fingerprint(
            {
                "boundary_edit_id": target["boundary_edit_id"],
                "start_us": start_us,
                "end_us": end_us,
                "version": 1,
            }
        )
        evidence = self.database.fetch_all(
            "SELECT evidence_item_id FROM proposal_evidence WHERE clip_proposal_id = ? "
            "ORDER BY evidence_item_id",
            (target["superseded_proposal_id"],),
        )
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO candidate_moments "
                "(id, analysis_run_id, generator_name, generator_version, anchor_us, start_us, "
                "end_us, local_confidence, category_hint, idempotency_key) "
                "SELECT ?, ?, 'editor-boundary-reanalysis', '1', ?, ?, ?, 1, category, ? "
                "FROM clip_proposals WHERE id = ?",
                (
                    candidate_id,
                    run_id,
                    anchor_us,
                    start_us,
                    end_us,
                    idempotency_key,
                    target["superseded_proposal_id"],
                ),
            )
            for row in evidence:
                connection.execute(
                    "INSERT INTO candidate_evidence(candidate_moment_id, evidence_item_id) VALUES (?, ?)",
                    (candidate_id, row["evidence_item_id"]),
                )

    def _discover(self, run_id, source, cancellation_requested: Callable[[], bool]) -> None:
        if cancellation_requested():
            raise AnalysisCancelled("Analysis cancellation was requested")
        if self.database.fetch_one(
            "SELECT 1 FROM boundary_reanalysis_targets WHERE analysis_run_id = ?",
            (run_id,),
        ) is not None:
            self._discover_boundary_reanalysis(run_id, source)
            self._report_stage_progress(0.95)
            return
        evidence_run_id = self._producer_run_id(run_id, "asr")
        embedding_run_id = self._producer_run_id(run_id, "embeddings")
        transcript_rows = [
            dict(row)
            for row in self.database.fetch_all(
                "SELECT id, start_us, end_us, content FROM evidence_items "
                "WHERE analysis_run_id = ? AND evidence_type = 'transcript' ORDER BY start_us",
                (evidence_run_id,),
            )
        ]
        windows = build_transcript_windows(transcript_rows)
        candidates = lexical_candidates(windows, int(source["source_end_us"]))
        candidates.extend(novelty_candidates(windows, int(source["source_end_us"])))
        embedding_generation = self.database.fetch_one(
            "SELECT g.* FROM embedding_generations g WHERE g.analysis_run_id = ? "
            "ORDER BY g.created_at DESC LIMIT 1",
            (embedding_run_id,),
        )
        if embedding_generation is not None:
            try:
                import numpy as np
            except ImportError as exc:
                raise RuntimeError("NumPy is required to read the configured embedding artifact") from exc
            vector_artifact = self.database.fetch_one(
                "SELECT * FROM artifacts WHERE id = ? AND removed_at IS NULL",
                (embedding_generation["vector_artifact_id"],),
            )
            manifest_artifact = self.database.fetch_one(
                "SELECT * FROM artifacts WHERE id = ? AND removed_at IS NULL",
                (embedding_generation["manifest_artifact_id"],),
            )
            if vector_artifact is None or manifest_artifact is None:
                raise RuntimeError("Registered embedding artifacts are missing")
            artifacts = ArtifactStore(self.database)
            vector_path = artifacts.require_intact(vector_artifact)
            manifest_path = artifacts.require_intact(manifest_artifact)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            vectors = np.load(vector_path, allow_pickle=False, mmap_mode="r")
            document_count = int(embedding_generation["document_count"])
            query_count = int(embedding_generation["query_count"])
            if vectors.shape != (document_count + query_count, int(embedding_generation["dimension"])):
                raise RuntimeError("Registered embedding artifact has an unexpected shape")
            document_vectors = vectors[:document_count]
            query_vectors = vectors[document_count:]
            similarities = query_vectors @ document_vectors.T
            candidates.extend(
                embedding_candidates(
                    windows,
                    tuple(manifest["document_keys"]),
                    tuple(manifest["query_keys"]),
                    similarities,
                    int(source["source_end_us"]),
                )
            )
        pending_audio_evidence: list[dict[str, object]] = []

        def evidence_factory(start_us, end_us, rms, median, mad, delta, delta_median, delta_mad) -> str:
            evidence_id = new_id("evidence")
            pending_audio_evidence.append(
                {
                    "id": evidence_id,
                    "start_us": start_us,
                    "end_us": end_us,
                    "rms": rms,
                    "median": median,
                    "mad": mad,
                    "energy_change": delta,
                    "energy_change_median": delta_median,
                    "energy_change_mad": delta_mad,
                }
            )
            return evidence_id

        audio_candidates, observations = audio_peak_candidates(
            self._analysis_audio_path(source["id"]),
            int(source["source_end_us"]),
            evidence_factory,
        )
        if cancellation_requested():
            raise AnalysisCancelled("Analysis cancellation was requested")
        candidates.extend(audio_candidates)
        speech_segments = [
            dict(row)
            for row in self.database.fetch_all(
                "SELECT id, start_us, end_us FROM transcript_segments "
                "WHERE analysis_run_id = ? ORDER BY start_us, id",
                (evidence_run_id,),
            )
        ]
        speech_words = [
            dict(row)
            for row in self.database.fetch_all(
                "SELECT w.id, w.start_us, w.end_us FROM transcript_words w "
                "JOIN transcript_segments s ON s.id = w.transcript_segment_id "
                "WHERE s.analysis_run_id = ? ORDER BY w.start_us, w.id",
                (evidence_run_id,),
            )
        ]
        pending_speech_evidence: list[dict[str, object]] = []

        def speech_evidence_factory(item: dict[str, object]) -> str:
            evidence_id = new_id("evidence")
            pending_speech_evidence.append({**item, "id": evidence_id})
            return evidence_id

        speech_candidates, speech_observations = speech_activity_candidates(
            speech_segments,
            speech_words,
            int(source["source_end_us"]),
            speech_evidence_factory,
        )
        candidates.extend(speech_candidates)
        self._report_stage_progress(0.60)
        candidate_rate = 30 if self.configuration.budget_tier == "default" else 50
        hard_cap = max(
            10,
            math.ceil(candidate_rate * max(int(source["source_end_us"]) / 3_600_000_000, 1 / 60)),
        )
        candidates = self._fair_cap(candidates, hard_cap)
        run = self.database.fetch_one(
            "SELECT requested_more_from_run_id FROM analysis_runs WHERE id = ?",
            (run_id,),
        )
        if run is not None and run["requested_more_from_run_id"] is not None:
            parent_keys = {
                (str(row["generator_name"]), str(row["generator_version"]), str(row["idempotency_key"]))
                for row in self.database.fetch_all(
                    "SELECT generator_name, generator_version, idempotency_key FROM candidate_moments "
                    "WHERE analysis_run_id = ?",
                    (run["requested_more_from_run_id"],),
                )
            }
            candidates = [
                candidate
                for candidate in candidates
                if (candidate.generator_name, candidate.generator_version, candidate.idempotency_key)
                not in parent_keys
            ]
        retained_evidence = {identifier for candidate in candidates for identifier in candidate.evidence_ids}
        audio_observations = {str(value["evidence_id"]): value for value in observations}
        speech_observations_by_id = {str(value["evidence_id"]): value for value in speech_observations}
        with self.database.transaction(immediate=True) as connection:
            for item in pending_audio_evidence:
                if item["id"] not in retained_evidence:
                    continue
                content = canonical_json(
                    {
                        "rms": item["rms"],
                        "rolling_median": item["median"],
                        "rolling_mad": item["mad"],
                        "energy_change": item["energy_change"],
                        "rolling_energy_change_median": item["energy_change_median"],
                        "rolling_energy_change_mad": item["energy_change_mad"],
                    }
                )
                connection.execute(
                    "INSERT INTO evidence_items "
                    "(id, source_recording_id, analysis_run_id, producer_generation, evidence_type, "
                    "start_us, end_us, content, content_hash, locator_json) "
                    "VALUES (?, ?, ?, 'audio-energy-v2', 'audio_energy', ?, ?, ?, ?, '{}')",
                    (
                        item["id"],
                        source["id"],
                        run_id,
                        item["start_us"],
                        item["end_us"],
                        content,
                        sha256(content.encode("utf-8")).hexdigest(),
                    ),
                )
                observation = audio_observations[str(item["id"])]
                connection.execute(
                    "INSERT INTO observations "
                    "(id, evidence_item_id, observation_type, numeric_value, metadata_json) "
                    "VALUES (?, ?, 'rms_energy_peak', ?, ?)",
                    (
                        new_id("observation"),
                        item["id"],
                        observation["local_z"],
                        canonical_json(
                            {
                                "rms": item["rms"],
                                "median": item["median"],
                                "mad": item["mad"],
                                "rms_z": observation["rms_z"],
                                "energy_change": item["energy_change"],
                                "energy_change_z": observation["energy_change_z"],
                                "normalization_window_seconds": 300,
                            }
                        ),
                    ),
                )
            for item in pending_speech_evidence:
                if item["id"] not in retained_evidence:
                    continue
                content = canonical_json(
                    {
                        "speech_ratio": item["speech_ratio"],
                        "word_count": item["word_count"],
                        "speech_rate_words_per_second": item["speech_rate_words_per_second"],
                        "pause_before_seconds": item["pause_before_seconds"],
                        "segmentation": (
                            "faster-whisper-silero-vad"
                            if self.configuration.asr_profile.startswith("whisper-")
                            else "asr-segment-timing"
                        ),
                    }
                )
                connection.execute(
                    "INSERT INTO evidence_items "
                    "(id, source_recording_id, analysis_run_id, producer_generation, evidence_type, "
                    "start_us, end_us, content, content_hash, locator_json) "
                    "VALUES (?, ?, ?, 'speech-activity-v1', 'speech_activity', ?, ?, ?, ?, '{}')",
                    (
                        item["id"],
                        source["id"],
                        run_id,
                        item["start_us"],
                        item["end_us"],
                        content,
                        sha256(content.encode("utf-8")).hexdigest(),
                    ),
                )
                observation = speech_observations_by_id[str(item["id"])]
                connection.execute(
                    "INSERT INTO observations "
                    "(id, evidence_item_id, observation_type, numeric_value, metadata_json) "
                    "VALUES (?, ?, 'speech_activity_change', ?, ?)",
                    (
                        new_id("observation"),
                        item["id"],
                        observation["local_score"],
                        canonical_json(
                            {
                                "speech_rate_local_z": observation["speech_rate_local_z"],
                                "speech_activity_change": observation["speech_activity_change"],
                                "speech_activity_change_local_z": observation[
                                    "speech_activity_change_local_z"
                                ],
                                "normalization_window_seconds": 300,
                                "bin_seconds": 5,
                            }
                        ),
                    ),
                )
            for candidate in candidates:
                candidate_id = new_id("candidate")
                connection.execute(
                    "INSERT INTO candidate_moments "
                    "(id, analysis_run_id, generator_name, generator_version, anchor_us, start_us, "
                    "end_us, local_confidence, category_hint, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        run_id,
                        candidate.generator_name,
                        candidate.generator_version,
                        candidate.anchor_us,
                        candidate.start_us,
                        candidate.end_us,
                        candidate.local_confidence,
                        candidate.category_hint.value if candidate.category_hint else None,
                        candidate.idempotency_key,
                    ),
                )
                for evidence_id in candidate.evidence_ids:
                    connection.execute(
                        "INSERT INTO candidate_evidence(candidate_moment_id, evidence_item_id) VALUES (?, ?)",
                        (candidate_id, evidence_id),
                    )
        self._report_stage_progress(0.95)

    def _create_envelopes(self, run_id, source, cancellation_requested: Callable[[], bool]) -> None:
        transcript_run_id = self._producer_run_id(run_id, "asr")
        candidates = self.database.fetch_all(
            "SELECT * FROM candidate_moments WHERE analysis_run_id = ? ORDER BY anchor_us, id",
            (run_id,),
        )
        clusters: list[list] = []
        for candidate in candidates:
            if not clusters or int(candidate["anchor_us"]) - int(clusters[-1][0]["anchor_us"]) > 60_000_000:
                clusters.append([candidate])
            else:
                clusters[-1].append(candidate)
        source_end_us = int(source["source_end_us"])
        source_hours = source_end_us / 3_600_000_000
        envelope_rate = 6 if self.configuration.budget_tier == "default" else 10
        target_envelope_count = min(100, max(1, math.ceil(envelope_rate * source_hours)))
        soft_coverage_limit = min(source_end_us, max(15 * 60 * 1_000_000, int(source_end_us * 0.10)))
        hard_coverage_limit = min(source_end_us, max(30 * 60 * 1_000_000, int(source_end_us * 0.20)))
        selected_intervals: list[tuple[int, int]] = []
        selected_sections: set[int] = set()
        selected_categories: set[str] = set()
        selected_generators: set[str] = set()
        saturated = False
        ordered_clusters = _balanced_cluster_order(clusters)
        with self.database.transaction(immediate=True) as connection:
            for members in ordered_clusters:
                if cancellation_requested():
                    raise AnalysisCancelled("Analysis cancellation was requested")
                if len(selected_intervals) >= target_envelope_count:
                    saturated = len(ordered_clusters) > len(selected_intervals)
                    break
                cluster_id = new_id("cluster")
                envelope_id = new_id("envelope")
                member_ids = tuple(str(member["id"]) for member in members)
                candidate_start = min(
                    int(member["start_us"] if member["start_us"] is not None else member["anchor_us"])
                    for member in members
                )
                candidate_end = max(
                    int(member["end_us"] if member["end_us"] is not None else member["anchor_us"] + 1)
                    for member in members
                )
                envelope_start, envelope_end = _context_envelope_bounds(
                    candidate_start,
                    candidate_end,
                    source_end_us,
                )
                if envelope_end <= envelope_start:
                    continue
                proposed_intervals = [*selected_intervals, (envelope_start, envelope_end)]
                proposed_coverage = _union_duration_us(proposed_intervals)
                _, section, categories, generators, _ = _cluster_traits(members)
                adds_coverage = (
                    section not in selected_sections
                    or bool(categories - selected_categories)
                    or bool(generators - selected_generators)
                )
                if proposed_coverage > hard_coverage_limit or (
                    self.configuration.budget_tier == "default"
                    and selected_intervals
                    and proposed_coverage > soft_coverage_limit
                    and not adds_coverage
                ):
                    saturated = True
                    continue
                selected_intervals.append((envelope_start, envelope_end))
                selected_sections.add(section)
                selected_categories.update(categories)
                selected_generators.update(generators)
                connection.execute(
                    "INSERT INTO candidate_clusters "
                    "(id, analysis_run_id, start_us, end_us, clustering_version, idempotency_key) "
                    "VALUES (?, ?, ?, ?, 'temporal-60s-section-balanced-v2', ?)",
                    (
                        cluster_id,
                        run_id,
                        candidate_start,
                        candidate_end,
                        fingerprint({"members": member_ids}),
                    ),
                )
                for member_id in member_ids:
                    connection.execute(
                        "INSERT INTO cluster_members(candidate_cluster_id, candidate_moment_id) VALUES (?, ?)",
                        (cluster_id, member_id),
                    )
                evidence = connection.execute(
                    "SELECT id, start_us, end_us FROM evidence_items "
                    "WHERE analysis_run_id IN (?, ?) AND start_us < ? AND end_us > ? "
                    "ORDER BY start_us, id",
                    (run_id, transcript_run_id, envelope_end, envelope_start),
                ).fetchall()
                package = {
                    "members": member_ids,
                    "evidence": [row["id"] for row in evidence],
                    "start_us": envelope_start,
                    "end_us": envelope_end,
                }
                connection.execute(
                    "INSERT INTO context_envelopes "
                    "(id, candidate_cluster_id, analysis_run_id, start_us, end_us, package_fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        envelope_id,
                        cluster_id,
                        run_id,
                        envelope_start,
                        envelope_end,
                        fingerprint(package),
                    ),
                )
                anchor_values: set[tuple[int, str, str | None]] = {
                    (envelope_start, "envelope_start", None),
                    (envelope_end, "envelope_end", None),
                }
                for member in members:
                    anchor_values.add((int(member["anchor_us"]), "candidate_anchor", None))
                    if member["start_us"] is not None:
                        anchor_values.add((int(member["start_us"]), "candidate_start", None))
                    if member["end_us"] is not None:
                        anchor_values.add((int(member["end_us"]), "candidate_end", None))
                for item in evidence:
                    anchor_values.add((int(item["start_us"]), "evidence_start", str(item["id"])))
                    anchor_values.add((int(item["end_us"]), "evidence_end", str(item["id"])))
                words = connection.execute(
                    "SELECT w.start_us, w.end_us FROM transcript_words w "
                    "JOIN transcript_segments s ON s.id = w.transcript_segment_id "
                    "WHERE s.analysis_run_id = ? AND w.start_us < ? AND w.end_us > ?",
                    (transcript_run_id, envelope_end, envelope_start),
                ).fetchall()
                for word in words:
                    anchor_values.add((int(word["start_us"]), "word_start", None))
                    anchor_values.add((int(word["end_us"]), "word_end", None))
                transcript_spans = connection.execute(
                    "SELECT start_us, end_us FROM transcript_segments WHERE analysis_run_id = ? "
                    "AND start_us < ? AND end_us > ? ORDER BY start_us",
                    (transcript_run_id, envelope_end, envelope_start),
                ).fetchall()
                for previous, following in zip(transcript_spans, transcript_spans[1:], strict=False):
                    gap_start = int(previous["end_us"])
                    gap_end = int(following["start_us"])
                    if gap_end - gap_start >= 300_000:
                        anchor_values.add(((gap_start + gap_end) // 2, "speech_pause", None))
                for point, anchor_type, evidence_id in sorted(anchor_values):
                    if not envelope_start <= point <= envelope_end:
                        continue
                    connection.execute(
                        "INSERT INTO boundary_anchors "
                        "(id, context_envelope_id, source_time_us, anchor_type, evidence_item_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (new_id("anchor"), envelope_id, point, anchor_type, evidence_id),
                    )
            saturated = saturated or len(selected_intervals) < len(ordered_clusters)
            if saturated:
                connection.execute("UPDATE analysis_runs SET coverage_saturated = 1 WHERE id = ?", (run_id,))

    def _evaluate(self, run_id, source, cancellation_requested: Callable[[], bool]) -> None:
        transcript_run_id = self._producer_run_id(run_id, "asr")
        boundary_target = self.database.fetch_one(
            "SELECT superseded_proposal_id, requested_start_us, requested_end_us "
            "FROM boundary_reanalysis_targets WHERE analysis_run_id = ?",
            (run_id,),
        )
        envelopes = self.database.fetch_all(
            "SELECT * FROM context_envelopes WHERE analysis_run_id = ? AND disposition IS NULL ORDER BY start_us, id",
            (run_id,),
        )
        profile = self.database.fetch_one(
            "SELECT p.* FROM creator_profile_revisions p "
            "JOIN analysis_runs r ON r.creator_profile_revision_id = p.id WHERE r.id = ?",
            (run_id,),
        )
        source_hours = max(1.0, int(source["source_end_us"]) / 3_600_000_000)
        source_prompt_cap = int(self.configuration.max_prompt_tokens_per_source_hour * source_hours)
        try:
            for envelope_index, envelope in enumerate(envelopes):
                if cancellation_requested():
                    raise AnalysisCancelled("Analysis cancellation was requested")
                candidate_rows = [
                    dict(row)
                    for row in self.database.fetch_all(
                        "SELECT c.id, c.generator_name, c.generator_version, c.anchor_us, "
                        "c.start_us, c.end_us, c.local_confidence, c.category_hint "
                        "FROM candidate_moments c "
                        "JOIN cluster_members cm ON cm.candidate_moment_id = c.id "
                        "WHERE cm.candidate_cluster_id = ? ORDER BY c.anchor_us, c.id",
                        (envelope["candidate_cluster_id"],),
                    )
                ]
                evidence_rows = [
                    dict(row)
                    for row in self.database.fetch_all(
                        "SELECT id, evidence_type, start_us, end_us, content FROM evidence_items "
                        "WHERE analysis_run_id IN (?, ?) "
                        "AND start_us < ? AND end_us > ? ORDER BY start_us, id",
                        (run_id, transcript_run_id, envelope["end_us"], envelope["start_us"]),
                    )
                ]
                anchor_rows = [
                    dict(row)
                    for row in self.database.fetch_all(
                        "SELECT id, source_time_us, anchor_type, evidence_item_id FROM boundary_anchors "
                        "WHERE context_envelope_id = ? ORDER BY source_time_us, id",
                        (envelope["id"],),
                    )
                ]
                candidate_id_map = {
                    f"c{index:03d}": str(candidate["id"]) for index, candidate in enumerate(candidate_rows, start=1)
                }
                evidence_id_map = {
                    f"e{index:03d}": str(item["id"]) for index, item in enumerate(evidence_rows, start=1)
                }
                anchor_id_map = {
                    f"a{index:04d}": str(anchor["id"]) for index, anchor in enumerate(anchor_rows, start=1)
                }
                candidate_alias = {value: key for key, value in candidate_id_map.items()}
                evidence_alias = {value: key for key, value in evidence_id_map.items()}
                anchor_alias = {value: key for key, value in anchor_id_map.items()}
                candidates = [
                    {**candidate, "id": candidate_alias[str(candidate["id"])]} for candidate in candidate_rows
                ]
                evidence = [{**item, "id": evidence_alias[str(item["id"])]} for item in evidence_rows]
                anchors = [
                    {
                        **anchor,
                        "id": anchor_alias[str(anchor["id"])],
                        "evidence_item_id": (
                            evidence_alias.get(str(anchor["evidence_item_id"]))
                            if anchor["evidence_item_id"] is not None
                            else None
                        ),
                    }
                    for anchor in anchor_rows
                ]
                run_tokens = int(
                    self.database.fetch_one("SELECT prompt_tokens FROM analysis_runs WHERE id = ?", (run_id,))[
                        "prompt_tokens"
                    ]
                )
                remaining_tokens = min(
                    source_prompt_cap - run_tokens,
                    self.configuration.max_prompt_tokens_per_run - run_tokens,
                )
                compact_profile = {
                    "revision_id": profile["id"] if profile else None,
                    "languages": json.loads(profile["languages_json"]) if profile else [],
                    "category_priorities": json.loads(profile["category_priorities_json"]) if profile else {},
                    "desired_content": profile["desired_content"] if profile else "",
                    "avoided_content": profile["avoided_content"] if profile else "",
                    "preferred_durations": json.loads(profile["preferred_durations_json"]) if profile else {},
                }
                package = {
                    "envelope_id": envelope["id"],
                    "start_us": envelope["start_us"],
                    "end_us": envelope["end_us"],
                    "source_end_us": source["source_end_us"],
                    "creator_profile": compact_profile,
                    "candidates": candidates,
                    "evidence": evidence,
                    "anchors": anchors,
                    "remaining_prompt_tokens": max(0, remaining_tokens),
                    "intent": (
                        {
                            "kind": "boundary_reanalysis",
                            "superseded_proposal_id": boundary_target["superseded_proposal_id"],
                            "requested_start_us": boundary_target["requested_start_us"],
                            "requested_end_us": boundary_target["requested_end_us"],
                        }
                        if boundary_target is not None
                        else {"kind": "standard_discovery"}
                    ),
                    "_id_maps": {
                        "candidate": candidate_id_map,
                        "evidence": evidence_id_map,
                        "anchor": anchor_id_map,
                    },
                }
                attempt_id = new_id("evaluation")
                with self.database.transaction(immediate=True) as connection:
                    attempt_number = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next "
                            "FROM evaluation_attempts WHERE context_envelope_id = ? AND model_profile = ?",
                            (envelope["id"], self.configuration.evaluator_profile),
                        ).fetchone()["next"]
                    )
                    connection.execute(
                        "INSERT INTO evaluation_attempts "
                        "(id, context_envelope_id, model_profile, state, attempt_number, started_at) "
                        "VALUES (?, ?, ?, 'running', ?, ?)",
                        (
                            attempt_id,
                            envelope["id"],
                            self.configuration.evaluator_profile,
                            attempt_number,
                            utc_now(),
                        ),
                    )
                outcome: EvaluationOutcome | None = None
                try:
                    if remaining_tokens <= 0:
                        outcome = EvaluationOutcome(
                            disposition="input_too_large",
                            candidate_outcomes=tuple(
                                CandidateEvaluationOutcome(
                                    str(candidate["id"]),
                                    "omitted_by_prompt_budget",
                                    reason="Analysis Run prompt-token budget is exhausted",
                                )
                                for candidate in candidates
                            ),
                        )
                    else:
                        try:
                            outcome = self.evaluator.evaluate(
                                package,
                                cancellation_requested=cancellation_requested,
                                worker_started=self._worker_started,
                            )
                        except EvaluatorExecutionError as exc:
                            outcome = exc.outcome
                            raise
                    outcome = self._restore_database_ids(outcome, package["_id_maps"])
                    if len(outcome.proposals) > 3:
                        raise ValueError("Evaluator returned more than three proposals")
                    self._persist_evaluation(
                        attempt_id,
                        envelope,
                        source,
                        candidate_rows,
                        evidence_rows,
                        outcome,
                    )
                    self._report_stage_progress(
                        0.05 + 0.90 * (envelope_index + 1) / max(1, len(envelopes))
                    )
                except BaseException as exc:
                    self._persist_failed_evaluation(attempt_id, envelope, source, outcome, exc)
                    raise
        finally:
            self.evaluator.close()

    @staticmethod
    def _restore_database_ids(outcome: EvaluationOutcome, id_maps: dict[str, dict[str, str]]) -> EvaluationOutcome:
        candidate_ids = id_maps["candidate"]
        evidence_ids = id_maps["evidence"]
        proposals = tuple(
            replace(
                proposal,
                candidate_ids=tuple(candidate_ids.get(identifier, identifier) for identifier in proposal.candidate_ids),
                evidence_ids=tuple(evidence_ids.get(identifier, identifier) for identifier in proposal.evidence_ids),
            )
            for proposal in outcome.proposals
        )
        candidate_outcomes = tuple(
            replace(item, candidate_id=candidate_ids.get(item.candidate_id, item.candidate_id))
            for item in outcome.candidate_outcomes
        )
        return replace(outcome, proposals=proposals, candidate_outcomes=candidate_outcomes)

    def _persist_evaluation(
        self, attempt_id, envelope, source, candidates, evidence, outcome: EvaluationOutcome
    ) -> None:
        allowed_candidates = {str(item["id"]) for item in candidates}
        allowed_evidence = {str(item["id"]) for item in evidence}
        allowed_anchor_times = {
            int(row["source_time_us"])
            for row in self.database.fetch_all(
                "SELECT source_time_us FROM boundary_anchors WHERE context_envelope_id = ?",
                (envelope["id"],),
            )
        }
        drafts = outcome.proposals
        allowed_dispositions = {
            "proposal_set",
            "semantic_rejection",
            "insufficient_context",
            "input_too_large",
            "invalid_for_profile",
        }
        if outcome.disposition not in allowed_dispositions:
            raise ValueError("Evaluator returned an unknown disposition")
        if (outcome.disposition == "proposal_set") != bool(drafts):
            raise ValueError("Evaluator disposition and proposal set disagree")
        raw_path: Path | None = None
        raw_digest: str | None = None
        if outcome.raw_response is not None:
            raw_directory = (
                self.database.settings.work_dir / "artifacts" / "evaluator" / str(envelope["analysis_run_id"])
            )
            raw_directory.mkdir(parents=True, exist_ok=True)
            raw_path = raw_directory / f"{attempt_id}.json"
            partial = raw_path.with_name(f"{raw_path.name}.partial")
            partial.write_text(outcome.raw_response, encoding="utf-8")
            partial.replace(raw_path)
            raw_digest = sha256_file(raw_path)
        covered: dict[str, str] = {}
        proposal_ids: list[str] = []
        boundary_target = self.database.fetch_one(
            "SELECT superseded_proposal_id FROM boundary_reanalysis_targets WHERE analysis_run_id = ?",
            (envelope["analysis_run_id"],),
        )
        superseded_proposal_id = (
            str(boundary_target["superseded_proposal_id"]) if boundary_target is not None else None
        )
        with self.database.transaction(immediate=True) as connection:
            for draft in drafts:
                draft.validate(int(source["source_end_us"]))
                if draft.interval.start_us < int(envelope["start_us"]) or draft.interval.end_us > int(
                    envelope["end_us"]
                ):
                    raise ValueError("Evaluator proposal is outside its Context Envelope")
                selected_times = {
                    draft.interval.start_us,
                    draft.interval.end_us,
                    draft.structure.event_us,
                    *(
                        point
                        for point in (
                            draft.structure.setup_start_us,
                            draft.structure.hook_us,
                            draft.structure.payoff_us,
                            draft.structure.exit_us,
                        )
                        if point is not None
                    ),
                }
                if not selected_times <= allowed_anchor_times:
                    raise ValueError("Evaluator selected a time that is not a Boundary Anchor")
                if not set(draft.candidate_ids) <= allowed_candidates:
                    raise ValueError("Evaluator referenced an unknown Candidate Moment")
                if not set(draft.evidence_ids) <= allowed_evidence:
                    raise ValueError("Evaluator referenced an unknown Evidence Item")
                if len(set(draft.candidate_ids)) != len(draft.candidate_ids):
                    raise ValueError("Evaluator duplicated a Candidate Moment within one proposal")
                if len(set(draft.evidence_ids)) != len(draft.evidence_ids):
                    raise ValueError("Evaluator duplicated an Evidence Item within one proposal")
                if any(candidate_id in covered for candidate_id in draft.candidate_ids):
                    raise ValueError("One Candidate Moment cannot contribute to multiple proposals")
                proposal_id = new_id("proposal")
                proposal_ids.append(proposal_id)
                structure = draft.structure
                connection.execute(
                    "INSERT INTO clip_proposals "
                    "(id, analysis_run_id, context_envelope_id, evaluation_attempt_id, category, "
                    "summary, start_us, end_us, event_us, setup_start_us, hook_us, payoff_us, "
                    "exit_us, judgments_json, reasons_against_selection_json, "
                    "duration_exception_reason, supersedes_proposal_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        proposal_id,
                        envelope["analysis_run_id"],
                        envelope["id"],
                        attempt_id,
                        draft.category.value,
                        draft.summary,
                        draft.interval.start_us,
                        draft.interval.end_us,
                        structure.event_us,
                        structure.setup_start_us,
                        structure.hook_us,
                        structure.payoff_us,
                        structure.exit_us,
                        canonical_json(draft.judgments.as_dict()),
                        canonical_json(list(draft.reasons_against_selection)),
                        draft.duration_exception_reason,
                        superseded_proposal_id,
                        utc_now(),
                    ),
                )
                for evidence_id in draft.evidence_ids:
                    connection.execute(
                        "INSERT INTO proposal_evidence(clip_proposal_id, evidence_item_id) VALUES (?, ?)",
                        (proposal_id, evidence_id),
                    )
                for candidate_id in draft.candidate_ids:
                    connection.execute(
                        "INSERT INTO proposal_candidates(clip_proposal_id, candidate_moment_id) VALUES (?, ?)",
                        (proposal_id, candidate_id),
                    )
                    covered[candidate_id] = proposal_id
                for risk in draft.risks:
                    connection.execute(
                        "INSERT INTO proposal_risks(clip_proposal_id, risk_kind, reason) VALUES (?, ?, ?)",
                        (proposal_id, risk.kind.value, risk.reason),
                    )
            supplied_outcomes = {item.candidate_id: item for item in outcome.candidate_outcomes}
            if (
                len(outcome.candidate_outcomes) != len(allowed_candidates)
                or set(supplied_outcomes) != allowed_candidates
            ):
                raise ValueError("Evaluator must return exactly one outcome for every Candidate Moment")
            for candidate_id in sorted(allowed_candidates):
                candidate_outcome = supplied_outcomes[candidate_id]
                if candidate_outcome.outcome not in CANDIDATE_OUTCOMES:
                    raise ValueError("Evaluator returned an unknown Candidate Evaluation Outcome")
                proposal_id = None
                if candidate_outcome.proposal_index is not None:
                    if not 0 <= candidate_outcome.proposal_index < len(proposal_ids):
                        raise ValueError("Candidate outcome references an unknown proposal index")
                    proposal_id = proposal_ids[candidate_outcome.proposal_index]
                linked_proposal = covered.get(candidate_id)
                if candidate_outcome.outcome == "covered_by_proposal":
                    if linked_proposal != proposal_id:
                        raise ValueError("Covered candidate outcome conflicts with proposal provenance")
                elif candidate_outcome.outcome == "duplicate_of_proposal":
                    if linked_proposal is not None or proposal_id is None:
                        raise ValueError("Duplicate candidate outcome conflicts with proposal provenance")
                elif linked_proposal is not None or proposal_id is not None:
                    raise ValueError("Unselected candidate outcome conflicts with proposal provenance")
                connection.execute(
                    "INSERT INTO candidate_outcomes "
                    "(evaluation_attempt_id, candidate_moment_id, outcome, clip_proposal_id, reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        attempt_id,
                        candidate_id,
                        candidate_outcome.outcome,
                        proposal_id,
                        candidate_outcome.reason,
                    ),
                )
            metadata = outcome.metadata or {}
            prompt_tokens = int(metadata.get("prompt_tokens", 0))
            reasoning_tokens = int(metadata.get("reasoning_tokens", 0))
            final_tokens = int(metadata.get("final_tokens", 0))
            if raw_path is not None and raw_digest is not None:
                ArtifactStore(self.database).register(
                    connection,
                    path=raw_path,
                    kind="evaluator_raw_response",
                    owner_type="evaluation_attempt",
                    owner_id=attempt_id,
                    source_recording_id=str(source["id"]),
                    configuration={"model_profile": self.configuration.evaluator_profile},
                    require_hash=True,
                    precomputed_sha256=raw_digest,
                    precomputed_size=raw_path.stat().st_size,
                    regenerable=True,
                    integrity={"validated": True, "sha256": raw_digest},
                )
            connection.execute(
                "UPDATE evaluation_attempts SET state = 'succeeded', disposition = ?, prompt_hash = ?, "
                "prompt_tokens = ?, reasoning_tokens = ?, final_tokens = ?, raw_response_relpath = ?, "
                "validation_errors_json = ?, runtime_metadata_json = ?, ended_at = ? WHERE id = ?",
                (
                    outcome.disposition,
                    metadata.get("prompt_hash"),
                    prompt_tokens,
                    reasoning_tokens,
                    final_tokens,
                    self.database.settings.relative_to_workdir(raw_path) if raw_path else None,
                    canonical_json(list(outcome.validation_errors)),
                    canonical_json(metadata),
                    utc_now(),
                    attempt_id,
                ),
            )
            connection.execute(
                "UPDATE context_envelopes SET disposition = ? WHERE id = ?",
                (outcome.disposition, envelope["id"]),
            )
            source_hours = max(1.0, int(source["source_end_us"]) / 3_600_000_000)
            token_limit = min(
                int(self.configuration.max_prompt_tokens_per_source_hour * source_hours),
                self.configuration.max_prompt_tokens_per_run,
            )
            connection.execute(
                "UPDATE analysis_runs SET prompt_tokens = prompt_tokens + ?, "
                "token_saturated = CASE WHEN prompt_tokens + ? >= ? THEN 1 ELSE token_saturated END "
                "WHERE id = ?",
                (
                    prompt_tokens,
                    prompt_tokens,
                    token_limit,
                    envelope["analysis_run_id"],
                ),
            )

    def _persist_failed_evaluation(self, attempt_id, envelope, source, outcome, error: BaseException) -> None:
        raw_path: Path | None = None
        raw_digest: str | None = None
        if outcome is not None and outcome.raw_response is not None:
            raw_directory = (
                self.database.settings.work_dir / "artifacts" / "evaluator" / str(envelope["analysis_run_id"])
            )
            raw_directory.mkdir(parents=True, exist_ok=True)
            raw_path = raw_directory / f"{attempt_id}.json"
            partial = raw_path.with_name(f"{raw_path.name}.partial")
            partial.write_text(outcome.raw_response, encoding="utf-8")
            partial.replace(raw_path)
            raw_digest = sha256_file(raw_path)
        metadata = outcome.metadata if outcome is not None and outcome.metadata is not None else {}
        validation_errors = list(outcome.validation_errors if outcome is not None else ())
        validation_errors.append(str(error)[:4000])
        target_state = "cancelled" if isinstance(error, (AnalysisCancelled, WorkerCancelled)) else "failed"
        prompt_tokens = int(metadata.get("prompt_tokens", 0))
        source_hours = max(1.0, int(source["source_end_us"]) / 3_600_000_000)
        token_limit = min(
            int(self.configuration.max_prompt_tokens_per_source_hour * source_hours),
            self.configuration.max_prompt_tokens_per_run,
        )
        with self.database.transaction(immediate=True) as connection:
            if raw_path is not None and raw_digest is not None:
                ArtifactStore(self.database).register(
                    connection,
                    path=raw_path,
                    kind="evaluator_raw_response",
                    owner_type="evaluation_attempt",
                    owner_id=attempt_id,
                    source_recording_id=str(source["id"]),
                    configuration={"model_profile": self.configuration.evaluator_profile},
                    require_hash=True,
                    precomputed_sha256=raw_digest,
                    precomputed_size=raw_path.stat().st_size,
                    regenerable=True,
                    integrity={"validated": False, "sha256": raw_digest},
                )
            connection.execute(
                "UPDATE evaluation_attempts SET state = ?, prompt_hash = ?, prompt_tokens = ?, "
                "reasoning_tokens = ?, final_tokens = ?, raw_response_relpath = ?, "
                "validation_errors_json = ?, runtime_metadata_json = ?, ended_at = ? WHERE id = ?",
                (
                    target_state,
                    metadata.get("prompt_hash"),
                    prompt_tokens,
                    int(metadata.get("reasoning_tokens", 0)),
                    int(metadata.get("final_tokens", 0)),
                    self.database.settings.relative_to_workdir(raw_path) if raw_path else None,
                    canonical_json(validation_errors),
                    canonical_json(metadata),
                    utc_now(),
                    attempt_id,
                ),
            )
            connection.execute(
                "UPDATE analysis_runs SET prompt_tokens = prompt_tokens + ?, "
                "token_saturated = CASE WHEN prompt_tokens + ? >= ? THEN 1 ELSE token_saturated END "
                "WHERE id = ?",
                (
                    prompt_tokens,
                    prompt_tokens,
                    token_limit,
                    envelope["analysis_run_id"],
                ),
            )

    def _rank(self, run_id: str, cancellation_requested: Callable[[], bool]) -> str:
        if cancellation_requested():
            raise AnalysisCancelled("Analysis cancellation was requested")
        rows = [
            dict(row)
            for row in self.database.fetch_all("SELECT * FROM clip_proposals WHERE analysis_run_id = ?", (run_id,))
        ]
        run = self.database.fetch_one(
            "SELECT requested_more_from_run_id FROM analysis_runs WHERE id = ?",
            (run_id,),
        )
        boundary_target = self.database.fetch_one(
            "SELECT parent_queue_snapshot_id, superseded_proposal_id "
            "FROM boundary_reanalysis_targets WHERE analysis_run_id = ?",
            (run_id,),
        )
        pinned_rows: list[dict[str, object]] = []
        parent_queue_snapshot_id = None
        if boundary_target is not None:
            parent_queue_snapshot_id = str(boundary_target["parent_queue_snapshot_id"])
        elif run is not None and run["requested_more_from_run_id"] is not None:
            parent = self.database.fetch_one(
                "SELECT queue_snapshot_id FROM analysis_runs WHERE id = ?",
                (run["requested_more_from_run_id"],),
            )
            parent_queue_snapshot_id = str(parent["queue_snapshot_id"])
        if parent_queue_snapshot_id is not None:
            pinned_rows = [
                dict(row)
                for row in self.database.fetch_all(
                    "SELECT p.*, e.rank AS parent_rank, e.baseline_score, e.diversity_json "
                    "FROM queue_entries e JOIN clip_proposals p ON p.id = e.clip_proposal_id "
                    "WHERE e.queue_snapshot_id = ? ORDER BY e.rank",
                    (parent_queue_snapshot_id,),
                )
            ]
            for row in pinned_rows:
                row["diversity"] = json.loads(str(row["diversity_json"]))

        for row in [*pinned_rows, *rows]:
            transcript_evidence = self.database.fetch_all(
                "SELECT e.content FROM evidence_items e JOIN proposal_evidence p "
                "ON p.evidence_item_id = e.id WHERE p.clip_proposal_id = ? "
                "AND e.evidence_type = 'transcript' ORDER BY e.start_us, e.id",
                (row["id"],),
            )
            row["semantic_text"] = " ".join(
                [str(row["summary"]), *(str(item["content"]) for item in transcript_evidence)]
            )
        profile = self.database.fetch_one(
            "SELECT p.category_priorities_json FROM creator_profile_revisions p "
            "JOIN analysis_runs r ON r.creator_profile_revision_id = p.id WHERE r.id = ?",
            (run_id,),
        )
        category_priorities = json.loads(str(profile["category_priorities_json"])) if profile else {}
        source = self.database.fetch_one(
            "SELECT s.source_end_us FROM source_recordings s JOIN analysis_runs r "
            "ON r.source_recording_id = s.id WHERE r.id = ?",
            (run_id,),
        )
        queue_target = (
            self.configuration.max_queue_size
            if self.configuration.budget_tier == "expanded"
            else default_queue_size(
                int(source["source_end_us"]),
                hard_cap=self.configuration.max_queue_size,
            )
        )
        if boundary_target is not None:
            successors = rank_proposals(
                rows,
                3,
                category_priorities=category_priorities,
            )
            replacement = successors[0] if successors else None
            replaced_parent: list[dict[str, object]] = []
            for proposal in pinned_rows:
                if proposal["id"] == boundary_target["superseded_proposal_id"]:
                    if replacement is not None:
                        replacement["diversity"] = {
                            **replacement["diversity"],
                            "boundary_reanalysis_replacement": True,
                            "supersedes_proposal_id": boundary_target["superseded_proposal_id"],
                        }
                        replaced_parent.append(replacement)
                else:
                    replaced_parent.append(proposal)
            extras = [row for row in rows if replacement is None or row["id"] != replacement["id"]]
            ranked = rank_proposals(
                extras,
                queue_target,
                category_priorities=category_priorities,
                pinned_rows=replaced_parent,
            )
        else:
            ranked = rank_proposals(
                rows,
                queue_target,
                category_priorities=category_priorities,
                pinned_rows=pinned_rows,
            )
        snapshot_id = new_id("queue")
        ranking_configuration = {
            "version": self.configuration.ranking_version,
            "judgment_weight": 1,
            "category_priority_weight": 2,
            "category_priorities": category_priorities,
            "risk_weight": 0,
            "queue_size_policy": {
                "proposals_per_source_hour": 3,
                "minimum_target": min(10, self.configuration.max_queue_size),
                "hard_cap": self.configuration.max_queue_size,
                "derived_target": queue_target,
                "budget_tier": self.configuration.budget_tier,
            },
            "coverage": {"category": True, "section_duration_seconds": 900},
            "temporal_duplicate_overlap_ratio": TEMPORAL_DUPLICATE_THRESHOLD,
            "semantic_duplicate_summary_token_jaccard": SEMANTIC_DUPLICATE_THRESHOLD,
            "tie_breakers": ["start_us", "proposal_id"],
            "pinned_parent_run_id": run["requested_more_from_run_id"] if run is not None else None,
            "pinned_parent_queue_snapshot_id": parent_queue_snapshot_id,
            "pinned_parent_count": len(pinned_rows),
            "boundary_reanalysis_target": dict(boundary_target) if boundary_target is not None else None,
        }
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO queue_snapshots "
                "(id, analysis_run_id, ranking_version, ranking_configuration_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    run_id,
                    self.configuration.ranking_version,
                    canonical_json(ranking_configuration),
                    utc_now(),
                ),
            )
            for rank, proposal in enumerate(ranked, start=1):
                connection.execute(
                    "INSERT INTO queue_entries "
                    "(queue_snapshot_id, clip_proposal_id, rank, baseline_score, diversity_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        proposal["id"],
                        rank,
                        proposal["baseline_score"],
                        canonical_json(proposal["diversity"]),
                    ),
                )
        return snapshot_id

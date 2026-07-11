from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .database import Database, utc_now
from .domain import AttemptState, canonical_json, new_id

ALLOWED_TRANSITIONS = {
    AttemptState.PENDING: {AttemptState.RUNNING, AttemptState.CANCELLED},
    AttemptState.RUNNING: {
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
        AttemptState.CANCELLED,
    },
    AttemptState.SUCCEEDED: set(),
    AttemptState.FAILED: set(),
    AttemptState.CANCELLED: set(),
}


@dataclass(frozen=True, slots=True)
class Attempt:
    id: str
    state: AttemptState
    attempt_number: int


class AttemptStore:
    def __init__(self, database: Database):
        self.database = database

    def create(
        self,
        *,
        scope_type: str,
        scope_id: str,
        stage_name: str,
        input_fingerprint: str,
        configuration_fingerprint: str,
        prior_attempt_id: str | None = None,
        checkpoint: dict[str, object] | None = None,
    ) -> Attempt:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) + 1 AS next_number "
                "FROM stage_attempts WHERE scope_type = ? AND scope_id = ? AND stage_name = ?",
                (scope_type, scope_id, stage_name),
            ).fetchone()
            attempt_id = new_id("attempt")
            number = int(row["next_number"])
            connection.execute(
                "INSERT INTO stage_attempts "
                "(id, scope_type, scope_id, stage_name, state, input_fingerprint, "
                "configuration_fingerprint, attempt_number, prior_attempt_id, checkpoint_json, "
                "created_at) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    scope_type,
                    scope_id,
                    stage_name,
                    input_fingerprint,
                    configuration_fingerprint,
                    number,
                    prior_attempt_id,
                    canonical_json(checkpoint) if checkpoint is not None else None,
                    utc_now(),
                ),
            )
            return Attempt(attempt_id, AttemptState.PENDING, number)

    def transition(
        self,
        attempt_id: str,
        target: AttemptState,
        *,
        owner_instance: str | None = None,
        retryable: bool = False,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> Attempt:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state, attempt_number, progress FROM stage_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown attempt: {attempt_id}")
            current = AttemptState(row["state"])
            if target not in ALLOWED_TRANSITIONS[current]:
                raise ValueError(f"Invalid attempt transition: {current} -> {target}")
            started_at = utc_now() if target is AttemptState.RUNNING else None
            ended_at = (
                utc_now()
                if target
                in {
                    AttemptState.SUCCEEDED,
                    AttemptState.FAILED,
                    AttemptState.CANCELLED,
                }
                else None
            )
            connection.execute(
                "UPDATE stage_attempts SET state = ?, owner_instance = COALESCE(?, owner_instance), "
                "started_at = COALESCE(?, started_at), ended_at = COALESCE(?, ended_at), "
                "progress = ?, retryable = ?, error_code = ?, error_summary = ? WHERE id = ?",
                (
                    target.value,
                    owner_instance,
                    started_at,
                    ended_at,
                    1.0 if target is AttemptState.SUCCEEDED else float(row["progress"]),
                    int(retryable),
                    error_code,
                    error_summary,
                    attempt_id,
                ),
            )
            return Attempt(attempt_id, target, int(row["attempt_number"]))

    def update_running(
        self,
        attempt_id: str,
        *,
        progress: float | None = None,
        worker_pid: int | None = None,
    ) -> None:
        if progress is not None and not 0 <= progress <= 1:
            raise ValueError("Attempt progress must be between 0 and 1")
        if worker_pid is not None and worker_pid <= 0:
            raise ValueError("Worker PID must be positive")
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state, progress FROM stage_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown attempt: {attempt_id}")
            if row["state"] != AttemptState.RUNNING.value:
                raise ValueError("Only a running attempt can report progress")
            next_progress = float(row["progress"]) if progress is None else progress
            if next_progress < float(row["progress"]):
                raise ValueError("Attempt progress cannot move backward")
            connection.execute(
                "UPDATE stage_attempts SET progress = ?, worker_pid = COALESCE(?, worker_pid) "
                "WHERE id = ?",
                (next_progress, worker_pid, attempt_id),
            )

    def request_cancellation(self, attempt_id: str, requester: str) -> None:
        with self.database.transaction(immediate=True) as connection:
            state = connection.execute("SELECT state FROM stage_attempts WHERE id = ?", (attempt_id,)).fetchone()
            if state is None:
                raise KeyError(f"Unknown attempt: {attempt_id}")
            if state["state"] != AttemptState.RUNNING.value:
                raise ValueError("Cancellation can be requested only for a running attempt")
            connection.execute(
                "INSERT INTO cancellation_requests(attempt_id, requested_at, requester) "
                "VALUES (?, ?, ?) ON CONFLICT(attempt_id) DO NOTHING",
                (attempt_id, utc_now(), requester),
            )

    def cancellation_requested(self, attempt_id: str) -> bool:
        return (
            self.database.fetch_one("SELECT 1 FROM cancellation_requests WHERE attempt_id = ?", (attempt_id,))
            is not None
        )

    def reconcile_interrupted(self, owner_instance: str) -> int:
        with self.database.transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "UPDATE stage_attempts SET state = 'failed', retryable = 1, "
                    "error_code = 'controller_interrupted', "
                    "error_summary = 'The controller stopped before this attempt completed.', "
                    "ended_at = ? WHERE state = 'running' AND owner_instance <> ?",
                    (utc_now(), owner_instance),
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeError("Could not reconcile interrupted attempts") from exc
            return cursor.rowcount

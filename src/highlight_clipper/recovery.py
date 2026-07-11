from __future__ import annotations

import ctypes
import os
import socket
import sqlite3
import threading
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime

from .database import Database, utc_now
from .domain import new_id

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _windows_creation_time(pid: int) -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel_time = ctypes.c_ulonglong()
        user_time = ctypes.c_ulonglong()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        return int(creation.value)
    finally:
        kernel32.CloseHandle(handle)


def current_owner_identity() -> str:
    creation = _windows_creation_time(os.getpid())
    return f"{socket.gethostname()}|{os.getpid()}|{creation if creation is not None else 'unknown'}"


def owner_is_live(identity: str) -> bool:
    try:
        host, pid_text, creation_text = identity.split("|", 2)
        pid = int(pid_text)
    except (ValueError, TypeError):
        return False
    if host.casefold() != socket.gethostname().casefold():
        return False
    if os.name == "nt":
        observed = _windows_creation_time(pid)
        if observed is None:
            return False
        return creation_text == "unknown" or observed == int(creation_text)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    stale_lease_recovered: bool
    attempts_reconciled: int
    quarantined_paths: tuple[str, ...]
    missing_valuable_artifacts: tuple[str, ...]


class OperationLeaseHeartbeat:
    def __init__(
        self,
        database: Database,
        operation_id: str,
        owner_instance: str,
        *,
        interval_seconds: float = 10.0,
    ):
        self.database = database
        self.operation_id = operation_id
        self.owner_instance = owner_instance
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lost = threading.Event()

    def __enter__(self) -> OperationLeaseHeartbeat:
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{self.operation_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with self.database.transaction(immediate=True) as connection:
                    cursor = connection.execute(
                        "UPDATE active_operation_lease SET heartbeat_at = ? "
                        "WHERE singleton = 1 AND operation_id = ? AND owner_instance = ?",
                        (utc_now(), self.operation_id, self.owner_instance),
                    )
                    if cursor.rowcount != 1:
                        self._lost.set()
                        return
            except sqlite3.Error:
                continue

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise RuntimeError("The active-operation lease was lost while work was running")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
            self._thread = None


def reconcile_startup(database: Database) -> RecoveryReport:
    stale_lease = False
    attempts_reconciled = 0
    with database.transaction(immediate=True) as connection:
        lease = connection.execute("SELECT * FROM active_operation_lease WHERE singleton = 1").fetchone()
        if lease and not owner_is_live(str(lease["owner_instance"])):
            stale_lease = True
            operation_id = str(lease["operation_id"])
            if lease["operation_type"] == "source_import":
                attempt = connection.execute(
                    "SELECT state, planned_source_recording_id FROM source_import_attempts WHERE id = ?",
                    (operation_id,),
                ).fetchone()
                if attempt and attempt["state"] == "running":
                    connection.execute(
                        "UPDATE source_import_attempts SET state = 'failed', retryable = 1, "
                        "error_code = 'controller_interrupted', "
                        "error_summary = 'The controller stopped before import completed.', ended_at = ? "
                        "WHERE id = ?",
                        (utc_now(), operation_id),
                    )
                    attempts_reconciled += 1
                    source_id = attempt["planned_source_recording_id"]
                    if source_id:
                        registered = connection.execute(
                            "SELECT 1 FROM source_recordings WHERE id = ?", (source_id,)
                        ).fetchone()
                        if not registered:
                            relative_path = f"sources/{source_id}"
                            connection.execute(
                                "INSERT INTO recovery_items "
                                "(id, item_type, relative_path, state, created_at) "
                                "VALUES (?, 'unregistered_source_tree', ?, 'pending', ?) "
                                "ON CONFLICT(relative_path) DO NOTHING",
                                (new_id("recovery"), relative_path, utc_now()),
                            )
            else:
                cursor = connection.execute(
                    "UPDATE analysis_runs SET state = 'failed', completed_at = ? WHERE id = ? AND state = 'running'",
                    (utc_now(), operation_id),
                )
                attempts_reconciled += cursor.rowcount
            cursor = connection.execute(
                "UPDATE stage_attempts SET state = 'failed', retryable = 1, "
                "error_code = 'controller_interrupted', "
                "error_summary = 'The controller stopped before this attempt completed.', ended_at = ? "
                "WHERE state = 'running' AND scope_id = ?",
                (utc_now(), operation_id),
            )
            attempts_reconciled += cursor.rowcount
            cursor = connection.execute(
                "UPDATE evaluation_attempts SET state = 'failed', ended_at = ? "
                "WHERE state = 'running' AND context_envelope_id IN "
                "(SELECT id FROM context_envelopes WHERE analysis_run_id = ?)",
                (utc_now(), operation_id),
            )
            attempts_reconciled += cursor.rowcount
            connection.execute("DELETE FROM active_operation_lease WHERE singleton = 1")

        running_exports = connection.execute(
            "SELECT idempotency_key, owner_instance FROM export_requests WHERE state = 'running'"
        ).fetchall()
        for request in running_exports:
            if not owner_is_live(str(request["owner_instance"])):
                connection.execute(
                    "UPDATE export_requests SET state = 'failed', "
                    "error_summary = 'The controller stopped before export registration completed.', "
                    "updated_at = ? WHERE idempotency_key = ?",
                    (utc_now(), request["idempotency_key"]),
                )
                attempts_reconciled += 1

    quarantined: list[str] = []
    quarantine_root = database.settings.work_dir / "tmp" / "recovery"
    for item in database.fetch_all("SELECT * FROM recovery_items WHERE state = 'pending' ORDER BY created_at, id"):
        path = database.settings.resolve_work_path(str(item["relative_path"]))
        try:
            if path.exists():
                quarantine_root.mkdir(parents=True, exist_ok=True)
                destination = quarantine_root / f"{path.name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
                path.replace(destination)
                quarantined.append(database.settings.relative_to_workdir(destination))
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE recovery_items SET state = 'completed', error_summary = NULL, "
                    "completed_at = ? WHERE id = ?",
                    (utc_now(), item["id"]),
                )
        except OSError as exc:
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE recovery_items SET error_summary = ? WHERE id = ?",
                    (str(exc)[:2000], item["id"]),
                )

    if database.fetch_one("SELECT 1 FROM active_operation_lease WHERE singleton = 1") is None:
        worker_root = database.settings.work_dir / "tmp" / "workers"
        for filename in ("request.json", "result.json"):
            for path in worker_root.glob(f"*/{filename}"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue

    missing_valuable: list[str] = []
    for artifact in database.fetch_all(
        "SELECT relative_path, kind, regenerable FROM artifacts WHERE removed_at IS NULL"
    ):
        path = database.settings.resolve_work_path(str(artifact["relative_path"]))
        if not path.is_file() and not int(artifact["regenerable"]):
            missing_valuable.append(f"{artifact['kind']}:{artifact['relative_path']}")
    return RecoveryReport(
        stale_lease,
        attempts_reconciled,
        tuple(quarantined),
        tuple(sorted(missing_valuable)),
    )

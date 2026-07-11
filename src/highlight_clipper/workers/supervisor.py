from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from ..runtime import configure_local_caches
from ..settings import Settings

WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102
INFINITE = 0xFFFFFFFF
CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class WorkerError(RuntimeError):
    pass


class WorkerCancelled(WorkerError):
    pass


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJob(AbstractContextManager):
    def __init__(self) -> None:
        self.handle: int | None = None

    def __enter__(self) -> WindowsJob:
        if os.name != "nt":
            return self
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, "SetInformationJobObject failed")
        self.handle = handle
        return self

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        if os.name != "nt":
            return
        if self.handle is None:
            raise RuntimeError("Windows Job Object is not open")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        if not kernel32.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.get_last_error()
            process.kill()
            process.wait(timeout=30)
            raise OSError(error, "AssignProcessToJobObject failed")
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = ntdll.NtResumeProcess(int(process._handle))
        if status != 0:
            try:
                self.terminate(7)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=30)
            raise OSError(int(status), "NtResumeProcess failed")

    def terminate(self, exit_code: int = 1) -> None:
        if os.name == "nt" and self.handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            if not kernel32.TerminateJobObject(self.handle, exit_code):
                raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if os.name == "nt" and self.handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            if not kernel32.CloseHandle(self.handle) and exc_type is None:
                error = ctypes.get_last_error()
                self.handle = None
                raise OSError(error, "CloseHandle failed for Windows Job Object")
            self.handle = None


class GpuMutex(AbstractContextManager):
    def __init__(self, settings: Settings, timeout_seconds: float = 30.0):
        device = os.environ.get("HIGHLIGHT_CLIPPER_GPU_DEVICE", "0")
        digest = hashlib.sha256(device.casefold().encode()).hexdigest()[:16]
        self.name = f"Global\\HighlightClipperGpuLease-v1-{digest}"
        self.timeout_ms = int(timeout_seconds * 1000)
        self.handle: int | None = None

    def __enter__(self) -> GpuMutex:
        if os.name != "nt":
            return self
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        result = kernel32.WaitForSingleObject(handle, self.timeout_ms)
        if result not in {WAIT_OBJECT_0, WAIT_ABANDONED}:
            kernel32.CloseHandle(handle)
            if result == WAIT_TIMEOUT:
                raise WorkerError("Timed out waiting for the exclusive GPU lease")
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        self.handle = handle
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if os.name == "nt" and self.handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel32.ReleaseMutex.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.ReleaseMutex(self.handle)
            kernel32.CloseHandle(self.handle)
            self.handle = None


@dataclass(frozen=True, slots=True)
class WorkerResult:
    payload: dict[str, object]
    pid: int
    stdout_log: Path
    stderr_log: Path
    elapsed_seconds: float
    vram_before_mib: int | None
    vram_after_mib: int | None


def used_vram_mib() -> int | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return sum(int(line.strip()) for line in result.stdout.splitlines() if line.strip())
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        return None


class WorkerSupervisor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def run_json_worker(
        self,
        module: str,
        request: dict[str, object],
        *,
        timeout_seconds: float,
        gpu: bool,
        cancellation_requested: Callable[[], bool] | None = None,
        worker_started: Callable[[int], None] | None = None,
    ) -> WorkerResult:
        worker_id = f"worker-{secrets.token_hex(12)}"
        directory = self.settings.work_dir / "tmp" / "workers" / worker_id
        directory.mkdir(parents=True, exist_ok=False)
        request_path = directory / "request.json"
        output_path = directory / "result.json"
        stdout_path = directory / "stdout.log"
        stderr_path = directory / "stderr.log"
        request_payload = {**request, "output_path": str(output_path)}
        environment = os.environ.copy()
        configure_local_caches(self.settings)
        for key in ("HF_HOME", "HF_HUB_CACHE", "TORCH_HOME", "XDG_CACHE_HOME"):
            environment[key] = os.environ[key]
        environment.update(
            {
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_DATASETS_OFFLINE": "1",
                "PYTHONUTF8": "1",
            }
        )
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        if os.name == "nt":
            flags |= CREATE_SUSPENDED
        mutex = GpuMutex(self.settings, timeout_seconds=60) if gpu else _NullContext()
        vram_before = used_vram_mib() if gpu else None
        started = time.monotonic()
        request_path.write_text(json.dumps(request_payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        try:
            with mutex, WindowsJob() as job, stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    [sys.executable, "-m", module, "--request", str(request_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=self.settings.repository_root,
                    env=environment,
                    shell=False,
                    creationflags=flags,
                )
                try:
                    job.assign_and_resume(process)
                    if worker_started is not None:
                        worker_started(process.pid)
                    while process.poll() is None:
                        if cancellation_requested and cancellation_requested():
                            self._terminate(process, job, 2)
                            raise WorkerCancelled("Worker cancellation was requested")
                        if time.monotonic() - started > timeout_seconds:
                            self._terminate(process, job, 3)
                            raise WorkerError(f"Worker exceeded its {timeout_seconds:.0f}-second deadline")
                        time.sleep(0.2)
                    if process.returncode != 0:
                        detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                        raise WorkerError(f"Worker exited with code {process.returncode}: {detail}")
                except BaseException:
                    if process.poll() is None:
                        self._terminate(process, job, 4)
                    raise
            if not output_path.is_file():
                raise WorkerError("Worker exited without a result artifact")
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise WorkerError("Worker result artifact is invalid JSON") from exc
            vram_after = used_vram_mib() if gpu else None
            return WorkerResult(
                payload=payload,
                pid=process.pid,
                stdout_log=stdout_path,
                stderr_log=stderr_path,
                elapsed_seconds=time.monotonic() - started,
                vram_before_mib=vram_before,
                vram_after_mib=vram_after,
            )
        finally:
            request_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes], job: WindowsJob, exit_code: int) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                job.terminate(exit_code)
            else:
                process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            pass
        process.kill()
        process.wait(timeout=15)


class _NullContext(AbstractContextManager):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .adapters.ffmpeg import FFmpegAdapter
from .settings import Settings


def configure_local_caches(settings: Settings) -> None:
    os.environ["HF_HOME"] = str(settings.work_dir / "cache" / "huggingface")
    os.environ["HF_HUB_CACHE"] = str(settings.work_dir / "cache" / "huggingface" / "hub")
    os.environ["TORCH_HOME"] = str(settings.work_dir / "cache" / "torch")
    os.environ["UV_CACHE_DIR"] = str(settings.work_dir / "cache" / "uv")
    os.environ["XDG_CACHE_HOME"] = str(settings.work_dir / "cache")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _command_output(arguments: list[str]) -> str | None:
    try:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip()


def target_machine_manifest(settings: Settings, *, check_media: bool = True) -> dict[str, object]:
    disk = shutil.disk_usage(settings.work_dir)
    tools: dict[str, object] = {}
    for name in ("ffmpeg", "ffprobe"):
        executable = shutil.which(name)
        if executable:
            path = Path(executable).resolve()
            tools[name] = {"path": str(path), "sha256": _sha256(path)}
    if check_media:
        tools["media_capabilities"] = FFmpegAdapter().preflight()
    gpu = _command_output(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    cuda = _command_output(["nvcc", "--version"])
    path_dlls: list[dict[str, str]] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate_dir = Path(directory)
        if not candidate_dir.is_dir():
            continue
        for pattern in ("cudnn64_*.dll", "cublas64_*.dll"):
            for candidate in candidate_dir.glob(pattern):
                try:
                    path_dlls.append({"path": str(candidate.resolve()), "sha256": _sha256(candidate)})
                except OSError:
                    continue
    installed_assets: list[dict[str, object]] = []
    for manifest_path in sorted(settings.work_dir.glob("**/asset-manifest.json")):
        try:
            relative_path = settings.relative_to_workdir(manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            installed_assets.append(
                {
                    "manifest_path": relative_path,
                    "manifest_sha256": _sha256(manifest_path),
                    "profile_id": manifest.get("profile_id"),
                    "profile_fingerprint": manifest.get("profile_fingerprint"),
                    "repository": manifest.get("repository"),
                    "revision": manifest.get("revision"),
                    "runtime_tag": manifest.get("tag"),
                }
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "operating_system": platform.platform(),
        "python": {"version": sys.version, "executable": sys.executable, "prefix": sys.prefix},
        "cpu": {"processor": platform.processor(), "logical_cores": os.cpu_count()},
        "memory": _command_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_OperatingSystem | "
                "Select-Object TotalVisibleMemorySize,FreePhysicalMemory,TotalVirtualMemorySize,FreeVirtualMemory | "
                "ConvertTo-Json -Compress",
            ]
        ),
        "pagefile": _command_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_PageFileUsage | "
                "Select-Object Name,AllocatedBaseSize,CurrentUsage,PeakUsage | "
                "ConvertTo-Json -Compress",
            ]
        ),
        "storage": {
            "work_dir": str(settings.work_dir),
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        },
        "gpu": gpu,
        "cuda": cuda,
        "cuda_runtime_dlls": path_dlls,
        "installed_assets": installed_assets,
        "locale": _command_output(["powershell", "-NoProfile", "-Command", "Get-Culture | ConvertTo-Json -Compress"]),
        "tools": tools,
    }


def write_runtime_manifest(settings: Settings, *, check_media: bool = True) -> Path:
    manifest = target_machine_manifest(settings, check_media=check_media)
    destination = settings.work_dir / "state" / "runtime-manifest.json"
    partial = destination.with_name(f"{destination.name}.partial")
    partial.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    partial.replace(destination)
    return destination

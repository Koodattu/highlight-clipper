from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
import uuid
import zipfile
from pathlib import Path

from .model_profiles import ModelAssetProfile, get_model_profile, load_catalog
from .runtime import configure_local_caches
from .settings import Settings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_manifest(directory: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(directory).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != "asset-manifest.json"
    ]


def _write_manifest(directory: Path, value: dict[str, object]) -> Path:
    path = directory / "asset-manifest.json"
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    partial.replace(path)
    return path


def verify_asset_directory(
    directory: Path,
    *,
    expected_profile: ModelAssetProfile | None = None,
    expected_runtime_tag: str | None = None,
) -> dict[str, object]:
    manifest_path = directory / "asset-manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Asset manifest is missing: {directory}")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        raise RuntimeError(f"Asset manifest has an unsupported schema: {directory}")
    if expected_profile is not None:
        expected_identity = {
            "profile_id": expected_profile.profile_id,
            "profile_fingerprint": expected_profile.identity_fingerprint,
            "repository": expected_profile.repository,
            "revision": expected_profile.revision,
        }
        mismatched = [key for key, value in expected_identity.items() if manifest.get(key) != value]
        if mismatched:
            raise RuntimeError("Asset manifest does not match the selected model profile: " + ", ".join(mismatched))
    if expected_runtime_tag is not None and manifest.get("tag") != expected_runtime_tag:
        raise RuntimeError("Asset manifest does not match the selected llama.cpp runtime tag")

    expected_paths: set[str] = set()
    for expected in manifest["files"]:
        relative = Path(str(expected["path"]))
        if relative.is_absolute() or not relative.parts:
            raise RuntimeError(f"Asset manifest contains an unsafe path: {relative}")
        path = (directory / relative).resolve()
        try:
            path.relative_to(directory.resolve())
        except ValueError as exc:
            raise RuntimeError(f"Asset manifest contains an unsafe path: {relative}") from exc
        normalized = relative.as_posix()
        if normalized in expected_paths:
            raise RuntimeError(f"Asset manifest lists a duplicate path: {normalized}")
        expected_paths.add(normalized)
        if not path.is_file() or path.stat().st_size != expected["size_bytes"] or _sha256(path) != expected["sha256"]:
            raise RuntimeError(f"Asset hash mismatch: {path}")
    actual_paths = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and path.name != "asset-manifest.json"
    }
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise RuntimeError(
            f"Asset directory contents differ from its manifest (missing={missing}, unexpected={unexpected})"
        )
    return {**manifest, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest()}


def download_model_profile(settings: Settings, profile_id: str) -> Path:
    profile = get_model_profile(profile_id)
    destination = profile.local_directory(settings)
    if (destination / "asset-manifest.json").is_file():
        verify_asset_directory(destination, expected_profile=profile)
        return destination
    if profile.estimated_download_bytes is not None:
        required = profile.estimated_download_bytes * 2 + 5 * 1024 * 1024 * 1024
        free = shutil.disk_usage(settings.work_dir).free
        if free < required:
            raise RuntimeError(
                f"Not enough free disk for {profile_id}: need about {required} bytes including cache reserve, "
                f"have {free} bytes"
            )
    try:
        from huggingface_hub import hf_hub_download, snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "Hugging Face setup support is not installed; sync the 'models' optional dependency"
        ) from exc
    configure_local_caches(settings)
    destination.mkdir(parents=True, exist_ok=True)
    if profile.files:
        for filename in profile.files:
            hf_hub_download(
                repo_id=profile.repository,
                filename=filename,
                revision=profile.revision,
                local_dir=destination,
                cache_dir=settings.work_dir / "cache" / "huggingface" / "hub",
            )
    else:
        snapshot_download(
            repo_id=profile.repository,
            revision=profile.revision,
            local_dir=destination,
            cache_dir=settings.work_dir / "cache" / "huggingface" / "hub",
            ignore_patterns=("*.msgpack", "*.h5", "*.ot", "*.onnx", "*.tflite"),
        )
    files = _tree_manifest(destination)
    if not files:
        raise RuntimeError(f"Model profile downloaded no files: {profile_id}")
    _write_manifest(
        destination,
        {
            "schema_version": 1,
            "profile_id": profile.profile_id,
            "profile_fingerprint": profile.identity_fingerprint,
            "repository": profile.repository,
            "revision": profile.revision,
            "files": files,
        },
    )
    verify_asset_directory(destination, expected_profile=profile)
    return destination


def _download(url: str, destination: Path, expected_sha256: str) -> None:
    if destination.is_file() and _sha256(destination) == expected_sha256:
        return
    partial = destination.with_name(f"{destination.name}.partial")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "highlight-clipper-setup/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("xb") as output:
        shutil.copyfileobj(response, output, length=8 * 1024 * 1024)
        output.flush()
        os.fsync(output.fileno())
    if _sha256(partial) != expected_sha256:
        raise RuntimeError(f"Downloaded archive hash mismatch: {destination.name}")
    partial.replace(destination)


def _extract_zip_safe(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise RuntimeError(f"Runtime archive contains an unsafe path: {member.filename}") from exc
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def install_llama_cpp_runtime(settings: Settings) -> Path:
    catalog = load_catalog()["llama_cpp"]
    tag = str(catalog["tag"])
    destination = settings.work_dir / "runtime" / "llama.cpp" / tag
    if (destination / "asset-manifest.json").is_file():
        verify_asset_directory(destination, expected_runtime_tag=tag)
        return destination
    downloads = settings.work_dir / "cache" / "downloads" / "llama.cpp" / tag
    downloads.mkdir(parents=True, exist_ok=True)
    for archive in catalog["archives"]:
        _download(str(archive["url"]), downloads / str(archive["name"]), str(archive["sha256"]))
    partial = destination.with_name(f"{destination.name}.partial-{uuid.uuid4().hex}")
    partial.mkdir(parents=True, exist_ok=False)
    for archive in catalog["archives"]:
        _extract_zip_safe(downloads / str(archive["name"]), partial)
    servers = list(partial.rglob("llama-server.exe"))
    if len(servers) != 1:
        raise RuntimeError("Pinned llama.cpp bundle did not contain exactly one llama-server.exe")
    server = servers[0]
    environment = os.environ.copy()
    runtime_directories = {str(server.parent), str(partial)}
    runtime_directories.update(str(path.parent) for path in partial.rglob("*.dll"))
    environment["PATH"] = os.pathsep.join(sorted(runtime_directories)) + os.pathsep + environment.get("PATH", "")
    help_result = subprocess.run(
        [str(server), "--help"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        timeout=60,
        cwd=server.parent,
        env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    required_flags = {
        "--api-key",
        "--cache-type-k",
        "--cache-type-v",
        "--ctx-size",
        "--fit",
        "--flash-attn",
        "--gpu-layers",
        "--host",
        "--no-mmproj",
        "--no-ui",
        "--n-predict",
        "--offline",
        "--parallel",
        "--port",
        "--spec-draft-model",
        "--spec-draft-n-max",
        "--spec-draft-ngl",
        "--spec-type",
    }
    missing = sorted(flag for flag in required_flags if flag not in help_result.stdout)
    if missing:
        raise RuntimeError("Pinned llama.cpp runtime lacks required flags: " + ", ".join(missing))
    files = _tree_manifest(partial)
    _write_manifest(
        partial,
        {
            "schema_version": 1,
            "tag": tag,
            "server_relative_path": server.relative_to(partial).as_posix(),
            "archives": catalog["archives"],
            "files": files,
        },
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial.replace(destination)
    verify_asset_directory(destination, expected_runtime_tag=tag)
    return destination

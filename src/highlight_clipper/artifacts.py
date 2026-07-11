from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .database import Database, utc_now
from .domain import canonical_json, fingerprint, new_id


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def copy_with_sha256(source: Path, partial_destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    partial_destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, partial_destination.open("xb") as writer:
        while chunk := reader.read(8 * 1024 * 1024):
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest(), size


class ArtifactStore:
    def __init__(self, database: Database):
        self.database = database
        self.settings = database.settings

    def require_intact(self, row) -> Path:
        kind = str(row["kind"])
        path = self.settings.resolve_work_path(str(row["relative_path"]))
        if not path.is_file():
            raise RuntimeError(f"Registered {kind} artifact is missing: {path}")
        expected_size = int(row["size_bytes"])
        if path.stat().st_size != expected_size:
            raise RuntimeError(f"Registered {kind} artifact has an unexpected size: {path}")
        expected_hash = row["sha256"]
        if not expected_hash:
            raise RuntimeError(f"Registered {kind} artifact has no integrity hash: {path}")
        if sha256_file(path) != str(expected_hash):
            raise RuntimeError(f"Registered {kind} artifact failed its integrity hash: {path}")
        return path

    def register(
        self,
        connection,
        *,
        path: Path,
        kind: str,
        owner_type: str,
        owner_id: str,
        configuration: dict[str, object],
        source_recording_id: str | None = None,
        require_hash: bool = False,
        regenerable: bool = False,
        integrity: dict[str, object] | None = None,
        precomputed_sha256: str | None = None,
        precomputed_size: int | None = None,
    ) -> str:
        relative_path = self.settings.relative_to_workdir(path)
        size = path.stat().st_size if precomputed_size is None else precomputed_size
        digest = precomputed_sha256
        if require_hash and digest is None:
            digest = sha256_file(path)
        if digest is not None and len(digest) != 64:
            raise ValueError("Precomputed artifact SHA-256 is invalid")
        artifact_id = new_id("artifact")
        connection.execute(
            "INSERT INTO artifacts "
            "(id, source_recording_id, owner_type, owner_id, kind, relative_path, size_bytes, "
            "sha256, integrity_json, configuration_fingerprint, regenerable, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                source_recording_id,
                owner_type,
                owner_id,
                kind,
                relative_path,
                size,
                digest,
                canonical_json(integrity or {"validated": True}),
                fingerprint(configuration),
                int(regenerable),
                utc_now(),
            ),
        )
        return artifact_id

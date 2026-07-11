from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .domain import fingerprint
from .settings import Settings


@dataclass(frozen=True, slots=True)
class ModelAssetProfile:
    profile_id: str
    kind: str
    repository: str
    revision: str
    destination: str
    files: tuple[str, ...] = ()
    architecture: str | None = None
    mtp: str | None = None
    execution: dict[str, object] | None = None
    estimated_download_bytes: int | None = None

    @property
    def identity_fingerprint(self) -> str:
        return fingerprint(
            {
                "profile_id": self.profile_id,
                "kind": self.kind,
                "repository": self.repository,
                "revision": self.revision,
                "files": self.files,
                "architecture": self.architecture,
                "mtp": self.mtp,
                "execution": self.execution,
                "estimated_download_bytes": self.estimated_download_bytes,
            }
        )

    def local_directory(self, settings: Settings) -> Path:
        return settings.resolve_work_path(self.destination)


def load_catalog() -> dict[str, object]:
    path = resources.files("highlight_clipper.assets").joinpath("model-catalog.json")
    return json.loads(path.read_text(encoding="utf-8"))


def get_model_profile(profile_id: str) -> ModelAssetProfile:
    catalog = load_catalog()
    try:
        raw = catalog["profiles"][profile_id]
    except KeyError as exc:
        raise KeyError(f"Unknown model profile: {profile_id}") from exc
    return ModelAssetProfile(
        profile_id=profile_id,
        kind=str(raw["kind"]),
        repository=str(raw["repository"]),
        revision=str(raw["revision"]),
        destination=str(raw["destination"]),
        files=tuple(raw.get("files", [])),
        architecture=raw.get("architecture"),
        mtp=raw.get("mtp"),
        execution=raw.get("execution"),
        estimated_download_bytes=raw.get("estimated_download_bytes"),
    )

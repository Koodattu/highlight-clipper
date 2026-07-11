from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

WORK_DIRECTORIES = (
    "artifacts",
    "backups",
    "cache/huggingface",
    "cache/torch",
    "cache/uv",
    "exports",
    "logs",
    "models/asr",
    "models/embeddings",
    "models/llm",
    "runtime/ffmpeg",
    "runtime/llama.cpp",
    "sources",
    "state",
    "tmp",
)


def find_repository_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / ".gitignore").is_file():
            return candidate
    raise RuntimeError("Could not find the highlight-clipper repository root")


@dataclass(frozen=True, slots=True)
class Settings:
    repository_root: Path
    work_dir: Path

    @classmethod
    def discover(cls, repository_root: Path | None = None) -> Settings:
        root = (repository_root or find_repository_root()).resolve()
        override = os.environ.get("HIGHLIGHT_CLIPPER_WORKDIR")
        private_root = (root / "workdir").resolve()
        if override:
            requested = Path(override)
            work_dir = (requested if requested.is_absolute() else root / requested).resolve()
        else:
            work_dir = private_root
        try:
            work_dir.relative_to(private_root)
        except ValueError as exc:
            raise RuntimeError("The Work Directory override must stay below the Git-ignored workdir directory") from exc
        return cls(repository_root=root, work_dir=work_dir)

    @property
    def database_path(self) -> Path:
        return self.work_dir / "state" / "highlight-clipper.sqlite3"

    def ensure_work_directories(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        for relative in WORK_DIRECTORIES:
            (self.work_dir / relative).mkdir(parents=True, exist_ok=True)

    def relative_to_workdir(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.work_dir.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"Path is outside the Work Directory: {resolved}") from exc

    def resolve_work_path(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise ValueError("Artifact path must be a non-empty relative path")
        resolved = (self.work_dir / relative_path).resolve()
        try:
            resolved.relative_to(self.work_dir.resolve())
        except ValueError as exc:
            raise ValueError("Artifact path escapes the Work Directory") from exc
        return resolved

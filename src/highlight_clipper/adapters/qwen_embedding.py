from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from ..domain import fingerprint
from ..model_profiles import get_model_profile
from ..ports import EmbeddingItem, EmbeddingResult
from ..settings import Settings
from ..setup_assets import verify_asset_directory
from ..workers.supervisor import WorkerSupervisor

QUERY_INSTRUCTION = (
    "Instruct: Find transcript passages that contain the requested kind of publishable creator highlight.\n"
    "Query: {query}"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class QwenEmbeddingAdapter:
    """Controller-side client for a disposable CPU sentence-transformers worker."""

    def __init__(
        self,
        settings: Settings,
        *,
        model_profile_id: str = "qwen3-embedding-0.6b",
        batch_size: int = 16,
        supervisor: WorkerSupervisor | None = None,
    ):
        self.settings = settings
        self.model_profile_id = model_profile_id
        self.batch_size = batch_size
        self.supervisor = supervisor or WorkerSupervisor(settings)

    def embed(
        self,
        documents: tuple[EmbeddingItem, ...],
        queries: tuple[EmbeddingItem, ...],
        output_directory: Path,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        worker_started: Callable[[int], None] | None = None,
    ) -> EmbeddingResult:
        profile = get_model_profile(self.model_profile_id)
        if profile.kind != "embedding_snapshot":
            raise RuntimeError("Selected embedding profile is not a sentence-transformers snapshot")
        model_directory = profile.local_directory(self.settings).resolve()
        model_manifest = verify_asset_directory(model_directory, expected_profile=profile)
        output_directory = output_directory.resolve()
        self.settings.relative_to_workdir(output_directory)
        request_fingerprint = fingerprint(
            {
                "adapter": "sentence-transformers-worker-v1",
                "model_profile_id": profile.profile_id,
                "model_revision": profile.revision,
                "model_manifest_sha256": model_manifest["manifest_sha256"],
                "query_instruction": QUERY_INSTRUCTION,
                "documents": [(item.key, fingerprint(item.text)) for item in documents],
                "queries": [(item.key, fingerprint(item.text)) for item in queries],
                "batch_size": self.batch_size,
            }
        )
        formatted_queries = tuple(
            EmbeddingItem(item.key, QUERY_INSTRUCTION.format(query=item.text)) for item in queries
        )
        worker = self.supervisor.run_json_worker(
            "highlight_clipper.workers.embed_main",
            {
                "model_path": str(model_directory),
                "model_profile_id": profile.profile_id,
                "model_revision": profile.revision,
                "model_manifest_sha256": model_manifest["manifest_sha256"],
                "documents": [{"key": item.key, "text": item.text} for item in documents],
                "queries": [{"key": item.key, "text": item.text} for item in formatted_queries],
                "output_directory": str(output_directory),
                "batch_size": self.batch_size,
                "device": "cpu",
                "fingerprint": request_fingerprint,
            },
            timeout_seconds=12 * 60 * 60,
            gpu=False,
            cancellation_requested=cancellation_requested,
            worker_started=worker_started,
        )
        vector_path = Path(str(worker.payload["vector_path"])).resolve(strict=True)
        manifest_path = Path(str(worker.payload["manifest_path"])).resolve(strict=True)
        for path in (vector_path, manifest_path):
            self.settings.relative_to_workdir(path)
            try:
                path.relative_to(output_directory)
            except ValueError as exc:
                raise RuntimeError("Embedding worker returned an output outside its generation directory") from exc
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("fingerprint") != request_fingerprint:
            raise RuntimeError("Embedding manifest fingerprint does not match its request")
        if _sha256(vector_path) != manifest.get("vector_sha256"):
            raise RuntimeError("Embedding vector artifact hash does not match its manifest")
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("NumPy is required to validate embedding artifacts") from exc
        vectors = np.load(vector_path, allow_pickle=False, mmap_mode="r")
        expected_count = len(documents) + len(queries)
        if vectors.shape != (expected_count, int(manifest["dimension"])):
            raise RuntimeError("Embedding vector artifact has an unexpected shape")
        if not np.isfinite(vectors).all():
            raise RuntimeError("Embedding vector artifact contains non-finite values")
        norms = np.linalg.norm(vectors, axis=1)
        if not np.allclose(norms, 1.0, rtol=1e-4, atol=1e-4):
            raise RuntimeError("Embedding vector artifact is not normalized")
        if tuple(manifest["document_keys"]) != tuple(item.key for item in documents):
            raise RuntimeError("Embedding document order changed")
        if tuple(manifest["query_keys"]) != tuple(item.key for item in queries):
            raise RuntimeError("Embedding query order changed")
        return EmbeddingResult(
            vector_path=vector_path,
            manifest_path=manifest_path,
            document_keys=tuple(manifest["document_keys"]),
            query_keys=tuple(manifest["query_keys"]),
            dimension=int(manifest["dimension"]),
            dtype=str(manifest["dtype"]),
            metadata={
                "request_fingerprint": request_fingerprint,
                "model_manifest_sha256": model_manifest["manifest_sha256"],
                "worker_pid": worker.pid,
                "elapsed_seconds": worker.elapsed_seconds,
                "vector_sha256": manifest["vector_sha256"],
                "vector_size_bytes": manifest["vector_size_bytes"],
            },
        )

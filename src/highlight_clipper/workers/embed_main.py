from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def _write_json_atomic(path: Path, value: object) -> None:
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    partial.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def embed(request: dict[str, object]) -> dict[str, object]:
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("sentence-transformers is not installed in the project environment") from exc

    model_path = Path(str(request["model_path"])).resolve(strict=True)
    output_directory = Path(str(request["output_directory"])).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    documents = list(request["documents"])
    queries = list(request["queries"])
    if not documents or not queries:
        raise ValueError("Embedding worker requires at least one document and one query")

    model = SentenceTransformer(
        str(model_path),
        device=str(request.get("device", "cpu")),
        trust_remote_code=True,
        local_files_only=True,
    )
    document_vectors = model.encode(
        [str(item["text"]) for item in documents],
        batch_size=int(request.get("batch_size", 16)),
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    query_vectors = model.encode(
        [str(item["text"]) for item in queries],
        batch_size=int(request.get("batch_size", 16)),
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    vectors = np.concatenate((document_vectors, query_vectors), axis=0).astype(np.float32, copy=False)
    if vectors.ndim != 2 or not np.isfinite(vectors).all():
        raise RuntimeError("Embedding model returned an invalid vector array")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not np.isfinite(norms).all() or np.any(norms <= 1e-12):
        raise RuntimeError("Embedding model returned a zero-length vector")
    vectors = vectors / norms

    vector_path = output_directory / "vectors.npy"
    vector_partial = vector_path.with_name(f"{vector_path.name}.partial")
    with vector_partial.open("wb") as handle:
        np.save(handle, vectors, allow_pickle=False)
        handle.flush()
        os.fsync(handle.fileno())
    vector_partial.replace(vector_path)
    vector_sha256 = _sha256(vector_path)

    manifest_path = output_directory / "embedding-manifest.json"
    manifest = {
        "schema_version": 1,
        "fingerprint": request["fingerprint"],
        "model_profile_id": request["model_profile_id"],
        "model_revision": request["model_revision"],
        "model_manifest_sha256": request["model_manifest_sha256"],
        "document_keys": [str(item["key"]) for item in documents],
        "query_keys": [str(item["key"]) for item in queries],
        "document_count": len(documents),
        "query_count": len(queries),
        "dimension": int(vectors.shape[1]),
        "dtype": str(vectors.dtype),
        "normalized": True,
        "vector_file": vector_path.name,
        "vector_sha256": vector_sha256,
        "vector_size_bytes": vector_path.stat().st_size,
    }
    _write_json_atomic(manifest_path, manifest)
    return {
        **manifest,
        "vector_path": str(vector_path),
        "manifest_path": str(manifest_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    arguments = parser.parse_args()
    request = json.loads(arguments.request.read_text(encoding="utf-8"))
    output = Path(str(request["output_path"])).resolve()
    _write_json_atomic(output, embed(request))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

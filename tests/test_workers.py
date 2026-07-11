from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import pytest

from highlight_clipper.adapters.faster_whisper import FasterWhisperAdapter
from highlight_clipper.adapters.qwen_embedding import (
    EMBEDDING_ATTENTION,
    EMBEDDING_COMPUTE_DTYPE,
    EMBEDDING_DEVICE,
    QwenEmbeddingAdapter,
)
from highlight_clipper.model_profiles import get_model_profile
from highlight_clipper.ports import EmbeddingItem
from highlight_clipper.settings import Settings
from highlight_clipper.workers.embed_main import embed
from highlight_clipper.workers.supervisor import WorkerCancelled, WorkerResult, WorkerSupervisor


class WorkerSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(Path(__file__).resolve().parents[1], root / "workdir")
        self.settings.ensure_work_directories()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_disposable_worker_returns_json_from_a_different_process(self) -> None:
        sentinel = "private-transcript-sentinel"
        result = WorkerSupervisor(self.settings).run_json_worker(
            "tests.worker_fixture",
            {"value": sentinel},
            timeout_seconds=30,
            gpu=False,
        )
        self.assertEqual(result.payload, {"echo": sentinel})
        self.assertGreater(result.pid, 0)
        self.assertFalse((result.stdout_log.parent / "request.json").exists())
        self.assertFalse((result.stdout_log.parent / "result.json").exists())

    def test_cancellation_terminates_a_blocking_worker(self) -> None:
        started = time.monotonic()
        with self.assertRaises(WorkerCancelled):
            WorkerSupervisor(self.settings).run_json_worker(
                "tests.worker_fixture",
                {"delay_seconds": 30},
                timeout_seconds=60,
                gpu=False,
                cancellation_requested=lambda: time.monotonic() - started > 0.3,
            )
        worker_directories = list((self.settings.work_dir / "tmp" / "workers").iterdir())
        self.assertEqual(len(worker_directories), 1)
        self.assertFalse((worker_directories[0] / "request.json").exists())
        self.assertFalse((worker_directories[0] / "result.json").exists())


class CapturingSupervisor:
    def __init__(self):
        self.requests: list[dict[str, object]] = []

    def run_json_worker(self, module, request, **kwargs):
        self.requests.append(request)
        if kwargs.get("worker_started") is not None:
            kwargs["worker_started"](123)
        temporary = Path(request["checkpoint_path"]).parent
        temporary.mkdir(parents=True, exist_ok=True)
        return WorkerResult(
            payload={"segments": []},
            pid=123,
            stdout_log=temporary / "stdout.log",
            stderr_log=temporary / "stderr.log",
            elapsed_seconds=0.1,
            vram_before_mib=0,
            vram_after_mib=0,
        )


class RejectingSupervisor:
    def __init__(self):
        self.module: str | None = None
        self.request: dict[str, object] | None = None
        self.options: dict[str, object] | None = None

    def run_json_worker(self, module, request, **kwargs):
        self.module = module
        self.request = request
        self.options = kwargs
        raise RuntimeError("request captured")


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class AsrIdentityTests(unittest.TestCase):
    def test_same_size_different_audio_cannot_share_a_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(root, root / "workdir")
            settings.ensure_work_directories()
            profile = get_model_profile("whisper-turbo")
            model_directory = profile.local_directory(settings)
            model_directory.mkdir(parents=True)
            model_file = model_directory / "model.bin"
            model_file.write_bytes(b"model")
            digest = hashlib.sha256(model_file.read_bytes()).hexdigest()
            manifest = {
                "schema_version": 1,
                "profile_id": profile.profile_id,
                "profile_fingerprint": profile.identity_fingerprint,
                "repository": profile.repository,
                "revision": profile.revision,
                "files": [{"path": model_file.name, "size_bytes": model_file.stat().st_size, "sha256": digest}],
            }
            (model_directory / "asset-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            first = root / "first.wav"
            second = root / "second.wav"
            first.write_bytes(b"A" * 128)
            second.write_bytes(b"B" * 128)
            supervisor = CapturingSupervisor()
            adapter = FasterWhisperAdapter(
                settings,
                model_directory,
                model_profile_id=profile.profile_id,
                model_revision=profile.revision,
                language="fi",
                supervisor=supervisor,  # type: ignore[arg-type]
            )
            adapter.transcribe(first)
            adapter.transcribe(second)
            self.assertEqual(supervisor.requests[0]["language"], "fi")
            self.assertNotEqual(supervisor.requests[0]["fingerprint"], supervisor.requests[1]["fingerprint"])
            self.assertNotEqual(supervisor.requests[0]["checkpoint_path"], supervisor.requests[1]["checkpoint_path"])


class EmbeddingGpuPolicyTests(unittest.TestCase):
    def test_adapter_requires_cuda_worker_and_records_the_execution_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(root, root / "workdir")
            settings.ensure_work_directories()
            profile = get_model_profile("qwen3-embedding-0.6b")
            model_directory = profile.local_directory(settings)
            model_directory.mkdir(parents=True)
            model_file = model_directory / "model.bin"
            model_file.write_bytes(b"model")
            digest = hashlib.sha256(model_file.read_bytes()).hexdigest()
            manifest = {
                "schema_version": 1,
                "profile_id": profile.profile_id,
                "profile_fingerprint": profile.identity_fingerprint,
                "repository": profile.repository,
                "revision": profile.revision,
                "files": [{"path": model_file.name, "size_bytes": model_file.stat().st_size, "sha256": digest}],
            }
            (model_directory / "asset-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            supervisor = RejectingSupervisor()
            adapter = QwenEmbeddingAdapter(settings, supervisor=supervisor)  # type: ignore[arg-type]

            with self.assertRaisesRegex(RuntimeError, "request captured"):
                adapter.embed(
                    (EmbeddingItem("document", "Finnish transcript"),),
                    (EmbeddingItem("query", "Find a highlight"),),
                    settings.work_dir / "artifacts" / "embeddings" / "test",
                )

            self.assertEqual(supervisor.module, "highlight_clipper.workers.embed_main")
            self.assertIsNotNone(supervisor.request)
            self.assertIsNotNone(supervisor.options)
            self.assertEqual(supervisor.request["device"], EMBEDDING_DEVICE)
            self.assertEqual(supervisor.request["compute_dtype"], EMBEDDING_COMPUTE_DTYPE)
            self.assertEqual(supervisor.request["attention_implementation"], EMBEDDING_ATTENTION)
            self.assertTrue(supervisor.options["gpu"])
            self.assertEqual(len(str(supervisor.request["fingerprint"])), 64)

    def test_worker_rejects_cpu_execution_before_loading_the_model(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires the CUDA device"):
            embed({"device": "cpu"})


@pytest.mark.gpu
@pytest.mark.real_model
@unittest.skipUnless(
    os.environ.get("HIGHLIGHT_CLIPPER_RUN_REAL_GPU_TESTS") == "1",
    "Set HIGHLIGHT_CLIPPER_RUN_REAL_GPU_TESTS=1 for the explicit CUDA embedding smoke",
)
class RealEmbeddingGpuTests(unittest.TestCase):
    def test_qwen_embedding_executes_on_cuda_and_writes_normalized_vectors(self) -> None:
        import torch

        self.assertTrue(torch.cuda.is_available())
        self.assertIsNotNone(torch.version.cuda)
        settings = Settings.discover()
        temporary_root = settings.work_dir / "tmp" / "real-model-tests"
        temporary_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as temporary:
            result = QwenEmbeddingAdapter(settings, batch_size=2).embed(
                (
                    EmbeddingItem("document:fi", "Tämä on lyhyt suomenkielinen testi."),
                    EmbeddingItem("document:en", "This is a short English test."),
                ),
                (EmbeddingItem("query", "Find a publishable highlight"),),
                Path(temporary) / "generation",
            )
        self.assertEqual(result.dimension, 1024)
        self.assertEqual(result.dtype, "float32")
        self.assertTrue(str(result.metadata["effective_device"]).startswith("cuda"))
        self.assertEqual(result.metadata["compute_dtype"], EMBEDDING_COMPUTE_DTYPE)


if __name__ == "__main__":
    unittest.main()

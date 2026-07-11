from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from ..artifacts import sha256_file
from ..domain import fingerprint
from ..model_profiles import get_model_profile
from ..ports import TranscriptionResult, TranscriptSegment, TranscriptWord
from ..settings import Settings
from ..setup_assets import verify_asset_directory
from ..timebase import seconds_to_us
from ..workers.supervisor import WorkerSupervisor

FASTER_WHISPER_VAD_PARAMETERS = {
    "min_silence_duration_ms": 500,
    "speech_pad_ms": 200,
}


class FasterWhisperAdapter:
    """Controller-side client for a disposable faster-whisper worker process."""

    def __init__(
        self,
        settings: Settings,
        model_path: Path,
        *,
        model_profile_id: str,
        model_revision: str,
        device: str = "cuda",
        compute_type: str = "float16",
        language: str | None = None,
        chunk_seconds: int = 900,
        overlap_seconds: int = 15,
        vad_parameters: dict[str, object] | None = None,
        supervisor: WorkerSupervisor | None = None,
    ):
        self.settings = settings
        self.model_path = model_path.resolve()
        self.model_profile_id = model_profile_id
        self.model_revision = model_revision
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.chunk_seconds = chunk_seconds
        self.overlap_seconds = overlap_seconds
        self.vad_parameters = dict(vad_parameters or FASTER_WHISPER_VAD_PARAMETERS)
        self.supervisor = supervisor or WorkerSupervisor(settings)

    def transcribe(
        self,
        audio_path: Path,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        worker_started: Callable[[int], None] | None = None,
    ) -> TranscriptionResult:
        if not self.model_path.is_dir():
            raise RuntimeError(f"ASR model directory does not exist: {self.model_path}")
        model_profile = get_model_profile(self.model_profile_id)
        if model_profile.kind != "asr_snapshot":
            raise RuntimeError("Selected ASR profile is not a faster-whisper snapshot")
        if self.model_path != model_profile.local_directory(self.settings).resolve():
            raise RuntimeError("ASR model path does not match the selected catalog profile")
        model_manifest = verify_asset_directory(self.model_path, expected_profile=model_profile)
        if self.model_revision != model_profile.revision:
            raise RuntimeError("ASR revision does not match the selected catalog profile")
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise RuntimeError("FFmpeg is required for chunked ASR")
        audio_path = audio_path.resolve(strict=True)
        audio_sha256 = sha256_file(audio_path)
        profile_fingerprint = fingerprint(
            {
                "adapter": "faster-whisper-worker-v1",
                "model_profile_id": self.model_profile_id,
                "model_revision": self.model_revision,
                "model_manifest_sha256": model_manifest["manifest_sha256"],
                "audio_sha256": audio_sha256,
                "audio_size_bytes": audio_path.stat().st_size,
                "device": self.device,
                "compute_type": self.compute_type,
                "language": self.language,
                "chunk_seconds": self.chunk_seconds,
                "overlap_seconds": self.overlap_seconds,
                "vad_parameters": self.vad_parameters,
            }
        )
        checkpoint_path = self.settings.work_dir / "artifacts" / "asr-checkpoints" / f"{profile_fingerprint}.json"
        worker = self.supervisor.run_json_worker(
            "highlight_clipper.workers.asr_main",
            {
                "audio_path": str(audio_path.resolve()),
                "model_path": str(self.model_path),
                "model_revision": self.model_revision,
                "model_profile_id": self.model_profile_id,
                "model_manifest_sha256": model_manifest["manifest_sha256"],
                "audio_sha256": audio_sha256,
                "checkpoint_path": str(checkpoint_path),
                "ffmpeg_path": str(Path(ffmpeg_path).resolve()),
                "device": self.device,
                "compute_type": self.compute_type,
                "language": self.language,
                "chunk_seconds": self.chunk_seconds,
                "overlap_seconds": self.overlap_seconds,
                "vad_parameters": self.vad_parameters,
                "fingerprint": profile_fingerprint,
            },
            timeout_seconds=24 * 60 * 60,
            gpu=self.device == "cuda",
            cancellation_requested=cancellation_requested,
            worker_started=worker_started,
        )
        raw_segments = list(worker.payload.get("segments", []))
        segments: list[TranscriptSegment] = []
        words: list[TranscriptWord] = []
        for raw in raw_segments:
            start_us = seconds_to_us(raw["start"])
            end_us = seconds_to_us(raw["end"])
            if end_us <= start_us or not str(raw["text"]).strip():
                continue
            output_index = len(segments)
            segments.append(
                TranscriptSegment(
                    start_us=start_us,
                    end_us=end_us,
                    text=str(raw["text"]).strip(),
                    language=str(raw["language"]) if raw.get("language") else None,
                    avg_log_probability=raw.get("avg_log_probability"),
                    no_speech_probability=raw.get("no_speech_probability"),
                )
            )
            for word in raw.get("words", []):
                word_start = seconds_to_us(word["start"])
                word_end = seconds_to_us(word["end"])
                if word_end <= word_start:
                    continue
                words.append(
                    TranscriptWord(
                        segment_index=output_index,
                        start_us=word_start,
                        end_us=word_end,
                        text=str(word["text"]),
                        probability=word.get("probability"),
                    )
                )
        return TranscriptionResult(
            segments=tuple(segments),
            words=tuple(words),
            raw=worker.payload,
            metadata={
                "profile_fingerprint": profile_fingerprint,
                "model_profile_id": self.model_profile_id,
                "model_manifest_sha256": model_manifest["manifest_sha256"],
                "audio_sha256": audio_sha256,
                "worker_pid": worker.pid,
                "elapsed_seconds": worker.elapsed_seconds,
                "vram_before_mib": worker.vram_before_mib,
                "vram_after_mib": worker.vram_after_mib,
                "checkpoint_path": str(checkpoint_path),
            },
        )

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .adapters.fake import FakeAsrAdapter, FakeEvaluatorAdapter
from .adapters.faster_whisper import FASTER_WHISPER_VAD_PARAMETERS, FasterWhisperAdapter
from .adapters.llama_cpp import LlamaCppEvaluatorAdapter, LlamaEvaluatorProfile
from .adapters.qwen_embedding import QwenEmbeddingAdapter
from .database import Database
from .domain import fingerprint
from .model_profiles import get_model_profile, load_catalog
from .ports import TranscriptSegment
from .workflows.analyze import AnalysisConfig, AnalysisWorkflow


@dataclass(frozen=True, slots=True)
class AnalysisSelection:
    mode: str = "real"
    asr_profile: str = "whisper-turbo"
    embedding_profile: str = "qwen3-embedding-0.6b"
    evaluator_profile: str = "qwen36-35b-a3b"
    evaluator_context_size: int = 32_768
    evaluator_mtp: bool = False
    budget_tier: str = "default"
    fake_transcript: str = "Wow, this funny story matters because the ending works."


def selection_from_configuration(configuration: dict[str, object]) -> AnalysisSelection:
    mode = "fake" if configuration.get("asr_profile") == "fake-v1" else "real"
    return AnalysisSelection(
        mode=mode,
        asr_profile=str(configuration.get("asr_profile", "whisper-turbo")),
        embedding_profile=str(configuration.get("embedding_profile", "qwen3-embedding-0.6b")),
        evaluator_profile=str(configuration.get("evaluator_profile", "qwen36-35b-a3b")),
        evaluator_context_size=int(configuration.get("evaluator_context_size", 32_768)),
        evaluator_mtp=bool(configuration.get("evaluator_mtp", False)),
        budget_tier=str(configuration.get("budget_tier", "default")),
    )


def build_analysis_workflow(
    database: Database,
    selection: AnalysisSelection,
    *,
    source_end_us: int,
    cancellation_requested: Callable[[], bool] | None = None,
) -> AnalysisWorkflow:
    if selection.mode == "fake":
        segment_end = max(1, source_end_us - 1)
        segment_start = max(0, segment_end - min(60_000_000, segment_end))
        segments = (
            TranscriptSegment(
                start_us=segment_start,
                end_us=segment_end,
                text=selection.fake_transcript,
                language="en",
            ),
        )
        return AnalysisWorkflow(
            database,
            FakeAsrAdapter(segments),
            FakeEvaluatorAdapter(),
            configuration=AnalysisConfig(
                asr_profile="fake-v1",
                embedding_profile="none",
                evaluator_profile="fake-v1",
                retrieval_version="text-audio-speech-section-balanced-v3",
                asr_execution_identity="fake-asr-v1",
                embedding_execution_identity="none",
                evaluator_execution_identity="fake-evaluator-v1",
                budget_tier=selection.budget_tier,
            ),
            external_cancellation_requested=cancellation_requested,
        )
    if selection.mode != "real":
        raise ValueError(f"Unknown analysis mode: {selection.mode}")
    if not 8_192 <= selection.evaluator_context_size <= 262_144:
        raise ValueError("Evaluator context size must be between 8,192 and 262,144 tokens")

    asr_profile = get_model_profile(selection.asr_profile)
    if asr_profile.kind != "asr_snapshot":
        raise ValueError("Selected ASR profile is not supported by the faster-whisper worker")
    if selection.embedding_profile != "qwen3-embedding-0.6b":
        raise ValueError("Only qwen3-embedding-0.6b is promoted for the first real workflow")
    evaluator_profile = LlamaEvaluatorProfile.from_catalog(
        selection.evaluator_profile,
        mtp=selection.evaluator_mtp,
        context_size=selection.evaluator_context_size,
    )
    embedding_asset = get_model_profile(selection.embedding_profile)
    evaluator_asset = get_model_profile(selection.evaluator_profile)
    asr_execution_identity = fingerprint(
        {
            "adapter": "faster-whisper-worker-v1",
            "asset": asr_profile.identity_fingerprint,
            "device": "cuda",
            "compute_type": "float16",
            "language": None,
            "chunk_seconds": 900,
            "overlap_seconds": 15,
            "vad_parameters": FASTER_WHISPER_VAD_PARAMETERS,
        }
    )
    embedding_execution_identity = fingerprint(
        {
            "adapter": "sentence-transformers-worker-v1",
            "asset": embedding_asset.identity_fingerprint,
            "batch_size": 16,
            "device": "cpu",
            "query_instruction_version": 1,
        }
    )
    evaluator_execution_identity = fingerprint(
        {
            "adapter": "llama-cpp-anchored-json-v7",
            "asset": evaluator_asset.identity_fingerprint,
            "runtime": load_catalog()["llama_cpp"],
            "context_size": evaluator_profile.context_size,
            "output_tokens": evaluator_profile.output_tokens,
            "temperature": evaluator_profile.temperature,
            "top_p": evaluator_profile.top_p,
            "top_k": evaluator_profile.top_k,
            "min_p": evaluator_profile.min_p,
            "presence_penalty": evaluator_profile.presence_penalty,
            "repeat_penalty": evaluator_profile.repeat_penalty,
            "chat_template_kwargs": evaluator_profile.chat_template_kwargs,
            "seed": evaluator_profile.seed,
            "mtp": evaluator_profile.mtp,
        }
    )
    return AnalysisWorkflow(
        database,
        FasterWhisperAdapter(
            database.settings,
            asr_profile.local_directory(database.settings),
            model_profile_id=asr_profile.profile_id,
            model_revision=asr_profile.revision,
        ),
        LlamaCppEvaluatorAdapter(database.settings, evaluator_profile),
        embedding=QwenEmbeddingAdapter(
            database.settings,
            model_profile_id=selection.embedding_profile,
        ),
        configuration=AnalysisConfig(
            asr_profile=selection.asr_profile,
            embedding_profile=selection.embedding_profile,
            evaluator_profile=selection.evaluator_profile,
            evaluator_context_size=selection.evaluator_context_size,
            evaluator_mtp=selection.evaluator_mtp,
            retrieval_version="text-audio-speech-embedding-section-balanced-v3",
            asr_execution_identity=asr_execution_identity,
            embedding_execution_identity=embedding_execution_identity,
            evaluator_execution_identity=evaluator_execution_identity,
            budget_tier=selection.budget_tier,
        ),
        external_cancellation_requested=cancellation_requested,
    )

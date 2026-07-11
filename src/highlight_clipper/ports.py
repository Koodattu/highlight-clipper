from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .domain import ProposalDraft


@dataclass(frozen=True, slots=True)
class SelectedStream:
    index: int
    codec: str
    time_base: str
    start_time: str | None
    duration: str | None
    disposition_default: bool


@dataclass(frozen=True, slots=True)
class MediaProbe:
    format_name: str
    format_duration: str | None
    format_start_time: str | None
    video_streams: tuple[SelectedStream, ...]
    audio_streams: tuple[SelectedStream, ...]
    raw: dict[str, object]


@dataclass(frozen=True, slots=True)
class ImportedMedia:
    source_end_us: int
    video_stream_index: int
    audio_stream_index: int
    manifest: dict[str, object]
    proxy_path: Path
    analysis_audio_path: Path


class MediaAdapter(Protocol):
    def probe(self, path: Path) -> MediaProbe: ...

    def prepare_import(
        self,
        source_path: Path,
        destination_dir: Path,
        video_stream: int | None,
        audio_stream: int | None,
    ) -> ImportedMedia: ...

    def render_export(
        self,
        source_path: Path,
        destination: Path,
        start_us: int,
        end_us: int,
        *,
        source_end_us: int,
        video_stream_index: int,
        audio_stream_index: int,
        video_origin_seconds: str,
        audio_start_seconds: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start_us: int
    end_us: int
    text: str
    language: str | None
    avg_log_probability: float | None = None
    no_speech_probability: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptWord:
    segment_index: int
    start_us: int
    end_us: int
    text: str
    probability: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    segments: tuple[TranscriptSegment, ...]
    words: tuple[TranscriptWord, ...] = ()
    raw: dict[str, object] | None = None
    metadata: dict[str, object] | None = None


CANDIDATE_OUTCOMES = frozenset(
    {
        "covered_by_proposal",
        "duplicate_of_proposal",
        "too_weak",
        "insufficient_context",
        "omitted_by_proposal_cap",
        "omitted_by_prompt_budget",
        "invalid_evaluator_output",
    }
)


@dataclass(frozen=True, slots=True)
class CandidateEvaluationOutcome:
    candidate_id: str
    outcome: str
    proposal_index: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in CANDIDATE_OUTCOMES:
            raise ValueError(f"Unknown Candidate Evaluation Outcome: {self.outcome}")
        links_proposal = self.outcome in {"covered_by_proposal", "duplicate_of_proposal"}
        if links_proposal and (self.proposal_index is None or self.proposal_index < 0):
            raise ValueError(f"{self.outcome} requires a non-negative proposal index")
        if not links_proposal and self.proposal_index is not None:
            raise ValueError(f"{self.outcome} cannot reference a proposal index")
        if self.outcome == "duplicate_of_proposal" and not (self.reason or "").strip():
            raise ValueError("duplicate_of_proposal requires a reason")


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    disposition: str
    proposals: tuple[ProposalDraft, ...] = ()
    candidate_outcomes: tuple[CandidateEvaluationOutcome, ...] = ()
    raw_response: str | None = None
    metadata: dict[str, object] | None = None
    validation_errors: tuple[str, ...] = ()


class EvaluatorExecutionError(RuntimeError):
    def __init__(self, message: str, outcome: EvaluationOutcome):
        super().__init__(message)
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class EmbeddingItem:
    key: str
    text: str


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vector_path: Path
    manifest_path: Path
    document_keys: tuple[str, ...]
    query_keys: tuple[str, ...]
    dimension: int
    dtype: str
    metadata: dict[str, object]


class AsrAdapter(Protocol):
    def transcribe(
        self,
        audio_path: Path,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        worker_started: Callable[[int], None] | None = None,
    ) -> TranscriptionResult: ...


class EmbeddingAdapter(Protocol):
    def embed(
        self,
        documents: tuple[EmbeddingItem, ...],
        queries: tuple[EmbeddingItem, ...],
        output_directory: Path,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        worker_started: Callable[[int], None] | None = None,
    ) -> EmbeddingResult: ...


class EvaluatorAdapter(Protocol):
    def evaluate(
        self,
        envelope: dict[str, object],
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        worker_started: Callable[[int], None] | None = None,
    ) -> EvaluationOutcome: ...

    def close(self) -> None: ...

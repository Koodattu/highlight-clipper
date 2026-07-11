from __future__ import annotations

from collections.abc import Callable, Sequence

from ..domain import (
    ProposalCategory,
    ProposalDraft,
    ProposalJudgments,
    ProposalStructure,
    canonical_json,
)
from ..ports import (
    CandidateEvaluationOutcome,
    EvaluationOutcome,
    TranscriptionResult,
    TranscriptSegment,
)
from ..timebase import SourceInterval


class FakeAsrAdapter:
    """Deterministic CPU-only ASR seam for workflow and recovery tests."""

    def __init__(self, segments: Sequence[TranscriptSegment] | None = None):
        self._segments = tuple(segments or ())

    def transcribe(
        self,
        audio_path,
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        worker_started: Callable[[int], None] | None = None,
    ) -> TranscriptionResult:
        if cancellation_requested and cancellation_requested():
            raise RuntimeError("Fake ASR was cancelled")
        return TranscriptionResult(
            segments=self._segments,
            raw={"adapter": "fake", "segments": len(self._segments)},
            metadata={"profile": "fake-v1"},
        )


class FakeEvaluatorAdapter:
    """Deterministic semantic evaluator that still obeys the real anchor contract."""

    def evaluate(
        self,
        envelope: dict[str, object],
        *,
        cancellation_requested: Callable[[], bool] | None = None,
        worker_started: Callable[[int], None] | None = None,
    ) -> EvaluationOutcome:
        if cancellation_requested and cancellation_requested():
            raise RuntimeError("Fake evaluator was cancelled")
        anchors = sorted(envelope["anchors"], key=lambda item: (int(item["source_time_us"]), str(item["id"])))
        candidates = list(envelope["candidates"])
        evidence = list(envelope["evidence"])
        source_end_us = int(envelope["source_end_us"])
        if not anchors or not candidates or not evidence:
            return EvaluationOutcome(
                disposition="insufficient_context",
                candidate_outcomes=tuple(
                    CandidateEvaluationOutcome(str(item["id"]), "insufficient_context") for item in candidates
                ),
            )
        drafts: list[ProposalDraft] = []
        used_intervals: list[SourceInterval] = []
        for candidate in candidates[:3]:
            event_us = int(candidate["anchor_us"])
            before = [item for item in anchors if int(item["source_time_us"]) <= event_us]
            after = [item for item in anchors if int(item["source_time_us"]) > event_us]
            desired_start = max(0, event_us - 15_000_000)
            desired_end = min(source_end_us, event_us + 45_000_000)
            start = min(
                before or anchors,
                key=lambda item: (abs(int(item["source_time_us"]) - desired_start), item["id"]),
            )
            end = min(
                after or anchors,
                key=lambda item: (abs(int(item["source_time_us"]) - desired_end), item["id"]),
            )
            start_us = int(start["source_time_us"])
            end_us = int(end["source_time_us"])
            if end_us <= start_us:
                start_us = max(0, min(event_us, source_end_us - 1))
                end_us = source_end_us
            interval = SourceInterval(start_us, end_us)
            if any(
                interval.intersection_us(existing) * 2 >= min(interval.duration_us, existing.duration_us)
                for existing in used_intervals
            ):
                continue
            used_intervals.append(interval)
            relevant_evidence = tuple(
                str(item["id"])
                for item in evidence
                if int(item["start_us"]) < interval.end_us and int(item["end_us"]) > interval.start_us
            )
            category_value = candidate.get("category_hint") or ProposalCategory.REACTION.value
            category = ProposalCategory(str(category_value))
            summary = next(
                (str(item["content"]).strip() for item in evidence if item["id"] in relevant_evidence),
                "Candidate moment",
            )
            duration_exception = None
            if interval.duration_us < 15_000_000:
                duration_exception = "Source or anchor set is shorter than the preferred duration"
            drafts.append(
                ProposalDraft(
                    interval=interval,
                    category=category,
                    summary=summary[:240],
                    structure=ProposalStructure(event_us=event_us),
                    judgments=ProposalJudgments(3, 3, 2, 3, 3, 3, 3),
                    evidence_ids=relevant_evidence,
                    candidate_ids=(str(candidate["id"]),),
                    duration_exception_reason=duration_exception,
                ).validate(source_end_us)
            )
        candidate_outcomes: list[CandidateEvaluationOutcome] = []
        for candidate in candidates:
            proposal_index = next(
                (index for index, draft in enumerate(drafts) if str(candidate["id"]) in draft.candidate_ids),
                None,
            )
            candidate_outcomes.append(
                CandidateEvaluationOutcome(
                    candidate_id=str(candidate["id"]),
                    outcome="covered_by_proposal" if proposal_index is not None else "too_weak",
                    proposal_index=proposal_index,
                    reason=None if proposal_index is not None else "No distinct proposal",
                )
            )
        return EvaluationOutcome(
            disposition="proposal_set" if drafts else "semantic_rejection",
            proposals=tuple(drafts),
            candidate_outcomes=tuple(candidate_outcomes),
            raw_response=canonical_json(
                {"adapter": "fake", "disposition": "proposal_set" if drafts else "semantic_rejection"}
            ),
            metadata={"prompt_tokens": 0, "final_tokens": 0},
        )

    def close(self) -> None:
        return None

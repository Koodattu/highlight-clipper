from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Any

from .timebase import SourceInterval


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


class AttemptState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DecisionValue(StrEnum):
    ACCEPT = "accept"
    MAYBE = "maybe"
    REJECT = "reject"
    WITHDRAWN = "withdrawn"


class ProposalCategory(StrEnum):
    REACTION = "reaction"
    COMEDY = "comedy"
    STORY = "story"
    OPINION = "opinion"
    EXPLANATION = "explanation"


class RejectionReason(StrEnum):
    NO_PAYOFF = "no_payoff"
    TOO_MUCH_CONTEXT = "too_much_context"
    ROUTINE_CONTENT = "routine_content"
    REPETITION = "repetition"
    POOR_MEDIA_QUALITY = "poor_media_quality"
    POOR_CREATOR_FIT = "poor_creator_fit"
    PUBLICATION_RISK = "publication_risk"
    UNUSABLE_BOUNDARIES = "unusable_boundaries"


class RiskKind(StrEnum):
    PRIVACY = "privacy"
    HATEFUL_LANGUAGE = "hateful_language"
    COPYRIGHT = "copyright"
    PERSONAL_INFORMATION = "personal_information"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ProposalRisk:
    kind: RiskKind
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("Proposal Risk requires an inspectable reason")


@dataclass(frozen=True, slots=True)
class ProposalStructure:
    event_us: int
    setup_start_us: int | None = None
    hook_us: int | None = None
    payoff_us: int | None = None
    exit_us: int | None = None

    def validate(self, interval: SourceInterval) -> ProposalStructure:
        if not interval.contains_point(self.event_us):
            raise ValueError("Event must be inside the proposal interval")
        for label, point in (
            ("Setup start", self.setup_start_us),
            ("Hook", self.hook_us),
            ("Payoff", self.payoff_us),
        ):
            if point is not None and not interval.contains_point(point):
                raise ValueError(f"{label} must be inside the proposal interval")
        if self.setup_start_us is not None and self.setup_start_us > self.event_us:
            raise ValueError("Setup start must not follow Event")
        if self.hook_us is not None and self.hook_us > self.event_us:
            raise ValueError("Hook must not follow Event")
        if self.payoff_us is not None and self.payoff_us < self.event_us:
            raise ValueError("Payoff must not precede Event")
        if self.exit_us is not None:
            if not self.event_us <= self.exit_us <= interval.end_us:
                raise ValueError("Exit must follow Event and not exceed proposal end")
            if self.payoff_us is not None and self.exit_us < self.payoff_us:
                raise ValueError("Exit must not precede Payoff")
        return self


@dataclass(frozen=True, slots=True)
class ProposalJudgments:
    salience: int
    standalone_coherence: int
    hook_strength: int
    payoff_strength: int
    creator_fit: int
    short_form_suitability: int
    context_sufficiency: int

    def __post_init__(self) -> None:
        for value in self.as_dict().values():
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
                raise ValueError("Every proposal judgment must be an integer from 0 through 4")

    def as_dict(self) -> dict[str, int]:
        return {
            "salience": self.salience,
            "standalone_coherence": self.standalone_coherence,
            "hook_strength": self.hook_strength,
            "payoff_strength": self.payoff_strength,
            "creator_fit": self.creator_fit,
            "short_form_suitability": self.short_form_suitability,
            "context_sufficiency": self.context_sufficiency,
        }

    @property
    def baseline_score(self) -> int:
        return sum(self.as_dict().values())


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    interval: SourceInterval
    category: ProposalCategory
    summary: str
    structure: ProposalStructure
    judgments: ProposalJudgments
    evidence_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    risks: tuple[ProposalRisk, ...] = field(default_factory=tuple)
    reasons_against_selection: tuple[str, ...] = field(default_factory=tuple)
    duration_exception_reason: str | None = None

    def validate(self, source_end_us: int) -> ProposalDraft:
        self.interval.validate_within(source_end_us)
        self.structure.validate(self.interval)
        if self.interval.duration_us > 240_000_000:
            raise ValueError("Machine proposals may not exceed 240 seconds")
        if not self.summary.strip():
            raise ValueError("Proposal summary is required")
        if not self.evidence_ids or not self.candidate_ids:
            raise ValueError("Proposal provenance is required")
        if any(not reason.strip() for reason in self.reasons_against_selection):
            raise ValueError("Reasons against selection must be inspectable text")
        return self

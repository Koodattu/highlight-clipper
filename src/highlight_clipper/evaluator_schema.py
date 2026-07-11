from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import RiskKind


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JudgmentScores(StrictModel):
    salience: int = Field(ge=0, le=4)
    standalone_coherence: int = Field(ge=0, le=4)
    hook_strength: int = Field(ge=0, le=4)
    payoff_strength: int = Field(ge=0, le=4)
    creator_fit: int = Field(ge=0, le=4)
    short_form_suitability: int = Field(ge=0, le=4)
    context_sufficiency: int = Field(ge=0, le=4)


class RiskOutput(StrictModel):
    kind: RiskKind
    reason: str = Field(min_length=1, max_length=500)


class AnchoredProposalOutput(StrictModel):
    category: Literal["reaction", "comedy", "story", "opinion", "explanation"]
    summary: str = Field(min_length=1, max_length=500)
    start_anchor_id: str
    end_anchor_id: str
    event_anchor_id: str
    setup_start_anchor_id: str | None = None
    hook_anchor_id: str | None = None
    payoff_anchor_id: str | None = None
    exit_anchor_id: str | None = None
    judgments: JudgmentScores
    risks: list[RiskOutput] = Field(default_factory=list)
    reasons_against_selection: list[str] = Field(default_factory=list, max_length=10)
    evidence_ids: list[str] = Field(min_length=1)
    duration_exception_reason: str | None = Field(default=None, max_length=500)


class CoveredCandidateOutcome(StrictModel):
    candidate_id: str
    outcome: Literal["covered_by_proposal"]
    proposal_index: int = Field(ge=0, le=2)
    reason: str | None = Field(default=None, max_length=500)


class DuplicateCandidateOutcome(StrictModel):
    candidate_id: str
    outcome: Literal["duplicate_of_proposal"]
    proposal_index: int = Field(ge=0, le=2)
    reason: str = Field(min_length=1, max_length=500)


class UnselectedCandidateOutcome(StrictModel):
    candidate_id: str
    outcome: Literal["too_weak", "insufficient_context", "omitted_by_proposal_cap"]
    reason: str = Field(min_length=1, max_length=500)


CandidateOutcomeOutput = Annotated[
    CoveredCandidateOutcome | DuplicateCandidateOutcome | UnselectedCandidateOutcome,
    Field(discriminator="outcome"),
]


class EvaluationResponse(StrictModel):
    disposition: Literal[
        "proposal_set",
        "semantic_rejection",
        "insufficient_context",
    ]
    proposals: list[AnchoredProposalOutput] = Field(default_factory=list, max_length=3)
    candidate_outcomes: list[CandidateOutcomeOutput]
    disposition_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def disposition_matches_proposals(self):
        if self.disposition == "proposal_set" and not 1 <= len(self.proposals) <= 3:
            raise ValueError("proposal_set requires one to three proposals")
        if self.disposition != "proposal_set" and self.proposals:
            raise ValueError("Non-proposal dispositions must not include proposals")
        return self

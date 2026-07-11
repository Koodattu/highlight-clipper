from __future__ import annotations

from dataclasses import dataclass

from ..database import Database, utc_now
from ..domain import (
    DecisionValue,
    ProposalCategory,
    RejectionReason,
    canonical_json,
    fingerprint,
    new_id,
)
from ..timebase import SourceInterval


@dataclass(frozen=True, slots=True)
class DecisionResult:
    decision_id: str
    revision_number: int
    decision: DecisionValue


def save_creator_profile(
    database: Database,
    *,
    languages: list[str],
    category_priorities: dict[str, int],
    desired_content: str,
    avoided_content: str,
    preferred_durations: dict[str, list[int]],
) -> str:
    if not languages or not set(languages) <= {"fi", "en"}:
        raise ValueError("Creator Profile languages must contain Finnish and/or English")
    categories = {category.value for category in ProposalCategory}
    if set(category_priorities) != categories:
        raise ValueError("Creator Profile must prioritize every proposal category")
    if any(not isinstance(value, int) or not 0 <= value <= 4 for value in category_priorities.values()):
        raise ValueError("Creator Profile category priorities must be integers from 0 through 4")
    if set(preferred_durations) != categories:
        raise ValueError("Creator Profile must define a preferred duration for every proposal category")
    for category, duration in preferred_durations.items():
        if (
            not isinstance(duration, list)
            or len(duration) != 2
            or any(not isinstance(value, int) for value in duration)
            or not 1 <= duration[0] < duration[1] <= 240
        ):
            raise ValueError(f"Creator Profile duration for {category} must be 1-240 seconds with start < end")
    with database.transaction(immediate=True) as connection:
        revision = int(
            connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 AS next FROM creator_profile_revisions"
            ).fetchone()["next"]
        )
        profile_id = new_id("profile")
        connection.execute(
            "INSERT INTO creator_profile_revisions "
            "(id, revision_number, languages_json, category_priorities_json, desired_content, "
            "avoided_content, preferred_durations_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                profile_id,
                revision,
                canonical_json(languages),
                canonical_json(category_priorities),
                desired_content.strip(),
                avoided_content.strip(),
                canonical_json(preferred_durations),
                utc_now(),
            ),
        )
        return profile_id


def record_decision(
    database: Database,
    proposal_id: str,
    decision: DecisionValue,
    *,
    idempotency_key: str,
    expected_prior_revision: int,
    rejection_reason: RejectionReason | None = None,
    note: str = "",
    boundary: SourceInterval | None = None,
) -> DecisionResult:
    if decision is DecisionValue.REJECT and rejection_reason is None:
        raise ValueError("A rejection requires a structured Rejection Reason")
    if decision is not DecisionValue.REJECT and rejection_reason is not None:
        raise ValueError("Rejection Reason is valid only for a rejection")
    if decision is DecisionValue.WITHDRAWN and boundary is not None:
        raise ValueError("A withdrawn decision cannot create a Boundary Edit")
    proposal = database.fetch_one(
        "SELECT p.*, e.start_us AS envelope_start_us, e.end_us AS envelope_end_us, "
        "s.source_end_us FROM clip_proposals p "
        "JOIN context_envelopes e ON e.id = p.context_envelope_id "
        "JOIN analysis_runs r ON r.id = p.analysis_run_id "
        "JOIN source_recordings s ON s.id = r.source_recording_id WHERE p.id = ?",
        (proposal_id,),
    )
    if proposal is None:
        raise KeyError(f"Unknown Clip Proposal: {proposal_id}")
    if boundary is not None:
        boundary.validate_within(int(proposal["source_end_us"]))
        if boundary.start_us == int(proposal["start_us"]) and boundary.end_us == int(proposal["end_us"]):
            boundary = None
    normalized_note = note.strip()
    request_fingerprint = fingerprint(
        {
            "proposal_id": proposal_id,
            "decision": decision.value,
            "expected_prior_revision": expected_prior_revision,
            "rejection_reason": rejection_reason.value if rejection_reason else None,
            "note": normalized_note,
            "boundary": ({"start_us": boundary.start_us, "end_us": boundary.end_us} if boundary else None),
        }
    )
    with database.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT d.*, b.start_us AS boundary_start_us, b.end_us AS boundary_end_us "
            "FROM editorial_decisions d LEFT JOIN boundary_edits b ON b.editorial_decision_id = d.id "
            "WHERE d.idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            existing_boundary = (
                {
                    "start_us": int(existing["boundary_start_us"]),
                    "end_us": int(existing["boundary_end_us"]),
                }
                if existing["boundary_start_us"] is not None
                else None
            )
            legacy_matches = (
                existing["request_fingerprint"] is None
                and existing["clip_proposal_id"] == proposal_id
                and existing["decision"] == decision.value
                and int(existing["expected_prior_revision"]) == expected_prior_revision
                and existing["rejection_reason"] == (rejection_reason.value if rejection_reason else None)
                and existing["note"] == normalized_note
                and existing_boundary
                == ({"start_us": boundary.start_us, "end_us": boundary.end_us} if boundary else None)
            )
            if existing["request_fingerprint"] != request_fingerprint and not legacy_matches:
                raise ValueError("Idempotency key was already used for a different decision")
            if legacy_matches:
                connection.execute(
                    "UPDATE editorial_decisions SET request_fingerprint = ? WHERE id = ?",
                    (request_fingerprint, existing["id"]),
                )
            return DecisionResult(existing["id"], int(existing["revision_number"]), decision)
        current_revision = int(
            connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) AS revision FROM editorial_decisions "
                "WHERE clip_proposal_id = ?",
                (proposal_id,),
            ).fetchone()["revision"]
        )
        if current_revision != expected_prior_revision:
            raise RuntimeError(
                f"Decision changed concurrently; expected revision {expected_prior_revision}, "
                f"current revision is {current_revision}"
            )
        decision_id = new_id("decision")
        revision = current_revision + 1
        connection.execute(
            "INSERT INTO editorial_decisions "
            "(id, clip_proposal_id, revision_number, decision, rejection_reason, note, "
            "idempotency_key, expected_prior_revision, request_fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                proposal_id,
                revision,
                decision.value,
                rejection_reason.value if rejection_reason else None,
                normalized_note,
                idempotency_key,
                expected_prior_revision,
                request_fingerprint,
                utc_now(),
            ),
        )
        if boundary is not None:
            outside = boundary.start_us < int(proposal["envelope_start_us"]) or boundary.end_us > int(
                proposal["envelope_end_us"]
            )
            connection.execute(
                "INSERT INTO boundary_edits "
                "(id, editorial_decision_id, start_us, end_us, outside_evaluated_context, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    new_id("boundary"),
                    decision_id,
                    boundary.start_us,
                    boundary.end_us,
                    int(outside),
                    utc_now(),
                ),
            )
        return DecisionResult(decision_id, revision, decision)


def withdraw_decision(
    database: Database, proposal_id: str, *, idempotency_key: str, expected_prior_revision: int
) -> DecisionResult:
    return record_decision(
        database,
        proposal_id,
        DecisionValue.WITHDRAWN,
        idempotency_key=idempotency_key,
        expected_prior_revision=expected_prior_revision,
    )

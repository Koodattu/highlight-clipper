from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path

from ..artifacts import ArtifactStore, sha256_file
from ..database import Database
from ..domain import fingerprint

MATCHING_POLICY_VERSION = "event-max-cardinality-rank-overlap-v2"
REVIEW_TIMER_POLICY_VERSION = "visible-active-idle60-heartbeat5-v1"


@dataclass(frozen=True, slots=True)
class ReferenceMoment:
    id: str
    annotation_set_id: str
    category: str
    language_slice: str
    start_us: int
    end_us: int
    event_us: int


@dataclass(frozen=True, slots=True)
class ProposalMoment:
    id: str
    rank: int
    category: str
    start_us: int
    end_us: int
    event_us: int


@dataclass(frozen=True, slots=True)
class MatchedPair:
    proposal: ProposalMoment
    reference: ReferenceMoment
    overlap_us: int


@dataclass(frozen=True, slots=True)
class EvaluationReportResult:
    path: Path
    sha256: str
    report: dict[str, object]


@dataclass(slots=True)
class _FlowEdge:
    to: int
    reverse: int
    capacity: int
    cost: int


def _add_edge(graph: list[list[_FlowEdge]], source: int, target: int, capacity: int, cost: int) -> _FlowEdge:
    forward = _FlowEdge(target, len(graph[target]), capacity, cost)
    reverse = _FlowEdge(source, len(graph[source]), 0, -cost)
    graph[source].append(forward)
    graph[target].append(reverse)
    return forward


def match_proposals(
    proposals: list[ProposalMoment],
    references: list[ReferenceMoment],
    *,
    source_end_us: int,
) -> list[MatchedPair]:
    ordered_proposals = sorted(proposals, key=lambda item: (item.rank, item.id))
    ordered_references = sorted(references, key=lambda item: (item.start_us, item.id))
    valid_pairs = [
        (proposal_index, reference_index)
        for proposal_index, proposal in enumerate(ordered_proposals)
        for reference_index, reference in enumerate(ordered_references)
        if reference.start_us <= proposal.event_us < reference.end_us
    ]
    if not valid_pairs:
        return []
    maximum_matches = min(len(ordered_proposals), len(ordered_references))
    tie_total_max = maximum_matches * (len(valid_pairs) + 1)
    overlap_scale = tie_total_max + 1
    rank_scale = maximum_matches * max(1, source_end_us) * overlap_scale + tie_total_max + 1
    tie_order = {
        pair: index
        for index, pair in enumerate(
            sorted(
                valid_pairs,
                key=lambda pair: (
                    ordered_references[pair[1]].start_us,
                    ordered_references[pair[1]].id,
                    ordered_proposals[pair[0]].id,
                ),
            )
        )
    }

    source_node = 0
    proposal_offset = 1
    reference_offset = proposal_offset + len(ordered_proposals)
    sink_node = reference_offset + len(ordered_references)
    graph: list[list[_FlowEdge]] = [[] for _ in range(sink_node + 1)]
    for proposal_index in range(len(ordered_proposals)):
        _add_edge(graph, source_node, proposal_offset + proposal_index, 1, 0)
    for reference_index in range(len(ordered_references)):
        _add_edge(graph, reference_offset + reference_index, sink_node, 1, 0)
    match_edges: list[tuple[int, int, _FlowEdge, int]] = []
    for proposal_index, reference_index in valid_pairs:
        proposal = ordered_proposals[proposal_index]
        reference = ordered_references[reference_index]
        overlap = max(0, min(proposal.end_us, reference.end_us) - max(proposal.start_us, reference.start_us))
        cost = proposal.rank * rank_scale - overlap * overlap_scale + tie_order[(proposal_index, reference_index)]
        edge = _add_edge(
            graph,
            proposal_offset + proposal_index,
            reference_offset + reference_index,
            1,
            cost,
        )
        match_edges.append((proposal_index, reference_index, edge, overlap))

    while True:
        distance: list[int | None] = [None] * len(graph)
        predecessor: list[tuple[int, int] | None] = [None] * len(graph)
        distance[source_node] = 0
        for _ in range(len(graph) - 1):
            changed = False
            for node, edges in enumerate(graph):
                if distance[node] is None:
                    continue
                for edge_index, edge in enumerate(edges):
                    if edge.capacity <= 0:
                        continue
                    candidate = distance[node] + edge.cost
                    if distance[edge.to] is None or candidate < distance[edge.to]:
                        distance[edge.to] = candidate
                        predecessor[edge.to] = (node, edge_index)
                        changed = True
            if not changed:
                break
        if predecessor[sink_node] is None:
            break
        node = sink_node
        while node != source_node:
            prior_node, edge_index = predecessor[node]
            edge = graph[prior_node][edge_index]
            edge.capacity -= 1
            graph[node][edge.reverse].capacity += 1
            node = prior_node

    matches = [
        MatchedPair(ordered_proposals[proposal_index], ordered_references[reference_index], overlap)
        for proposal_index, reference_index, edge, overlap in match_edges
        if edge.capacity == 0
    ]
    return sorted(matches, key=lambda item: (item.proposal.rank, item.reference.start_us, item.reference.id))


def _percentile_nearest_rank(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _match_metrics(
    proposals: list[ProposalMoment],
    references: list[ReferenceMoment],
    *,
    source_end_us: int,
) -> tuple[dict[str, object], list[MatchedPair]]:
    matches = match_proposals(proposals, references, source_end_us=source_end_us)
    return (
        {
            "reference_count": len(references),
            "returned_proposal_count": len(proposals),
            "matched_count": len(matches),
            "recall": len(matches) / len(references) if references else None,
        },
        matches,
    )


def _latest_reference_rows(connection, source_id: str) -> list:
    rows = connection.execute(
        "SELECT * FROM reference_moment_revisions WHERE source_recording_id = ? "
        "ORDER BY annotation_set_id, revision_number DESC",
        (source_id,),
    ).fetchall()
    latest: dict[str, object] = {}
    for row in rows:
        latest.setdefault(str(row["annotation_set_id"]), row)
    return list(latest.values())


def evaluate_analysis(database: Database, analysis_run_id: str) -> EvaluationReportResult:
    if database.fetch_one(
        "SELECT 1 FROM boundary_reanalysis_targets WHERE analysis_run_id = ?",
        (analysis_run_id,),
    ) is not None:
        raise ValueError("Human-seeded boundary reanalysis is excluded from blind retrieval evaluation")
    # Evaluation inputs are captured from one SQLite read snapshot. Review heartbeats,
    # decisions, or reference edits arriving concurrently belong to the next report.
    with database.transaction() as connection:
        run = connection.execute(
            "SELECT r.*, s.source_end_us, s.sha256 AS source_sha256, q.id AS queue_snapshot_id, "
            "q.created_at AS queue_created_at FROM analysis_runs r "
            "JOIN source_recordings s ON s.id = r.source_recording_id "
            "JOIN queue_snapshots q ON q.analysis_run_id = r.id "
            "WHERE r.id = ? AND r.state = 'succeeded'",
            (analysis_run_id,),
        ).fetchone()
        if run is None:
            raise ValueError("Evaluation requires a succeeded Analysis Run with a Queue Snapshot")
        run = dict(run)
        request_more_parent = None
        candidate_run_ids = [analysis_run_id]
        if run["requested_more_from_run_id"] is not None:
            parent = connection.execute(
                "SELECT id, input_fingerprint, configuration_fingerprint, queue_snapshot_id "
                "FROM analysis_runs WHERE id = ? AND state = 'succeeded'",
                (run["requested_more_from_run_id"],),
            ).fetchone()
            if parent is None:
                raise ValueError("Request More evaluation requires its succeeded parent Analysis Run")
            request_more_parent = dict(parent)
            candidate_run_ids.append(str(parent["id"]))
        latest_references = _latest_reference_rows(connection, str(run["source_recording_id"]))
        if not latest_references:
            raise ValueError("Evaluation requires Reference Moments for this Source Recording")
        if any(not int(row["frozen"]) for row in latest_references):
            raise ValueError("Freeze every current Reference Moment revision before evaluation")
        proposal_rows = connection.execute(
            "SELECT p.id, e.rank, p.category, p.start_us, p.end_us, p.event_us "
            "FROM queue_entries e JOIN clip_proposals p ON p.id = e.clip_proposal_id "
            "WHERE e.queue_snapshot_id = ? ORDER BY e.rank, p.id",
            (run["queue_snapshot_id"],),
        ).fetchall()
        latest_decisions = connection.execute(
            "SELECT d.clip_proposal_id, d.id, d.revision_number, d.decision, "
            "b.id AS boundary_edit_id FROM queue_entries q "
            "JOIN editorial_decisions d ON d.clip_proposal_id = q.clip_proposal_id "
            "LEFT JOIN boundary_edits b ON b.editorial_decision_id = d.id "
            "WHERE q.queue_snapshot_id = ? AND d.revision_number = "
            "(SELECT MAX(d2.revision_number) FROM editorial_decisions d2 "
            "WHERE d2.clip_proposal_id = d.clip_proposal_id) "
            "ORDER BY d.clip_proposal_id",
            (run["queue_snapshot_id"],),
        ).fetchall()
        activity_rows = connection.execute(
            "SELECT id, clip_proposal_id, active_milliseconds, activity_kind, created_at, "
            "session_id, sequence_number FROM review_activity_events "
            "WHERE queue_snapshot_id = ? ORDER BY created_at, session_id, sequence_number, id",
            (run["queue_snapshot_id"],),
        ).fetchall()
        placeholders = ", ".join("?" for _ in candidate_run_ids)
        candidate_rows = connection.execute(
            "SELECT analysis_run_id, id, anchor_us FROM candidate_moments "
            f"WHERE analysis_run_id IN ({placeholders}) ORDER BY anchor_us, analysis_run_id, id",
            tuple(candidate_run_ids),
        ).fetchall()

    references = [
        ReferenceMoment(
            id=str(row["id"]),
            annotation_set_id=str(row["annotation_set_id"]),
            category=str(row["category"]),
            language_slice=str(row["language_slice"]),
            start_us=int(row["start_us"]),
            end_us=int(row["end_us"]),
            event_us=int(row["event_us"]),
        )
        for row in latest_references
        if row["certainty"] == "definite"
    ]
    if not references:
        raise ValueError("Evaluation requires at least one frozen definite Reference Moment")
    proposals = [
        ProposalMoment(
            id=str(row["id"]),
            rank=int(row["rank"]),
            category=str(row["category"]),
            start_us=int(row["start_us"]),
            end_us=int(row["end_us"]),
            event_us=int(row["event_us"]),
        )
        for row in proposal_rows
    ]
    source_end_us = int(run["source_end_us"])
    recalls: dict[str, object] = {}
    matches_at_30: list[MatchedPair] = []
    for cutoff in (10, 20, 30):
        metrics, matches = _match_metrics(
            [proposal for proposal in proposals if proposal.rank <= cutoff],
            references,
            source_end_us=source_end_us,
        )
        recalls[f"recall_at_{cutoff}"] = metrics
        if cutoff == 30:
            matches_at_30 = matches

    start_errors = [abs(pair.proposal.start_us - pair.reference.start_us) / 1_000_000 for pair in matches_at_30]
    end_errors = [abs(pair.proposal.end_us - pair.reference.end_us) / 1_000_000 for pair in matches_at_30]
    tiou = [
        pair.overlap_us
        / (
            max(pair.proposal.end_us, pair.reference.end_us)
            - min(pair.proposal.start_us, pair.reference.start_us)
        )
        for pair in matches_at_30
    ]
    boundary_metrics = {
        "matched_count": len(matches_at_30),
        "start_absolute_error_seconds": {
            "median": statistics.median(start_errors) if start_errors else None,
            "p90_nearest_rank": _percentile_nearest_rank(start_errors, 0.9),
        },
        "end_absolute_error_seconds": {
            "median": statistics.median(end_errors) if end_errors else None,
            "p90_nearest_rank": _percentile_nearest_rank(end_errors, 0.9),
        },
        "temporal_iou": {
            "median": statistics.median(tiou) if tiou else None,
            "values": tiou,
        },
    }

    def slice_metrics(attribute: str) -> dict[str, object]:
        values = sorted({str(getattr(reference, attribute)) for reference in references})
        result: dict[str, object] = {}
        proposals_at_30 = [proposal for proposal in proposals if proposal.rank <= 30]
        for value in values:
            selected = [reference for reference in references if str(getattr(reference, attribute)) == value]
            result[value] = _match_metrics(
                proposals_at_30,
                selected,
                source_end_us=source_end_us,
            )[0]
        return result

    accepted = [row for row in latest_decisions if row["decision"] == "accept"]
    maybe = [row for row in latest_decisions if row["decision"] == "maybe"]
    proposal_count = len(proposals)
    active_milliseconds = sum(int(row["active_milliseconds"]) for row in activity_rows)
    active_minutes = active_milliseconds / 60_000
    source_hours = source_end_us / 3_600_000_000
    recall_by_review_minutes: dict[str, object] = {}
    for minutes in (10, 20, 30):
        threshold_ms = minutes * 60_000
        cumulative_ms = 0
        reviewed_ids: set[str] = set()
        for row in activity_rows:
            if cumulative_ms >= threshold_ms:
                break
            reviewed_ids.add(str(row["clip_proposal_id"]))
            cumulative_ms += int(row["active_milliseconds"])
        metrics, _ = _match_metrics(
            [proposal for proposal in proposals if proposal.id in reviewed_ids],
            references,
            source_end_us=source_end_us,
        )
        recall_by_review_minutes[f"recall_after_{minutes}_minutes"] = {
            **metrics,
            "observed_active_milliseconds": min(cumulative_ms, threshold_ms),
            "threshold_reached": active_milliseconds >= threshold_ms,
        }
    product_metrics = {
        "acceptance_rate": len(accepted) / proposal_count if proposal_count else None,
        "maybe_rate": len(maybe) / proposal_count if proposal_count else None,
        "accepted_without_boundary_edit_rate": (
            sum(row["boundary_edit_id"] is None for row in accepted) / len(accepted) if accepted else None
        ),
        "accepted_clip_count": len(accepted),
        "queue_category_count": len({proposal.category for proposal in proposals}),
        "active_review_time": {
            "available": bool(activity_rows),
            "active_milliseconds": active_milliseconds,
            "review_minutes_per_source_hour": active_minutes / source_hours if source_hours else None,
            "review_minutes_per_accepted_clip": active_minutes / len(accepted) if accepted else None,
            "policy_version": REVIEW_TIMER_POLICY_VERSION,
            "policy": "visible review tab; playback or interaction; idle after 60 seconds; 5-second heartbeat",
        },
        "recall_by_review_minutes": recall_by_review_minutes,
    }
    discovered = sum(
        any(reference.start_us <= int(candidate["anchor_us"]) < reference.end_us for candidate in candidate_rows)
        for reference in references
    )
    report_input = {
        "analysis_run_id": analysis_run_id,
        "source_sha256": run["source_sha256"],
        "source_end_us": source_end_us,
        "analysis_input_fingerprint": run["input_fingerprint"],
        "analysis_configuration_fingerprint": run["configuration_fingerprint"],
        "queue_snapshot_id": run["queue_snapshot_id"],
        "request_more_parent": request_more_parent,
        "latest_reference_revisions": [dict(row) for row in latest_references],
        "references": [asdict(reference) for reference in references],
        "proposals": [asdict(proposal) for proposal in proposals],
        "candidate_anchors": [dict(row) for row in candidate_rows],
        "latest_queue_decisions": [dict(row) for row in latest_decisions],
        "review_activity_events": [dict(row) for row in activity_rows],
        "matching_policy_version": MATCHING_POLICY_VERSION,
        "review_timer_policy_version": REVIEW_TIMER_POLICY_VERSION,
    }
    report_fingerprint = fingerprint(report_input)
    report: dict[str, object] = {
        "schema_version": 2,
        "report_fingerprint": report_fingerprint,
        "matching_policy_version": MATCHING_POLICY_VERSION,
        "review_timer_policy_version": REVIEW_TIMER_POLICY_VERSION,
        "analysis": {
            "analysis_run_id": analysis_run_id,
            "source_recording_id": run["source_recording_id"],
            "source_sha256": run["source_sha256"],
            "queue_snapshot_id": run["queue_snapshot_id"],
            "input_fingerprint": run["input_fingerprint"],
            "configuration_fingerprint": run["configuration_fingerprint"],
            "configuration": json.loads(str(run["configuration_json"])),
            "request_more_parent": request_more_parent,
        },
        "reference_summary": {
            "frozen_latest_count": len(latest_references),
            "definite_count": len(references),
            "possible_count": sum(row["certainty"] == "possible" for row in latest_references),
        },
        "discovery": {
            "definite_reference_count": len(references),
            "discovered_count": discovered,
            "recall": discovered / len(references),
        },
        "queue_recall": recalls,
        "boundary_metrics": boundary_metrics,
        "category_slices_at_30": slice_metrics("category"),
        "language_slices_at_30": slice_metrics("language_slice"),
        "product_metrics": product_metrics,
        "matches_at_30": [
            {
                "proposal_id": pair.proposal.id,
                "proposal_rank": pair.proposal.rank,
                "reference_id": pair.reference.id,
                "reference_annotation_set_id": pair.reference.annotation_set_id,
                "overlap_us": pair.overlap_us,
            }
            for pair in matches_at_30
        ],
        "unavailable_metrics": ["cross_recording_macro_recall", "ablation_comparison"]
        + ([] if activity_rows else ["review_time_metrics_without_recorded_activity"]),
    }
    destination = (
        database.settings.work_dir
        / "artifacts"
        / "evaluation-reports"
        / analysis_run_id
        / f"{report_fingerprint}.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise RuntimeError("Existing deterministic evaluation report differs from the current result")
    else:
        partial = destination.with_name(f"{destination.name}.partial")
        partial.write_bytes(encoded)
        partial.replace(destination)
    digest = sha256_file(destination)
    relative_path = database.settings.relative_to_workdir(destination)
    existing_artifact = database.fetch_one("SELECT * FROM artifacts WHERE relative_path = ?", (relative_path,))
    if existing_artifact is not None:
        ArtifactStore(database).require_intact(existing_artifact)
    else:
        with database.transaction(immediate=True) as connection:
            ArtifactStore(database).register(
                connection,
                path=destination,
                kind="evaluation_report",
                owner_type="analysis",
                owner_id=analysis_run_id,
                source_recording_id=str(run["source_recording_id"]),
                configuration={
                    "matching_policy_version": MATCHING_POLICY_VERSION,
                    "report_fingerprint": report_fingerprint,
                },
                require_hash=True,
                precomputed_sha256=digest,
                precomputed_size=len(encoded),
                regenerable=True,
                integrity={"validated": True, "sha256": digest},
            )
    return EvaluationReportResult(destination, digest, report)

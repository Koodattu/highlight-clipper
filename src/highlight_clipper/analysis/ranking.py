from __future__ import annotations

import json
import math

from .retrieval import tokens

QUEUE_SECTION_US = 15 * 60 * 1_000_000
TEMPORAL_DUPLICATE_THRESHOLD = 0.50
SEMANTIC_DUPLICATE_THRESHOLD = 0.80


def default_queue_size(source_end_us: int, *, hard_cap: int = 30) -> int:
    if source_end_us <= 0 or hard_cap <= 0:
        raise ValueError("Queue sizing requires a positive source duration and hard cap")
    source_hours = source_end_us / 3_600_000_000
    return min(hard_cap, max(10, math.ceil(3 * source_hours)))


def _temporal_overlap_ratio(left: dict[str, object], right: dict[str, object]) -> float:
    overlap = max(
        0,
        min(int(left["end_us"]), int(right["end_us"]))
        - max(int(left["start_us"]), int(right["start_us"])),
    )
    shorter = min(
        int(left["end_us"]) - int(left["start_us"]),
        int(right["end_us"]) - int(right["start_us"]),
    )
    return overlap / shorter if shorter > 0 else 0.0


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def rank_proposals(
    rows: list[dict[str, object]],
    limit: int = 30,
    *,
    category_priorities: dict[str, int] | None = None,
    pinned_rows: list[dict[str, object]] | None = None,
    temporal_duplicate_threshold: float = TEMPORAL_DUPLICATE_THRESHOLD,
    semantic_duplicate_threshold: float = SEMANTIC_DUPLICATE_THRESHOLD,
) -> list[dict[str, object]]:
    if limit <= 0:
        return []
    priorities = category_priorities or {}
    selected: list[dict[str, object]] = []
    for row in pinned_rows or []:
        semantic_text = str(row.get("semantic_text") or row.get("summary") or "")
        selected.append(
            {
                **row,
                "baseline_score": float(row["baseline_score"]),
                "_semantic_tokens": tokens(semantic_text),
                "_section": int(row["start_us"]) // QUEUE_SECTION_US,
            }
        )
    selected = selected[:limit]
    remaining: list[dict[str, object]] = []
    for row in rows:
        judgments = json.loads(str(row["judgments_json"]))
        score = float(sum(int(value) for value in judgments.values()) + 2 * priorities.get(str(row["category"]), 0))
        semantic_text = str(row.get("semantic_text") or row.get("summary") or "")
        remaining.append(
            {
                **row,
                "baseline_score": score,
                "_semantic_tokens": tokens(semantic_text),
                "_section": int(row["start_us"]) // QUEUE_SECTION_US,
            }
        )
    remaining.sort(
        key=lambda row: (
            -float(row["baseline_score"]),
            int(row["start_us"]),
            str(row["id"]),
        )
    )

    seen_categories = {str(row["category"]) for row in selected}
    seen_sections = {int(row["_section"]) for row in selected}
    while remaining and len(selected) < limit:
        eligible: list[tuple[dict[str, object], float, float]] = []
        for row in remaining:
            temporal_similarity = max(
                (_temporal_overlap_ratio(row, prior) for prior in selected),
                default=0.0,
            )
            semantic_similarity = max(
                (
                    _jaccard(
                        row["_semantic_tokens"],
                        prior["_semantic_tokens"],
                    )
                    for prior in selected
                ),
                default=0.0,
            )
            if (
                temporal_similarity >= temporal_duplicate_threshold
                or semantic_similarity >= semantic_duplicate_threshold
            ):
                continue
            eligible.append((row, temporal_similarity, semantic_similarity))
        if not eligible:
            break
        row, temporal_similarity, semantic_similarity = min(
            eligible,
            key=lambda item: (
                -int(str(item[0]["category"]) not in seen_categories)
                - int(int(item[0]["_section"]) not in seen_sections),
                -int(str(item[0]["category"]) not in seen_categories),
                -float(item[0]["baseline_score"]),
                int(item[0]["start_us"]),
                str(item[0]["id"]),
            ),
        )
        remaining.remove(row)
        category = str(row["category"])
        section = int(row["_section"])
        row["diversity"] = {
            "version": "category-section-temporal-summary-v1",
            "new_category": category not in seen_categories,
            "new_section": section not in seen_sections,
            "section_index": section,
            "maximum_temporal_overlap_ratio": temporal_similarity,
            "maximum_summary_token_jaccard": semantic_similarity,
        }
        selected.append(row)
        seen_categories.add(category)
        seen_sections.add(section)

    return [
        {key: value for key, value in row.items() if key not in {"_semantic_tokens", "_section"}}
        for row in selected
    ]

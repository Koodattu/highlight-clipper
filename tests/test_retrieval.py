from __future__ import annotations

import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from highlight_clipper.analysis.ranking import default_queue_size, rank_proposals
from highlight_clipper.analysis.retrieval import (
    TranscriptWindow,
    audio_peak_candidates,
    embedding_candidates,
    speech_activity_candidates,
    transcript_window_key,
)
from highlight_clipper.workflows.analyze import (
    MAX_CONTEXT_ENVELOPE_US,
    SECTION_DURATION_US,
    _balanced_cluster_order,
    _context_envelope_bounds,
)


class EmbeddingRetrievalTests(unittest.TestCase):
    def test_synthetic_normalized_similarities_create_deterministic_category_candidates(self) -> None:
        first = TranscriptWindow(0, 20_000_000, "A funny misunderstanding and punchline", ("e1",))
        second = TranscriptWindow(30_000_000, 60_000_000, "A useful strategic explanation", ("e2",))
        windows = (first, second)
        document_keys = tuple(transcript_window_key(window) for window in windows)
        candidates = embedding_candidates(
            windows,
            document_keys,
            ("category:comedy", "category:explanation"),
            [[0.95, 0.10], [0.05, 0.99]],
            3_600_000_000,
        )
        comedy = next(candidate for candidate in candidates if candidate.category_hint.value == "comedy")
        explanation = next(candidate for candidate in candidates if candidate.category_hint.value == "explanation")
        self.assertEqual(comedy.evidence_ids, ("e1",))
        self.assertEqual(explanation.evidence_ids, ("e2",))
        self.assertEqual(comedy.generator_name, "multilingual-embedding-query")

    def test_envelope_order_spreads_its_prefix_across_a_long_recording(self) -> None:
        clusters = [
            [
                {
                    "id": f"candidate-{index}",
                    "anchor_us": index * SECTION_DURATION_US + 1_000_000,
                    "generator_name": "lexical-category",
                    "category_hint": "story",
                    "local_confidence": 1.0,
                }
            ]
            for index in range(8)
        ]
        ordered = _balanced_cluster_order(clusters)
        prefix_sections = [int(members[0]["anchor_us"]) // SECTION_DURATION_US for members in ordered[:4]]
        self.assertEqual(prefix_sections[:2], [0, 7])
        self.assertLessEqual(min(prefix_sections), 1)
        self.assertGreaterEqual(max(prefix_sections), 6)
        self.assertGreaterEqual(len(set(prefix_sections)), 4)

    def test_envelope_order_prefers_new_generator_and_category_within_a_section(self) -> None:
        clusters = [
            [
                {
                    "id": "lexical-story",
                    "anchor_us": 1_000_000,
                    "generator_name": "lexical-category",
                    "category_hint": "story",
                    "local_confidence": 0.9,
                }
            ],
            [
                {
                    "id": "embedding-comedy",
                    "anchor_us": 2_000_000,
                    "generator_name": "multilingual-embedding-query",
                    "category_hint": "comedy",
                    "local_confidence": 0.1,
                }
            ],
            [
                {
                    "id": "lexical-story-two",
                    "anchor_us": 3_000_000,
                    "generator_name": "lexical-category",
                    "category_hint": "story",
                    "local_confidence": 0.8,
                }
            ],
        ]
        ordered_ids = [members[0]["id"] for members in _balanced_cluster_order(clusters)]
        self.assertEqual(ordered_ids[:2], ["lexical-story", "embedding-comedy"])

    def test_long_candidate_context_is_capped_at_five_minutes(self) -> None:
        start_us, end_us = _context_envelope_bounds(
            10 * 60 * 1_000_000,
            13 * 60 * 1_000_000,
            60 * 60 * 1_000_000,
        )

        self.assertEqual(end_us - start_us, MAX_CONTEXT_ENVELOPE_US)
        self.assertLessEqual(start_us, 10 * 60 * 1_000_000)
        self.assertGreaterEqual(end_us, 13 * 60 * 1_000_000)

    def test_creator_category_priority_changes_the_transparent_baseline_order(self) -> None:
        judgments = json.dumps({"salience": 3, "standalone_coherence": 3})
        rows = [
            {"id": "story", "category": "story", "start_us": 0, "end_us": 10, "judgments_json": judgments},
            {
                "id": "reaction",
                "category": "reaction",
                "start_us": 20,
                "end_us": 30,
                "judgments_json": judgments,
            },
        ]
        ranked = rank_proposals(rows, 1, category_priorities={"story": 0, "reaction": 4})
        self.assertEqual(ranked[0]["id"], "reaction")

    def test_queue_size_scales_to_three_per_hour_between_ten_and_thirty(self) -> None:
        self.assertEqual(default_queue_size(3_600_000_000), 10)
        self.assertEqual(default_queue_size(4 * 3_600_000_000), 12)
        self.assertEqual(default_queue_size(10 * 3_600_000_000), 30)
        self.assertEqual(default_queue_size(20 * 3_600_000_000), 30)

    def test_ranking_prefix_preserves_category_and_recording_section_coverage(self) -> None:
        judgments = json.dumps({"salience": 3, "standalone_coherence": 3})
        rows = [
            {
                "id": "top-story",
                "category": "story",
                "start_us": 0,
                "end_us": 30_000_000,
                "summary": "first story",
                "judgments_json": judgments,
            },
            {
                "id": "same-section-story",
                "category": "story",
                "start_us": 60_000_000,
                "end_us": 90_000_000,
                "summary": "second story",
                "judgments_json": judgments,
            },
            {
                "id": "later-story",
                "category": "story",
                "start_us": 16 * 60 * 1_000_000,
                "end_us": 16 * 60 * 1_000_000 + 30_000_000,
                "summary": "later section",
                "judgments_json": judgments,
            },
            {
                "id": "comedy",
                "category": "comedy",
                "start_us": 120_000_000,
                "end_us": 150_000_000,
                "summary": "different joke",
                "judgments_json": judgments,
            },
        ]

        ranked = rank_proposals(rows, 3)

        self.assertEqual([row["id"] for row in ranked], ["top-story", "comedy", "later-story"])
        self.assertTrue(ranked[1]["diversity"]["new_category"])
        self.assertTrue(ranked[2]["diversity"]["new_section"])

    def test_ranking_does_not_readd_temporal_or_summary_duplicates(self) -> None:
        judgments = json.dumps({"salience": 3, "standalone_coherence": 3})
        rows = [
            {
                "id": "first",
                "category": "story",
                "start_us": 0,
                "end_us": 60_000_000,
                "summary": "the exact same memorable sentence",
                "judgments_json": judgments,
            },
            {
                "id": "temporal-duplicate",
                "category": "reaction",
                "start_us": 10_000_000,
                "end_us": 50_000_000,
                "summary": "different words",
                "judgments_json": judgments,
            },
            {
                "id": "summary-duplicate",
                "category": "comedy",
                "start_us": 20 * 60 * 1_000_000,
                "end_us": 20 * 60 * 1_000_000 + 60_000_000,
                "summary": "the exact same memorable sentence",
                "judgments_json": judgments,
            },
            {
                "id": "distinct",
                "category": "explanation",
                "start_us": 40 * 60 * 1_000_000,
                "end_us": 40 * 60 * 1_000_000 + 60_000_000,
                "summary": "a separate useful explanation",
                "judgments_json": judgments,
            },
        ]

        ranked = rank_proposals(rows, 4)

        self.assertEqual([row["id"] for row in ranked], ["first", "distinct"])

    def test_audio_peak_normalization_detects_a_change_without_favoring_an_entire_loud_section(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            audio_path = Path(temporary) / "nonstationary.wav"
            with wave.open(str(audio_path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(100)
                samples = [100] * 500 + [10_000] * 1_000
                audio.writeframes(b"".join(struct.pack("<h", value) for value in samples))
            candidates, observations = audio_peak_candidates(
                audio_path,
                15_000_000,
                lambda start, *_: f"evidence-{start}",
                normalization_window_seconds=5,
            )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].start_us, 5_000_000)
        self.assertGreater(observations[0]["energy_change_z"], observations[0]["rms_z"])

    def test_speech_activity_uses_pause_and_transcript_rate_as_independent_evidence(self) -> None:
        segments = [
            {"id": "s1", "start_us": 0, "end_us": 4_000_000},
            {"id": "s2", "start_us": 10_000_000, "end_us": 15_000_000},
        ]
        words = [
            {"id": f"w{index}", "start_us": 10_000_000 + index * 300_000, "end_us": 10_100_000 + index * 300_000}
            for index in range(10)
        ]
        captured: list[dict[str, object]] = []

        candidates, observations = speech_activity_candidates(
            segments,
            words,
            30_000_000,
            lambda item: captured.append(item) or f"speech-{item['start_us']}",
        )

        self.assertTrue(candidates)
        self.assertEqual(candidates[0].generator_name, "speech-activity-change")
        self.assertGreaterEqual(max(float(item["pause_before_seconds"]) for item in observations), 6.0)
        self.assertGreater(max(float(item["speech_rate_words_per_second"]) for item in captured), 1.0)


if __name__ == "__main__":
    unittest.main()

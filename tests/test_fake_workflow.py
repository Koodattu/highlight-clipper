from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from highlight_clipper.adapters.fake import FakeAsrAdapter, FakeEvaluatorAdapter
from highlight_clipper.adapters.ffmpeg import FFmpegAdapter
from highlight_clipper.database import Database
from highlight_clipper.domain import DecisionValue
from highlight_clipper.ports import (
    CandidateEvaluationOutcome,
    EvaluationOutcome,
    TranscriptionResult,
    TranscriptSegment,
    TranscriptWord,
)
from highlight_clipper.settings import Settings
from highlight_clipper.timebase import SourceInterval
from highlight_clipper.workers.supervisor import WorkerCancelled
from highlight_clipper.workflows.analyze import AnalysisCancelled, AnalysisConfig, AnalysisWorkflow
from highlight_clipper.workflows.evaluate import evaluate_analysis
from highlight_clipper.workflows.export import export_accepted_clip
from highlight_clipper.workflows.import_source import import_source
from highlight_clipper.workflows.review import record_decision, withdraw_decision


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class FakeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(root, root / "workdir")
        self.settings.ensure_work_directories()
        self.database = Database(self.settings)
        self.database.migrate()
        self.database.ensure_default_profile()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fixture(self) -> Path:
        path = Path(self.temporary.name) / "source with spaces.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=25:duration=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=4",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return path

    def test_import_analyze_review_export_and_withdraw(self) -> None:
        imported = import_source(self.database, self._fixture())
        self.assertGreater(imported.source_end_us, 3_900_000)
        asr = FakeAsrAdapter(
            [
                TranscriptSegment(
                    500_000,
                    3_500_000,
                    "Wow, this funny story matters because the ending works.",
                    "en",
                )
            ]
        )
        analysis = AnalysisWorkflow(self.database, asr, FakeEvaluatorAdapter()).run(imported.source_recording_id)
        self.assertGreaterEqual(analysis.proposal_count, 1)
        proposal = self.database.fetch_one(
            "SELECT p.* FROM queue_entries q JOIN clip_proposals p ON p.id = q.clip_proposal_id "
            "WHERE q.queue_snapshot_id = ? ORDER BY q.rank LIMIT 1",
            (analysis.queue_snapshot_id,),
        )
        decision_key = str(uuid.uuid4())
        decision = record_decision(
            self.database,
            proposal["id"],
            DecisionValue.ACCEPT,
            idempotency_key=decision_key,
            expected_prior_revision=0,
        )
        replayed_decision = record_decision(
            self.database,
            proposal["id"],
            DecisionValue.ACCEPT,
            idempotency_key=decision_key,
            expected_prior_revision=0,
        )
        self.assertEqual(replayed_decision.decision_id, decision.decision_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE editorial_decisions SET request_fingerprint = NULL WHERE id = ?",
                (decision.decision_id,),
            )
        legacy_replay = record_decision(
            self.database,
            proposal["id"],
            DecisionValue.ACCEPT,
            idempotency_key=decision_key,
            expected_prior_revision=0,
        )
        self.assertEqual(legacy_replay.decision_id, decision.decision_id)
        export_key = str(uuid.uuid4())
        exported = export_accepted_clip(
            self.database,
            proposal["id"],
            idempotency_key=export_key,
            confirmed=True,
            expected_decision_revision=decision.revision_number,
        )
        self.assertTrue(exported.path.is_file())
        self.assertEqual(len(exported.sha256), 64)
        replayed_export = export_accepted_clip(
            self.database,
            proposal["id"],
            idempotency_key=export_key,
            confirmed=True,
            expected_decision_revision=decision.revision_number,
        )
        self.assertEqual(replayed_export.export_id, exported.export_id)
        with self.database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM export_requests WHERE idempotency_key = ?", (export_key,))
            connection.execute(
                "UPDATE exports SET request_fingerprint = NULL WHERE id = ?",
                (exported.export_id,),
            )
        legacy_export = export_accepted_clip(
            self.database,
            proposal["id"],
            idempotency_key=export_key,
            confirmed=True,
            expected_decision_revision=decision.revision_number,
        )
        self.assertEqual(legacy_export.export_id, exported.export_id)
        refreshed_accept = record_decision(
            self.database,
            proposal["id"],
            DecisionValue.ACCEPT,
            idempotency_key=str(uuid.uuid4()),
            expected_prior_revision=decision.revision_number,
        )
        with self.assertRaisesRegex(RuntimeError, "Editorial Decision changed"):
            export_accepted_clip(
                self.database,
                proposal["id"],
                idempotency_key=str(uuid.uuid4()),
                confirmed=True,
                expected_decision_revision=decision.revision_number,
            )
        withdraw_decision(
            self.database,
            proposal["id"],
            idempotency_key=str(uuid.uuid4()),
            expected_prior_revision=refreshed_accept.revision_number,
        )
        with self.assertRaises(RuntimeError):
            export_accepted_clip(
                self.database,
                proposal["id"],
                idempotency_key=str(uuid.uuid4()),
                confirmed=True,
                expected_decision_revision=refreshed_accept.revision_number + 1,
            )
        self.database.integrity_check()

    def test_boundary_edits_record_only_real_corrections_and_never_withdrawals(self) -> None:
        imported = import_source(self.database, self._fixture())
        analysis = AnalysisWorkflow(
            self.database,
            FakeAsrAdapter(
                [
                    TranscriptSegment(
                        500_000,
                        3_500_000,
                        "Wow, this funny story matters because the ending works.",
                        "en",
                    )
                ]
            ),
            FakeEvaluatorAdapter(),
        ).run(imported.source_recording_id)
        proposal = self.database.fetch_one(
            "SELECT p.* FROM queue_entries q JOIN clip_proposals p ON p.id = q.clip_proposal_id "
            "WHERE q.queue_snapshot_id = ? ORDER BY q.rank LIMIT 1",
            (analysis.queue_snapshot_id,),
        )
        unchanged = SourceInterval(int(proposal["start_us"]), int(proposal["end_us"]))
        accepted = record_decision(
            self.database,
            proposal["id"],
            DecisionValue.ACCEPT,
            idempotency_key=str(uuid.uuid4()),
            expected_prior_revision=0,
            boundary=unchanged,
        )
        self.assertEqual(
            self.database.fetch_one("SELECT COUNT(*) AS count FROM boundary_edits")["count"],
            0,
        )
        with self.assertRaisesRegex(ValueError, "withdrawn decision"):
            record_decision(
                self.database,
                proposal["id"],
                DecisionValue.WITHDRAWN,
                idempotency_key=str(uuid.uuid4()),
                expected_prior_revision=accepted.revision_number,
                boundary=unchanged,
            )

    def test_analysis_fails_closed_when_registered_audio_bytes_change(self) -> None:
        imported = import_source(self.database, self._fixture())
        artifact = self.database.fetch_one(
            "SELECT * FROM artifacts WHERE source_recording_id = ? AND kind = 'analysis_audio'",
            (imported.source_recording_id,),
        )
        audio_path = self.settings.resolve_work_path(str(artifact["relative_path"]))
        with audio_path.open("r+b") as audio:
            audio.seek(max(44, audio_path.stat().st_size // 2))
            original = audio.read(1)
            audio.seek(-1, 1)
            audio.write(bytes([original[0] ^ 0xFF]))
        with self.assertRaisesRegex(RuntimeError, "integrity hash"):
            AnalysisWorkflow(
                self.database,
                FakeAsrAdapter([]),
                FakeEvaluatorAdapter(),
            ).run(imported.source_recording_id)
        self.assertEqual(
            self.database.fetch_one("SELECT COUNT(*) AS count FROM transcript_segments")["count"],
            0,
        )

    def test_frozen_reference_produces_a_deterministic_evaluation_report(self) -> None:
        imported = import_source(self.database, self._fixture())
        analysis = AnalysisWorkflow(
            self.database,
            FakeAsrAdapter(
                [
                    TranscriptSegment(
                        500_000,
                        3_500_000,
                        "Wow, this funny story matters because the ending works.",
                        "en",
                    )
                ]
            ),
            FakeEvaluatorAdapter(),
        ).run(imported.source_recording_id)
        proposal = self.database.fetch_one(
            "SELECT p.* FROM queue_entries q JOIN clip_proposals p ON p.id = q.clip_proposal_id "
            "WHERE q.queue_snapshot_id = ? ORDER BY q.rank LIMIT 1",
            (analysis.queue_snapshot_id,),
        )
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO reference_moment_revisions "
                "(id, source_recording_id, annotation_set_id, revision_number, certainty, category, "
                "start_us, end_us, event_us, short_form_suitability, rationale, frozen, language_slice, created_at) "
                "VALUES ('reference-test', ?, 'reference-set-test', 1, 'definite', ?, ?, ?, ?, 4, "
                "'fixture', 1, 'en', 'now')",
                (
                    imported.source_recording_id,
                    proposal["category"],
                    proposal["start_us"],
                    proposal["end_us"],
                    proposal["event_us"],
                ),
            )
        first = evaluate_analysis(self.database, analysis.analysis_run_id)
        second = evaluate_analysis(self.database, analysis.analysis_run_id)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.report["queue_recall"]["recall_at_30"]["recall"], 1.0)
        self.assertTrue(first.path.is_file())
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO review_activity_events "
                "(id, queue_snapshot_id, clip_proposal_id, session_id, sequence_number, "
                "active_milliseconds, activity_kind, created_at) "
                "VALUES ('review-activity-evaluation-test', ?, ?, 'session-evaluation-test', "
                "0, 1000, 'interaction', 'later')",
                (analysis.queue_snapshot_id, proposal["id"]),
            )
        after_review = evaluate_analysis(self.database, analysis.analysis_run_id)
        self.assertNotEqual(first.sha256, after_review.sha256)
        self.assertNotEqual(first.path, after_review.path)
        self.assertTrue(first.path.is_file())
        self.assertEqual(
            after_review.report["product_metrics"]["active_review_time"]["active_milliseconds"],
            1000,
        )

    def test_request_more_reuses_analysis_generations_and_preserves_the_parent_queue(self) -> None:
        imported = import_source(self.database, self._fixture())

        class CountingAsr(FakeAsrAdapter):
            def __init__(self):
                super().__init__(
                    [
                        TranscriptSegment(
                            500_000,
                            3_500_000,
                            "Wow, this funny story matters because the ending works.",
                            "en",
                        )
                    ]
                )
                self.calls = 0

            def transcribe(self, *args, **kwargs):
                self.calls += 1
                return super().transcribe(*args, **kwargs)

        asr = CountingAsr()
        first = AnalysisWorkflow(
            self.database,
            asr,
            FakeEvaluatorAdapter(),
            configuration=AnalysisConfig(),
        ).run(imported.source_recording_id)
        parent_entries = self.database.fetch_all(
            "SELECT rank, clip_proposal_id FROM queue_entries WHERE queue_snapshot_id = ? ORDER BY rank",
            (first.queue_snapshot_id,),
        )
        self.assertTrue(parent_entries)
        record_decision(
            self.database,
            str(parent_entries[0]["clip_proposal_id"]),
            DecisionValue.MAYBE,
            idempotency_key="request-more-parent-decision",
            expected_prior_revision=0,
        )

        expanded = AnalysisWorkflow(
            self.database,
            asr,
            FakeEvaluatorAdapter(),
            configuration=AnalysisConfig(budget_tier="expanded"),
        ).run(
            imported.source_recording_id,
            requested_more_from_run_id=first.analysis_run_id,
        )

        self.assertEqual(asr.calls, 1)
        reuses = self.database.fetch_all(
            "SELECT stage_name FROM analysis_stage_reuses WHERE analysis_run_id = ? ORDER BY stage_name",
            (expanded.analysis_run_id,),
        )
        self.assertEqual([row["stage_name"] for row in reuses], ["asr", "embeddings"])
        expanded_entries = self.database.fetch_all(
            "SELECT rank, clip_proposal_id FROM queue_entries WHERE queue_snapshot_id = ? ORDER BY rank",
            (expanded.queue_snapshot_id,),
        )
        self.assertEqual(
            [(row["rank"], row["clip_proposal_id"]) for row in expanded_entries[: len(parent_entries)]],
            [(row["rank"], row["clip_proposal_id"]) for row in parent_entries],
        )
        parent_after = self.database.fetch_all(
            "SELECT rank, clip_proposal_id FROM queue_entries WHERE queue_snapshot_id = ? ORDER BY rank",
            (first.queue_snapshot_id,),
        )
        self.assertEqual([tuple(row) for row in parent_after], [tuple(row) for row in parent_entries])
        decision = self.database.fetch_one(
            "SELECT decision FROM editorial_decisions WHERE clip_proposal_id = ?",
            (parent_entries[0]["clip_proposal_id"],),
        )
        self.assertEqual(decision["decision"], "maybe")
        parent_proposal = self.database.fetch_one(
            "SELECT * FROM clip_proposals WHERE id = ?",
            (parent_entries[0]["clip_proposal_id"],),
        )
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO reference_moment_revisions "
                "(id, source_recording_id, annotation_set_id, revision_number, certainty, category, "
                "start_us, end_us, event_us, short_form_suitability, rationale, frozen, language_slice, created_at) "
                "VALUES ('reference-request-more', ?, 'reference-set-request-more', 1, 'definite', ?, ?, ?, ?, 4, "
                "'request-more fixture', 1, 'en', 'now')",
                (
                    imported.source_recording_id,
                    parent_proposal["category"],
                    parent_proposal["start_us"],
                    parent_proposal["end_us"],
                    parent_proposal["event_us"],
                ),
            )
        report = evaluate_analysis(self.database, expanded.analysis_run_id).report
        self.assertEqual(report["discovery"]["recall"], 1.0)
        self.assertEqual(report["queue_recall"]["recall_at_30"]["recall"], 1.0)
        self.assertEqual(report["analysis"]["request_more_parent"]["id"], first.analysis_run_id)

    def test_outside_context_boundary_reanalysis_creates_an_undecided_successor(self) -> None:
        imported = import_source(self.database, self._fixture())
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE source_recordings SET source_end_us = 400000000 WHERE id = ?",
                (imported.source_recording_id,),
            )
        asr = FakeAsrAdapter(
            [
                TranscriptSegment(
                    500_000,
                    3_500_000,
                    "Wow, this funny story matters because the ending works.",
                    "en",
                )
            ]
        )
        first = AnalysisWorkflow(
            self.database,
            asr,
            FakeEvaluatorAdapter(),
            configuration=AnalysisConfig(),
        ).run(imported.source_recording_id)
        parent_entries = self.database.fetch_all(
            "SELECT rank, clip_proposal_id FROM queue_entries WHERE queue_snapshot_id = ? ORDER BY rank",
            (first.queue_snapshot_id,),
        )
        original_id = str(parent_entries[0]["clip_proposal_id"])
        record_decision(
            self.database,
            original_id,
            DecisionValue.MAYBE,
            idempotency_key="boundary-reanalysis-parent-decision",
            expected_prior_revision=0,
            boundary=SourceInterval(0, 200_000_000),
        )

        reanalysis = AnalysisWorkflow(
            self.database,
            asr,
            FakeEvaluatorAdapter(),
            configuration=AnalysisConfig(),
        ).run(
            imported.source_recording_id,
            boundary_reanalysis_queue_id=first.queue_snapshot_id,
            boundary_reanalysis_proposal_id=original_id,
        )

        successor = self.database.fetch_one(
            "SELECT * FROM clip_proposals WHERE analysis_run_id = ? AND supersedes_proposal_id = ?",
            (reanalysis.analysis_run_id, original_id),
        )
        self.assertIsNotNone(successor)
        successor_entry = self.database.fetch_one(
            "SELECT rank FROM queue_entries WHERE queue_snapshot_id = ? AND clip_proposal_id = ?",
            (reanalysis.queue_snapshot_id, successor["id"]),
        )
        self.assertEqual(successor_entry["rank"], parent_entries[0]["rank"])
        self.assertIsNone(
            self.database.fetch_one(
                "SELECT 1 FROM editorial_decisions WHERE clip_proposal_id = ?",
                (successor["id"],),
            )
        )
        original_decision = self.database.fetch_one(
            "SELECT decision FROM editorial_decisions WHERE clip_proposal_id = ?",
            (original_id,),
        )
        self.assertEqual(original_decision["decision"], "maybe")
        with self.assertRaisesRegex(ValueError, "Human-seeded"):
            evaluate_analysis(self.database, reanalysis.analysis_run_id)

        record_decision(
            self.database,
            original_id,
            DecisionValue.MAYBE,
            idempotency_key="boundary-reanalysis-rejection-decision",
            expected_prior_revision=1,
            boundary=SourceInterval(0, 210_000_000),
        )

        class SemanticRejectionEvaluator(FakeEvaluatorAdapter):
            def evaluate(self, envelope, *, cancellation_requested=None, worker_started=None):
                return EvaluationOutcome(
                    disposition="semantic_rejection",
                    candidate_outcomes=tuple(
                        CandidateEvaluationOutcome(
                            str(candidate["id"]),
                            "too_weak",
                            reason="The expanded interval does not support a successor proposal",
                        )
                        for candidate in envelope["candidates"]
                    ),
                    raw_response='{"disposition":"semantic_rejection"}',
                    metadata={"prompt_tokens": 0, "final_tokens": 0},
                )

        rejected = AnalysisWorkflow(
            self.database,
            asr,
            SemanticRejectionEvaluator(),
            configuration=AnalysisConfig(),
        ).run(
            imported.source_recording_id,
            boundary_reanalysis_queue_id=first.queue_snapshot_id,
            boundary_reanalysis_proposal_id=original_id,
        )
        rejected_entries = self.database.fetch_all(
            "SELECT clip_proposal_id FROM queue_entries WHERE queue_snapshot_id = ? ORDER BY rank",
            (rejected.queue_snapshot_id,),
        )
        self.assertNotIn(original_id, [row["clip_proposal_id"] for row in rejected_entries])
        self.assertEqual(rejected.proposal_count, max(0, len(parent_entries) - 1))

    def test_cancelled_analysis_retries_the_same_run_from_valid_completed_state(self) -> None:
        imported = import_source(self.database, self._fixture())
        cancellation = threading.Event()
        run_started = threading.Event()
        run_ids: list[str] = []
        errors: list[BaseException] = []

        class BlockingAsr:
            def transcribe(self, audio_path, *, cancellation_requested=None, worker_started=None):
                while not cancellation_requested():
                    time.sleep(0.01)
                raise WorkerCancelled("test cancellation")

        workflow = AnalysisWorkflow(
            self.database,
            BlockingAsr(),  # type: ignore[arg-type]
            FakeEvaluatorAdapter(),
            external_cancellation_requested=cancellation.is_set,
        )

        def execute() -> None:
            try:
                workflow.run(
                    imported.source_recording_id,
                    run_started=lambda run_id: (run_ids.append(run_id), run_started.set()),
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=execute)
        thread.start()
        self.assertTrue(run_started.wait(5))
        cancellation.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], AnalysisCancelled)
        run_id = run_ids[0]
        self.assertEqual(
            self.database.fetch_one("SELECT state FROM analysis_runs WHERE id = ?", (run_id,))["state"], "cancelled"
        )

        segment = TranscriptSegment(
            500_000,
            3_500_000,
            "Wow, this funny story matters because the ending works.",
            "en",
        )
        retried = AnalysisWorkflow(
            self.database,
            FakeAsrAdapter([segment]),
            FakeEvaluatorAdapter(),
        ).run(imported.source_recording_id, resume_run_id=run_id)
        self.assertEqual(retried.analysis_run_id, run_id)
        states = [
            row["state"]
            for row in self.database.fetch_all(
                "SELECT state FROM stage_attempts WHERE scope_id = ? AND stage_name = 'asr' ORDER BY attempt_number",
                (run_id,),
            )
        ]
        self.assertEqual(states, ["cancelled", "succeeded"])
        self.assertEqual(
            self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM artifacts WHERE owner_id = ? AND kind = 'asr_raw_output'",
                (run_id,),
            )["count"],
            1,
        )

    def test_export_cannot_commit_after_accept_is_withdrawn_while_rendering(self) -> None:
        imported = import_source(self.database, self._fixture())
        asr = FakeAsrAdapter(
            [
                TranscriptSegment(
                    500_000,
                    3_500_000,
                    "Wow, this funny story matters because the ending works.",
                    "en",
                )
            ]
        )
        analysis = AnalysisWorkflow(self.database, asr, FakeEvaluatorAdapter()).run(imported.source_recording_id)
        proposal = self.database.fetch_one(
            "SELECT p.id FROM queue_entries q JOIN clip_proposals p ON p.id = q.clip_proposal_id "
            "WHERE q.queue_snapshot_id = ? ORDER BY q.rank LIMIT 1",
            (analysis.queue_snapshot_id,),
        )
        accepted = record_decision(
            self.database,
            proposal["id"],
            DecisionValue.ACCEPT,
            idempotency_key=str(uuid.uuid4()),
            expected_prior_revision=0,
        )
        render_started = threading.Event()
        release_render = threading.Event()

        class BlockingMedia(FFmpegAdapter):
            def render_export(self, *args, **kwargs):
                render_started.set()
                if not release_render.wait(10):
                    raise RuntimeError("test render barrier timed out")
                return super().render_export(*args, **kwargs)

        errors: list[BaseException] = []
        export_key = str(uuid.uuid4())

        def execute_export() -> None:
            try:
                export_accepted_clip(
                    self.database,
                    proposal["id"],
                    idempotency_key=export_key,
                    confirmed=True,
                    expected_decision_revision=accepted.revision_number,
                    media=BlockingMedia(),
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=execute_export)
        thread.start()
        self.assertTrue(render_started.wait(10))
        withdraw_decision(
            self.database,
            proposal["id"],
            idempotency_key=str(uuid.uuid4()),
            expected_prior_revision=accepted.revision_number,
        )
        release_render.set()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(self.database.fetch_one("SELECT COUNT(*) AS count FROM exports")["count"], 0)
        request = self.database.fetch_one("SELECT state FROM export_requests WHERE idempotency_key = ?", (export_key,))
        self.assertEqual(request["state"], "failed")

    def test_invalid_evaluator_lineage_preserves_raw_failed_attempt_for_audit(self) -> None:
        imported = import_source(self.database, self._fixture())
        asr = FakeAsrAdapter(
            [
                TranscriptSegment(
                    500_000,
                    3_500_000,
                    "Wow, this funny story matters because the ending works.",
                    "en",
                )
            ]
        )

        class DuplicateOutcomeEvaluator(FakeEvaluatorAdapter):
            def evaluate(self, envelope, *, cancellation_requested=None, worker_started=None):
                valid = super().evaluate(
                    envelope,
                    cancellation_requested=cancellation_requested,
                    worker_started=worker_started,
                )
                return EvaluationOutcome(
                    disposition=valid.disposition,
                    proposals=valid.proposals,
                    candidate_outcomes=(*valid.candidate_outcomes, valid.candidate_outcomes[0]),
                    raw_response='{"invalid_test":"duplicate_candidate_outcome"}',
                    metadata={**(valid.metadata or {}), "prompt_tokens": 7},
                )

        with self.assertRaises(ValueError):
            AnalysisWorkflow(self.database, asr, DuplicateOutcomeEvaluator()).run(imported.source_recording_id)
        attempt = self.database.fetch_one("SELECT * FROM evaluation_attempts ORDER BY started_at DESC LIMIT 1")
        self.assertEqual(attempt["state"], "failed")
        self.assertIsNotNone(attempt["raw_response_relpath"])
        self.assertTrue(self.settings.resolve_work_path(attempt["raw_response_relpath"]).is_file())
        self.assertIn("exactly one outcome", attempt["validation_errors_json"])
        run = self.database.fetch_one("SELECT prompt_tokens FROM analysis_runs ORDER BY created_at DESC LIMIT 1")
        self.assertEqual(run["prompt_tokens"], 7)
        self.assertEqual(self.database.fetch_one("SELECT COUNT(*) AS count FROM clip_proposals")["count"], 0)

    def test_asr_words_must_be_ordered_inside_their_parent_segment(self) -> None:
        imported = import_source(self.database, self._fixture())

        class InvalidWordAsr:
            def transcribe(self, audio_path, *, cancellation_requested=None, worker_started=None):
                return TranscriptionResult(
                    segments=(TranscriptSegment(500_000, 2_000_000, "hello world", "en"),),
                    words=(TranscriptWord(0, 1_900_000, 2_100_000, "world"),),
                    raw={"invalid_test": True},
                )

        with self.assertRaisesRegex(ValueError, "referenced transcript segment"):
            AnalysisWorkflow(
                self.database,
                InvalidWordAsr(),  # type: ignore[arg-type]
                FakeEvaluatorAdapter(),
            ).run(imported.source_recording_id)
        self.assertEqual(self.database.fetch_one("SELECT COUNT(*) AS count FROM transcript_segments")["count"], 0)
        self.assertEqual(self.database.fetch_one("SELECT COUNT(*) AS count FROM transcript_words")["count"], 0)


if __name__ == "__main__":
    unittest.main()

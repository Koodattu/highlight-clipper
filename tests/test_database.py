from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from highlight_clipper.attempts import AttemptStore
from highlight_clipper.database import Database
from highlight_clipper.domain import AttemptState, fingerprint
from highlight_clipper.recovery import reconcile_startup
from highlight_clipper.settings import Settings
from highlight_clipper.workflows.backup import create_backup, restore_backup


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(root, root / "workdir")
        self.settings.ensure_work_directories()
        self.database = Database(self.settings)
        self.database.migrate()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_migration_and_foreign_keys(self) -> None:
        self.database.integrity_check()
        connection = self.database.connect()
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO analysis_runs "
                    "(id, source_recording_id, creator_profile_revision_id, state, input_fingerprint, "
                    "configuration_fingerprint, configuration_json, created_at) "
                    "VALUES ('x', 'missing', 'missing', 'pending', ?, ?, '{}', 'now')",
                    ("0" * 64, "0" * 64),
                )
        finally:
            connection.close()

    def test_attempt_terminal_state_is_immutable(self) -> None:
        store = AttemptStore(self.database)
        digest = fingerprint({"test": True})
        attempt = store.create(
            scope_type="analysis",
            scope_id="run",
            stage_name="test",
            input_fingerprint=digest,
            configuration_fingerprint=digest,
        )
        store.transition(attempt.id, AttemptState.RUNNING, owner_instance="test")
        store.transition(attempt.id, AttemptState.SUCCEEDED)
        with self.assertRaises(ValueError):
            store.transition(attempt.id, AttemptState.RUNNING)

    def test_backup_is_consistent_and_closes_files(self) -> None:
        self.database.ensure_default_profile()
        backup = create_backup(self.database)
        restored = sqlite3.connect(backup.database_snapshot)
        try:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM creator_profile_revisions").fetchone()[0], 1)
        finally:
            restored.close()
        labels = json.loads(backup.portable_labels.read_text(encoding="utf-8"))
        self.assertIn("review_activity_events", labels["tables"])
        self.assertIn("transcript_words", labels["tables"])
        self.assertIn("embedding_generations", labels["tables"])
        self.assertIn("analysis_stage_reuses", labels["tables"])
        self.assertIn("boundary_reanalysis_targets", labels["tables"])

    def test_restore_is_verified_and_keeps_a_pre_restore_safety_backup(self) -> None:
        self.database.ensure_default_profile()
        backup = create_backup(self.database)
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE creator_profile_revisions SET desired_content = 'changed after backup'"
            )

        result = restore_backup(self.database, backup.directory)

        profile = self.database.fetch_one("SELECT desired_content FROM creator_profile_revisions")
        self.assertEqual(profile["desired_content"], "")
        self.assertTrue(result.safety_backup.is_dir())

    def test_restore_can_replace_a_corrupt_current_database(self) -> None:
        self.database.ensure_default_profile()
        backup = create_backup(self.database)
        for suffix in ("-wal", "-shm"):
            self.database.path.with_name(f"{self.database.path.name}{suffix}").unlink(missing_ok=True)
        self.database.path.write_bytes(b"not a sqlite database")

        result = restore_backup(self.database, backup.directory)

        self.database.integrity_check()
        self.assertIsNotNone(self.database.fetch_one("SELECT id FROM creator_profile_revisions"))
        self.assertTrue(result.safety_backup.name.startswith("pre-restore-raw-"))
        self.assertTrue((result.safety_backup / "highlight-clipper.sqlite3").is_file())

    def test_startup_recovery_persists_and_quarantines_an_unregistered_import_tree(self) -> None:
        source_id = "source_interrupted"
        attempt_id = "import_interrupted"
        abandoned = self.settings.work_dir / "sources" / source_id
        abandoned.mkdir(parents=True)
        (abandoned / "original.mp4.partial").write_bytes(b"partial")
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO source_import_attempts "
                "(id, state, input_path, owner_instance, planned_source_recording_id, created_at, started_at) "
                "VALUES (?, 'running', 'C:/input.mp4', 'test|999999|0', ?, 'now', 'now')",
                (attempt_id, source_id),
            )
            connection.execute(
                "INSERT INTO active_operation_lease "
                "(singleton, operation_type, operation_id, owner_instance, heartbeat_at) "
                "VALUES (1, 'source_import', ?, 'test|999999|0', 'now')",
                (attempt_id,),
            )
        report = reconcile_startup(self.database)
        self.assertTrue(report.stale_lease_recovered)
        self.assertFalse(abandoned.exists())
        self.assertEqual(len(report.quarantined_paths), 1)
        item = self.database.fetch_one(
            "SELECT * FROM recovery_items WHERE relative_path = ?", (f"sources/{source_id}",)
        )
        self.assertEqual(item["state"], "completed")
        self.assertEqual(
            self.database.fetch_one("SELECT state FROM source_import_attempts WHERE id = ?", (attempt_id,))["state"],
            "failed",
        )

    def test_startup_removes_private_worker_payloads_when_no_operation_is_active(self) -> None:
        worker = self.settings.work_dir / "tmp" / "workers" / "worker-old"
        worker.mkdir(parents=True)
        request = worker / "request.json"
        result = worker / "result.json"
        request.write_text("private request", encoding="utf-8")
        result.write_text("private result", encoding="utf-8")
        reconcile_startup(self.database)
        self.assertFalse(request.exists())
        self.assertFalse(result.exists())


if __name__ == "__main__":
    unittest.main()

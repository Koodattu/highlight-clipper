from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from highlight_clipper.database import Database
from highlight_clipper.settings import Settings
from highlight_clipper.web.app import create_app
from highlight_clipper.workflows.import_source import import_source


class WebSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = Settings(root, root / "workdir")
        self.settings.ensure_work_directories()
        self.app = create_app(self.settings, allowed_hosts={"testserver"})
        self.client = TestClient(self.app)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        match = re.search(r'name="highlight-clipper-token" content="([^"]+)"', page.text)
        self.assertIsNotNone(match)
        self.token = match.group(1)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def mutation_headers(self) -> dict[str, str]:
        return {"Origin": "http://testserver", "X-Highlight-Clipper-Token": self.token}

    def test_host_origin_and_session_token_are_enforced(self) -> None:
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)
        payload = {
            "languages": ["fi", "en"],
            "category_priorities": {
                "reaction": 1,
                "comedy": 1,
                "story": 1,
                "opinion": 1,
                "explanation": 1,
            },
            "desired_content": "",
            "avoided_content": "",
            "preferred_durations": {
                "reaction": [15, 60],
                "comedy": [20, 90],
                "story": [45, 180],
                "opinion": [30, 180],
                "explanation": [60, 240],
            },
        }
        self.assertEqual(self.client.post("/api/profiles", json=payload).status_code, 403)
        bad = {"Origin": "http://testserver", "X-Highlight-Clipper-Token": "wrong"}
        self.assertEqual(self.client.post("/api/profiles", json=payload, headers=bad).status_code, 403)
        response = self.client.post("/api/profiles", json=payload, headers=self.mutation_headers())
        self.assertEqual(response.status_code, 201)
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class WebMediaTests(WebSecurityTests):
    def _fixture(self) -> Path:
        path = Path(self.temporary.name) / "source.mp4"
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
                "color=size=160x90:rate=10:duration=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2",
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

    def test_opaque_media_ranges_are_bounded_and_validated(self) -> None:
        database = Database(self.settings)
        imported = import_source(database, self._fixture())
        source = next(
            item
            for item in self.client.get("/api/bootstrap").json()["sources"]
            if item["id"] == imported.source_recording_id
        )
        media_url = f"/api/media/{source['proxy_artifact_id']}"
        partial = self.client.get(media_url, headers={"Range": "bytes=0-99"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(len(partial.content), 100)
        self.assertTrue(partial.headers["content-range"].startswith("bytes 0-99/"))
        self.assertEqual(self.client.get(media_url, headers={"Range": "garbage"}).status_code, 416)
        self.assertEqual(self.client.get("/api/media/not-an-id").status_code, 404)
        source_artifact = database.fetch_one(
            "SELECT id FROM artifacts WHERE source_recording_id = ? AND kind = 'source_original'",
            (imported.source_recording_id,),
        )
        self.assertEqual(self.client.get(f"/api/media/{source_artifact['id']}").status_code, 404)
        proxy = database.fetch_one("SELECT relative_path FROM artifacts WHERE id = ?", (source["proxy_artifact_id"],))
        proxy_path = self.settings.resolve_work_path(str(proxy["relative_path"]))
        original_size = proxy_path.stat().st_size
        with proxy_path.open("ab") as handle:
            handle.write(b"tampered")
        try:
            self.assertEqual(self.client.get(media_url).status_code, 409)
        finally:
            with proxy_path.open("r+b") as handle:
                handle.truncate(original_size)

        waveform = self.client.get(f"/api/sources/{imported.source_recording_id}/waveform?bins=100")
        self.assertEqual(waveform.status_code, 200)
        self.assertLessEqual(len(waveform.json()["bins"]), 100)
        audio = database.fetch_one(
            "SELECT relative_path FROM artifacts WHERE source_recording_id = ? AND kind = 'analysis_audio'",
            (imported.source_recording_id,),
        )
        audio_path = self.settings.resolve_work_path(str(audio["relative_path"]))
        hidden_path = audio_path.with_suffix(".temporarily-hidden")
        audio_path.replace(hidden_path)
        try:
            cached = self.client.get(f"/api/sources/{imported.source_recording_id}/waveform?bins=100")
            self.assertEqual(cached.status_code, 200)
            self.assertEqual(cached.json()["bins"], waveform.json()["bins"])
        finally:
            hidden_path.replace(audio_path)

    def test_explicit_fake_analysis_runs_through_the_same_web_task_surface(self) -> None:
        database = Database(self.settings)
        imported = import_source(database, self._fixture())
        response = self.client.post(
            f"/api/sources/{imported.source_recording_id}/analyses",
            json={"mode": "fake"},
            headers=self.mutation_headers(),
        )
        self.assertEqual(response.status_code, 202)
        task_id = response.json()["task_id"]
        deadline = time.monotonic() + 10
        task = None
        while time.monotonic() < deadline:
            tasks = self.client.get("/api/bootstrap").json()["tasks"]
            task = next(item for item in tasks if item["id"] == task_id)
            if task["state"] not in {"pending", "running"}:
                break
            time.sleep(0.05)
        self.assertEqual(task["state"], "succeeded", task.get("error"))
        self.assertEqual(task["overall_progress"], 1.0)
        self.assertTrue(task["queue_snapshot_id"].startswith("queue_"))
        stage_progress = database.fetch_all(
            "SELECT progress FROM stage_attempts WHERE scope_type = 'analysis' AND scope_id = ?",
            (task["analysis_run_id"],),
        )
        self.assertTrue(stage_progress)
        self.assertTrue(all(float(row["progress"]) == 1.0 for row in stage_progress))
        queue = self.client.get(f"/api/queues/{task['queue_snapshot_id']}").json()
        invalid_boundary = {
            "decision": "accept",
            "idempotency_key": "invalid-boundary-web-test",
            "expected_prior_revision": 0,
            "boundary_start_seconds": 0,
            "boundary_end_seconds": 999,
        }
        invalid = self.client.post(
            f"/api/proposals/{queue['proposals'][0]['id']}/decisions",
            json=invalid_boundary,
            headers=self.mutation_headers(),
        )
        self.assertEqual(invalid.status_code, 422)
        activity = {
            "queue_snapshot_id": task["queue_snapshot_id"],
            "clip_proposal_id": queue["proposals"][0]["id"],
            "session_id": "review-session-test",
            "sequence_number": 0,
            "active_milliseconds": 5000,
            "activity_kind": "interaction",
        }
        recorded = self.client.post("/api/review-activity", json=activity, headers=self.mutation_headers())
        self.assertEqual(recorded.status_code, 201)
        self.assertTrue(recorded.json()["recorded"])
        replayed = self.client.post("/api/review-activity", json=activity, headers=self.mutation_headers())
        self.assertEqual(replayed.status_code, 201)
        self.assertFalse(replayed.json()["recorded"])

        more = self.client.post(
            f"/api/queues/{task['queue_snapshot_id']}/more",
            json={},
            headers=self.mutation_headers(),
        )
        self.assertEqual(more.status_code, 202, more.text)
        more_task_id = more.json()["task_id"]
        deadline = time.monotonic() + 10
        more_task = None
        while time.monotonic() < deadline:
            tasks = self.client.get("/api/bootstrap").json()["tasks"]
            more_task = next(item for item in tasks if item["id"] == more_task_id)
            if more_task["state"] not in {"pending", "running"}:
                break
            time.sleep(0.05)
        self.assertEqual(more_task["state"], "succeeded", more_task.get("error"))
        expanded_queue = self.client.get(f"/api/queues/{more_task['queue_snapshot_id']}").json()
        self.assertEqual(
            [proposal["id"] for proposal in expanded_queue["proposals"][: len(queue["proposals"])]],
            [proposal["id"] for proposal in queue["proposals"]],
        )
        duplicate_more = self.client.post(
            f"/api/queues/{task['queue_snapshot_id']}/more",
            json={},
            headers=self.mutation_headers(),
        )
        self.assertEqual(duplicate_more.status_code, 409)

        with database.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE source_recordings SET source_end_us = 400000000 WHERE id = ?",
                (imported.source_recording_id,),
            )
        stale_decision = self.client.post(
            f"/api/proposals/{queue['proposals'][0]['id']}/decisions",
            json={
                "decision": "maybe",
                "idempotency_key": "web-stale-boundary-decision",
                "expected_prior_revision": 0,
                "boundary_start_seconds": 0,
                "boundary_end_seconds": 200,
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(stale_decision.status_code, 201, stale_decision.text)
        reanalysis = self.client.post(
            f"/api/queues/{task['queue_snapshot_id']}/proposals/{queue['proposals'][0]['id']}/reanalyze-boundary",
            json={},
            headers=self.mutation_headers(),
        )
        self.assertEqual(reanalysis.status_code, 202, reanalysis.text)
        reanalysis_task_id = reanalysis.json()["task_id"]
        deadline = time.monotonic() + 10
        reanalysis_task = None
        while time.monotonic() < deadline:
            tasks = self.client.get("/api/bootstrap").json()["tasks"]
            reanalysis_task = next(item for item in tasks if item["id"] == reanalysis_task_id)
            if reanalysis_task["state"] not in {"pending", "running"}:
                break
            time.sleep(0.05)
        self.assertEqual(reanalysis_task["state"], "succeeded", reanalysis_task.get("error"))
        successor_queue = self.client.get(f"/api/queues/{reanalysis_task['queue_snapshot_id']}").json()
        self.assertEqual(
            successor_queue["proposals"][0]["supersedes_proposal_id"],
            queue["proposals"][0]["id"],
        )


if __name__ == "__main__":
    unittest.main()

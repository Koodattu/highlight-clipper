from __future__ import annotations

import json
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from decimal import Decimal
from pathlib import Path

from highlight_clipper.adapters.ffmpeg import FFmpegAdapter


def dominant_tone_by_zero_crossings(path: Path, seconds: float = 0.5) -> float:
    with wave.open(str(path), "rb") as audio:
        frame_count = int(audio.getframerate() * seconds)
        raw = audio.readframes(frame_count)
        samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)
        crossings = sum(1 for first, second in zip(samples, samples[1:], strict=False) if (first < 0) != (second < 0))
        duration = len(samples) / audio.getframerate()
    return crossings / (2 * duration)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class SelectedStreamAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fixture(self) -> Path:
        source = self.root / "staggered.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-copyts",
                "-vsync",
                "0",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:size=160x90:rate=10:duration=10",
                "-itsoffset",
                "5",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=10:duration=4",
                "-itsoffset",
                "2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=400:sample_rate=48000:duration=3[a0];"
                "sine=frequency=1000:sample_rate=48000:duration=4[a1];"
                "[a0][a1]concat=n=2:v=0:a=1",
                "-map",
                "0:v",
                "-map",
                "1:v",
                "-map",
                "2:a",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "pcm_s16le",
                "-metadata",
                "title=SENSITIVE_SOURCE_TITLE",
                "-metadata",
                "comment=SENSITIVE_SOURCE_COMMENT",
                str(source),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return source

    def _extract_audio(self, media: Path, destination: Path) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(media),
                "-t",
                "0.5",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(destination),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def test_import_proxy_analysis_audio_and_export_share_selected_video_source_time(self) -> None:
        adapter = FFmpegAdapter()
        source = self._fixture()
        destination = self.root / "prepared"
        destination.mkdir()
        imported = adapter.prepare_import(source, destination, video_stream=1, audio_stream=2)
        self.assertEqual(imported.source_end_us, 4_000_000)
        self.assertEqual(imported.manifest["source_time_origin_seconds"], "5.000000")
        self.assertAlmostEqual(dominant_tone_by_zero_crossings(imported.analysis_audio_path), 1000, delta=15)

        proxy_audio = self.root / "proxy.wav"
        self._extract_audio(imported.proxy_path, proxy_audio)
        self.assertAlmostEqual(dominant_tone_by_zero_crossings(proxy_audio), 1000, delta=20)

        export_path = self.root / "export.mp4"
        video_origin = str(imported.manifest["source_time_origin_seconds"])
        audio_start = str(Decimal(video_origin) + Decimal(str(imported.manifest["audio_video_start_offset_seconds"])))
        adapter.render_export(
            source,
            export_path,
            0,
            1_000_000,
            source_end_us=imported.source_end_us,
            video_stream_index=1,
            audio_stream_index=2,
            video_origin_seconds=video_origin,
            audio_start_seconds=audio_start,
        )
        export_audio = self.root / "export.wav"
        self._extract_audio(export_path, export_audio)
        self.assertAlmostEqual(dominant_tone_by_zero_crossings(export_audio), 1000, delta=20)
        exported_probe = adapter.probe(export_path)
        self.assertNotIn("SENSITIVE_SOURCE", json.dumps(exported_probe.raw))


if __name__ == "__main__":
    unittest.main()

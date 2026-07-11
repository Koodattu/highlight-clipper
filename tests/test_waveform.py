from __future__ import annotations

import struct
import tempfile
import unittest
import wave
from pathlib import Path

from highlight_clipper.waveform import build_waveform_peaks, read_waveform_peaks


class WaveformCacheTests(unittest.TestCase):
    def test_builds_compact_native_peaks_and_aggregates_requested_bins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio_path = root / "audio.wav"
            with wave.open(str(audio_path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(1000)
                samples = [0] * 100 + [1000] * 100 + [-32768] * 100 + [500] * 50
                audio.writeframes(b"".join(struct.pack("<h", value) for value in samples))
            waveform_path = root / "waveform.u16le"
            metadata = build_waveform_peaks(audio_path, waveform_path)
            self.assertEqual(metadata["peak_count"], 4)
            self.assertEqual(waveform_path.stat().st_size, 8)

            peaks, start_us, end_us = read_waveform_peaks(
                waveform_path,
                metadata,
                start_us=0,
                end_us=350_000,
                max_bins=2,
            )
            self.assertEqual(peaks, [round(1000 / 32768, 4), 1.0])
            self.assertEqual(start_us, 0)
            self.assertEqual(end_us, 400_000)

    def test_rejects_truncated_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "waveform.u16le"
            path.write_bytes(b"\x00\x00")
            with self.assertRaisesRegex(RuntimeError, "metadata"):
                read_waveform_peaks(
                    path,
                    {
                        "schema_version": 1,
                        "encoding": "pcm-absolute-peak-u16le",
                        "native_bin_us": 100_000,
                        "peak_count": 2,
                    },
                    start_us=0,
                    end_us=100_000,
                    max_bins=100,
                )


if __name__ == "__main__":
    unittest.main()

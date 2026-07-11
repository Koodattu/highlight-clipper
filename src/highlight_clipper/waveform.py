from __future__ import annotations

import audioop
import math
import os
import struct
import wave
from array import array
from pathlib import Path

WAVEFORM_SCHEMA_VERSION = 1
WAVEFORM_ENCODING = "pcm-absolute-peak-u16le"
NATIVE_BIN_US = 100_000


def build_waveform_peaks(
    audio_path: Path,
    destination: Path,
    *,
    native_bin_us: int = NATIVE_BIN_US,
) -> dict[str, int | str]:
    if native_bin_us <= 0:
        raise ValueError("Waveform native bin duration must be positive")
    partial = destination.with_name(f"{destination.name}.partial")
    partial.unlink(missing_ok=True)
    peak_count = 0
    with wave.open(str(audio_path), "rb") as audio:
        channels = audio.getnchannels()
        sample_width = audio.getsampwidth()
        sample_rate = audio.getframerate()
        if channels != 1 or sample_width != 2 or audio.getcomptype() != "NONE":
            raise RuntimeError("Waveform input must be uncompressed mono 16-bit PCM")
        frames_per_bin = max(1, round(sample_rate * native_bin_us / 1_000_000))
        with partial.open("xb") as output:
            while raw := audio.readframes(frames_per_bin):
                peak = min(65_535, audioop.max(raw, sample_width))
                output.write(struct.pack("<H", peak))
                peak_count += 1
            output.flush()
            os.fsync(output.fileno())
    partial.replace(destination)
    return {
        "schema_version": WAVEFORM_SCHEMA_VERSION,
        "encoding": WAVEFORM_ENCODING,
        "native_bin_us": native_bin_us,
        "peak_count": peak_count,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
    }


def read_waveform_peaks(
    path: Path,
    metadata: dict[str, object],
    *,
    start_us: int,
    end_us: int,
    max_bins: int,
) -> tuple[list[float], int, int]:
    if metadata.get("schema_version") != WAVEFORM_SCHEMA_VERSION or metadata.get("encoding") != WAVEFORM_ENCODING:
        raise RuntimeError("Waveform artifact has an unsupported format")
    native_bin_us = int(metadata["native_bin_us"])
    peak_count = int(metadata["peak_count"])
    if native_bin_us <= 0 or peak_count < 0 or path.stat().st_size != peak_count * 2:
        raise RuntimeError("Waveform artifact metadata does not match its bytes")
    if not 0 <= start_us < end_us or max_bins <= 0:
        raise ValueError("Waveform range and bin count must be positive")
    start_index = min(peak_count, start_us // native_bin_us)
    end_index = min(peak_count, math.ceil(end_us / native_bin_us))
    if end_index <= start_index:
        return [], int(start_index * native_bin_us), int(end_index * native_bin_us)
    with path.open("rb") as handle:
        handle.seek(start_index * 2)
        raw = handle.read((end_index - start_index) * 2)
    if len(raw) != (end_index - start_index) * 2:
        raise RuntimeError("Waveform artifact is truncated")
    values = array("H")
    values.frombytes(raw)
    if values.itemsize != 2:
        raise RuntimeError("Waveform reader does not support this platform")
    if struct.pack("=H", 1) != struct.pack("<H", 1):
        values.byteswap()
    group_size = max(1, math.ceil(len(values) / max_bins))
    peaks = [round(max(values[index : index + group_size]) / 32768, 4) for index in range(0, len(values), group_size)]
    return peaks, int(start_index * native_bin_us), int(end_index * native_bin_us)

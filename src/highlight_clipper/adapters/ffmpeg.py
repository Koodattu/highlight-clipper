from __future__ import annotations

import json
import os
import statistics
import subprocess
import threading
from collections import deque
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..ports import ImportedMedia, MediaProbe, SelectedStream
from ..timebase import seconds_to_us


class MediaError(RuntimeError):
    pass


def _decimal(value: object | None) -> Decimal | None:
    if value in (None, "", "N/A"):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


class FFmpegAdapter:
    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe"):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

    def _run(self, arguments: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                arguments,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except FileNotFoundError as exc:
            raise MediaError(f"Required media tool was not found: {arguments[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"Media command timed out: {arguments[0]}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "unknown media error").strip()[-2000:]
            raise MediaError(f"{arguments[0]} failed: {detail}") from exc

    def version_manifest(self) -> dict[str, str]:
        ffmpeg = self._run([self.ffmpeg, "-version"], timeout=15).stdout.splitlines()[0]
        ffprobe = self._run([self.ffprobe, "-version"], timeout=15).stdout.splitlines()[0]
        return {"ffmpeg": ffmpeg, "ffprobe": ffprobe}

    def preflight(self) -> dict[str, object]:
        versions = self.version_manifest()
        encoders = self._run([self.ffmpeg, "-hide_banner", "-encoders"], timeout=30).stdout
        filters = self._run([self.ffmpeg, "-hide_banner", "-filters"], timeout=30).stdout
        missing = [name for name in ("libx264", " aac ") if name not in encoders]
        missing.extend(name for name in ("scale", "aresample", "atrim") if name not in filters)
        if missing:
            raise MediaError("FFmpeg is missing required capabilities: " + ", ".join(missing))
        return {**versions, "required_capabilities": "ok"}

    def probe(self, path: Path) -> MediaProbe:
        result = self._run(
            [
                self.ffprobe,
                "-protocol_whitelist",
                "file,crypto,data",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        )
        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MediaError("FFprobe returned invalid JSON") from exc
        streams = raw.get("streams", [])

        def selected(kind: str) -> tuple[SelectedStream, ...]:
            values: list[SelectedStream] = []
            for stream in streams:
                if stream.get("codec_type") != kind:
                    continue
                values.append(
                    SelectedStream(
                        index=int(stream["index"]),
                        codec=str(stream.get("codec_name", "unknown")),
                        time_base=str(stream.get("time_base", "0/1")),
                        start_time=stream.get("start_time"),
                        duration=stream.get("duration"),
                        disposition_default=bool(stream.get("disposition", {}).get("default", 0)),
                    )
                )
            return tuple(values)

        format_data = raw.get("format", {})
        return MediaProbe(
            format_name=str(format_data.get("format_name", "unknown")),
            format_duration=format_data.get("duration"),
            format_start_time=format_data.get("start_time"),
            video_streams=selected("video"),
            audio_streams=selected("audio"),
            raw=raw,
        )

    @staticmethod
    def select_stream(streams: tuple[SelectedStream, ...], requested: int | None, kind: str) -> SelectedStream:
        if requested is not None:
            for stream in streams:
                if stream.index == requested:
                    return stream
            raise MediaError(f"Requested {kind} stream {requested} does not exist")
        defaults = [stream for stream in streams if stream.disposition_default]
        if len(defaults) == 1:
            return defaults[0]
        if len(streams) == 1:
            return streams[0]
        if not streams:
            raise MediaError(f"The source has no playable {kind} stream")
        indexes = ", ".join(str(stream.index) for stream in streams)
        raise MediaError(f"The source has ambiguous {kind} streams ({indexes}); select one explicitly")

    def _scan_video_timeline(self, path: Path, stream_index: int, probe: MediaProbe) -> tuple[int, Decimal]:
        command = [
            self.ffprobe,
            "-protocol_whitelist",
            "file,crypto,data",
            "-v",
            "error",
            "-select_streams",
            str(stream_index),
            "-show_entries",
            "frame=best_effort_timestamp_time,pkt_duration_time",
            "-of",
            "csv=p=0",
            str(path),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise MediaError(f"Required media tool was not found: {self.ffprobe}") from exc
        stderr_lines: deque[str] = deque(maxlen=100)
        assert process.stderr is not None

        def drain_stderr() -> None:
            for message in process.stderr:
                stderr_lines.append(message)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        first: Decimal | None = None
        last_timestamp: Decimal | None = None
        last_end: Decimal | None = None
        recent_deltas: deque[Decimal] = deque(maxlen=120)
        assert process.stdout is not None
        for line in process.stdout:
            fields = [field.strip() for field in line.strip().split(",")]
            timestamp = _decimal(fields[0] if fields else None)
            duration = _decimal(fields[1] if len(fields) > 1 else None)
            if timestamp is None:
                continue
            if first is None:
                first = timestamp
            if last_timestamp is not None and timestamp > last_timestamp:
                recent_deltas.append(timestamp - last_timestamp)
            last_timestamp = timestamp
            if duration is not None and duration > 0:
                candidate_end = timestamp + duration
                last_end = candidate_end if last_end is None else max(last_end, candidate_end)
        process.stdout.close()
        return_code = process.wait()
        stderr_thread.join(timeout=5)
        process.stderr.close()
        if return_code != 0:
            raise MediaError(f"FFprobe frame scan failed: {''.join(stderr_lines).strip()[-2000:]}")
        if first is None or last_timestamp is None:
            raise MediaError("No decodable displayed video frames were found")
        if last_end is None or last_end <= last_timestamp:
            fallback = statistics.median(recent_deltas) if recent_deltas else None
            if fallback is None or fallback <= 0:
                selected = next(
                    stream for stream in probe.raw.get("streams", []) if stream.get("index") == stream_index
                )
                rate = str(selected.get("avg_frame_rate") or selected.get("r_frame_rate") or "0/1")
                numerator, denominator = (int(value) for value in rate.split("/", 1))
                fallback = Decimal(denominator) / Decimal(numerator) if numerator else Decimal("0.04")
            last_end = last_timestamp + fallback
        source_end = last_end - first
        if source_end <= 0:
            raise MediaError("Selected video has no positive playable duration")
        return seconds_to_us(source_end), first

    @staticmethod
    def _audio_filter(audio_start: Decimal, video_start: Decimal, duration: Decimal) -> str:
        trim_before = max(Decimal(0), video_start - audio_start)
        delay = max(Decimal(0), audio_start - video_start)
        filters = [
            "asetpts=PTS-STARTPTS",
            f"atrim=start={trim_before:f}",
            "asetpts=PTS-STARTPTS",
            "aresample=async=1:first_pts=0",
        ]
        if delay > 0:
            filters.append(f"adelay={int(delay * 1000)}:all=1")
        filters.extend((f"apad=whole_dur={duration:f}", f"atrim=duration={duration:f}"))
        return ",".join(filters)

    def prepare_import(
        self,
        source_path: Path,
        destination_dir: Path,
        video_stream: int | None,
        audio_stream: int | None,
    ) -> ImportedMedia:
        probe = self.probe(source_path)
        video = self.select_stream(probe.video_streams, video_stream, "video")
        audio = self.select_stream(probe.audio_streams, audio_stream, "audio")
        source_end_us, video_origin = self._scan_video_timeline(source_path, video.index, probe)
        duration = Decimal(source_end_us) / Decimal(1_000_000)
        audio_start = _decimal(audio.start_time)
        if audio_start is None:
            audio_start = _decimal(probe.format_start_time) or video_origin
        audio_filter = self._audio_filter(audio_start, video_origin, duration)

        proxy_partial = destination_dir / "review.mp4.partial"
        audio_partial = destination_dir / "analysis.wav.partial"
        proxy_final = destination_dir / "review.mp4"
        audio_final = destination_dir / "analysis.wav"
        for path in (proxy_partial, audio_partial):
            if path.exists():
                path.unlink()

        video_filter = (
            "setpts=PTS-STARTPTS,"
            "scale=w='min(1280,iw)':h=-2:force_original_aspect_ratio=decrease,"
            "pad=ceil(iw/2)*2:ceil(ih/2)*2"
        )
        self._run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,crypto,data",
                "-i",
                str(source_path),
                "-filter_complex",
                f"[0:{video.index}]{video_filter}[v];[0:{audio.index}]{audio_filter}[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-map_metadata",
                "-1",
                "-map_metadata:s:v",
                "-1",
                "-map_metadata:s:a",
                "-1",
                "-map_chapters",
                "-1",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                "-t",
                f"{duration:f}",
                "-f",
                "mp4",
                str(proxy_partial),
            ]
        )
        self._run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,crypto,data",
                "-i",
                str(source_path),
                "-filter_complex",
                f"[0:{audio.index}]{audio_filter}[a]",
                "-map",
                "[a]",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-t",
                f"{duration:f}",
                "-f",
                "wav",
                str(audio_partial),
            ]
        )
        self._validate_derivative(proxy_partial, source_end_us, require_video=True)
        self._validate_derivative(audio_partial, source_end_us, require_video=False)
        proxy_partial.replace(proxy_final)
        audio_partial.replace(audio_final)

        stream_data = {int(stream["index"]): stream for stream in probe.raw.get("streams", [])}
        video_data = stream_data[video.index]
        audio_data = stream_data[audio.index]
        avg_rate = str(video_data.get("avg_frame_rate", "0/1"))
        real_rate = str(video_data.get("r_frame_rate", "0/1"))
        manifest: dict[str, object] = {
            "schema_version": 1,
            "source_time_origin_seconds": str(video_origin),
            "source_end_us": source_end_us,
            "container": probe.raw.get("format", {}),
            "selected_video_stream": video_data,
            "selected_audio_stream": audio_data,
            "audio_video_start_offset_seconds": str(audio_start - video_origin),
            "variable_frame_rate_indicated": avg_rate != real_rate,
            "tools": self.version_manifest(),
            "probe_arguments": ["-show_format", "-show_streams", "-of", "json"],
        }
        return ImportedMedia(
            source_end_us=source_end_us,
            video_stream_index=video.index,
            audio_stream_index=audio.index,
            manifest=manifest,
            proxy_path=proxy_final,
            analysis_audio_path=audio_final,
        )

    def _validate_derivative(self, path: Path, source_end_us: int, *, require_video: bool) -> None:
        probe = self.probe(path)
        if require_video and (not probe.video_streams or not probe.audio_streams):
            raise MediaError("Review proxy is missing its video or audio stream")
        if not require_video and (probe.video_streams or not probe.audio_streams):
            raise MediaError("Analysis audio has an unexpected stream layout")
        duration = _decimal(probe.format_duration)
        if duration is None:
            raise MediaError("Derivative has no playable duration")
        expected = Decimal(source_end_us) / Decimal(1_000_000)
        tolerance = max(Decimal("0.25"), expected * Decimal("0.002"))
        if abs(duration - expected) > tolerance:
            raise MediaError(f"Derivative duration {duration}s does not align with source duration {expected}s")

    def render_export(
        self,
        source_path: Path,
        destination: Path,
        start_us: int,
        end_us: int,
        *,
        source_end_us: int,
        video_stream_index: int,
        audio_stream_index: int,
        video_origin_seconds: str,
        audio_start_seconds: str,
    ) -> None:
        if not 0 <= start_us < end_us:
            raise ValueError("Export interval must satisfy 0 <= start < end")
        start = Decimal(start_us) / Decimal(1_000_000)
        end = Decimal(end_us) / Decimal(1_000_000)
        duration = Decimal(end_us - start_us) / Decimal(1_000_000)
        full_duration = Decimal(source_end_us) / Decimal(1_000_000)
        aligned_audio = self._audio_filter(Decimal(audio_start_seconds), Decimal(video_origin_seconds), full_duration)
        video_filter = (
            f"setpts=PTS-STARTPTS,trim=start={start:f}:end={end:f},setpts=PTS-STARTPTS,pad=ceil(iw/2)*2:ceil(ih/2)*2"
        )
        audio_filter = f"{aligned_audio},atrim=start={start:f}:end={end:f},asetpts=PTS-STARTPTS"
        self._run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,crypto,data",
                "-i",
                str(source_path),
                "-filter_complex",
                f"[0:{video_stream_index}]{video_filter}[v];[0:{audio_stream_index}]{audio_filter}[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-map_metadata",
                "-1",
                "-map_metadata:s:v",
                "-1",
                "-map_metadata:s:a",
                "-1",
                "-map_chapters",
                "-1",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-preset",
                "medium",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-t",
                f"{duration:f}",
                "-f",
                "mp4",
                str(destination),
            ]
        )

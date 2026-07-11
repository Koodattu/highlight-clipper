from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import wave
from pathlib import Path


def _write_atomic(path: Path, value: object) -> None:
    partial = path.with_name(f"{path.name}.partial")
    partial.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    partial.replace(path)


def _chunks(duration_seconds: float, chunk_seconds: float, overlap_seconds: float):
    start = 0.0
    index = 0
    stride = chunk_seconds - overlap_seconds
    while start < duration_seconds:
        end = min(duration_seconds, start + chunk_seconds)
        yield index, start, end
        if end >= duration_seconds:
            break
        start += stride
        index += 1


def _checkpoint_plan(plan: list[tuple[int, float, float]]) -> list[dict[str, object]]:
    return [{"index": index, "start": start, "end": end} for index, start, end in plan]


def _load_checkpoint(
    checkpoint_path: Path,
    *,
    fingerprint: str,
    plan: list[tuple[int, float, float]],
) -> dict[str, object]:
    fresh: dict[str, object] = {
        "schema_version": 1,
        "fingerprint": fingerprint,
        "plan": _checkpoint_plan(plan),
        "completed": {},
    }
    if not checkpoint_path.is_file():
        return fresh
    loaded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if loaded.get("fingerprint") != fingerprint:
        return fresh
    if loaded.get("schema_version") != 1 or loaded.get("plan") != fresh["plan"]:
        raise RuntimeError("ASR checkpoint does not match the deterministic chunk plan")
    completed = loaded.get("completed")
    if not isinstance(completed, dict):
        raise RuntimeError("ASR checkpoint completed-chunk data is invalid")
    plan_by_key = {str(index): (start, end) for index, start, end in plan}
    for key, chunk in completed.items():
        if key not in plan_by_key or not isinstance(chunk, dict):
            raise RuntimeError("ASR checkpoint contains an unknown completed chunk")
        start, end = plan_by_key[key]
        if chunk.get("index") != int(key) or chunk.get("start") != start or chunk.get("end") != end:
            raise RuntimeError("ASR checkpoint chunk identity is invalid")
        if not isinstance(chunk.get("segments"), list):
            raise RuntimeError("ASR checkpoint chunk result is invalid")
    return loaded


def _stitch_segments(raw_segments: list[dict[str, object]], duration: float) -> list[dict[str, object]]:
    stitched: list[dict[str, object]] = []
    previous_end = 0.0
    for raw in sorted(raw_segments, key=lambda item: (item["start"], item["end"], item["text"])):
        start = max(0.0, float(raw["start"]), previous_end)
        end = min(duration, float(raw["end"]))
        if end <= start or not str(raw["text"]).strip():
            continue
        kept_words: list[dict[str, object]] = []
        word_end = start
        for word in sorted(raw.get("words", []), key=lambda item: (item["start"], item["end"])):
            word_start = max(start, float(word["start"]), word_end)
            current_end = min(end, float(word["end"]))
            if current_end <= word_start:
                continue
            kept_words.append({**word, "start": word_start, "end": current_end})
            word_end = current_end
        stitched.append({**raw, "start": start, "end": end, "words": kept_words})
        previous_end = end
    return stitched


def transcribe(request: dict[str, object]) -> dict[str, object]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed in the project environment") from exc
    audio_path = Path(str(request["audio_path"])).resolve(strict=True)
    model_path = Path(str(request["model_path"])).resolve(strict=True)
    checkpoint_path = Path(str(request["checkpoint_path"])).resolve()
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(audio_path), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
    chunk_seconds = float(request.get("chunk_seconds", 900))
    overlap_seconds = float(request.get("overlap_seconds", 15))
    if not 0 < overlap_seconds < chunk_seconds:
        raise ValueError("ASR overlap must be positive and shorter than a chunk")
    plan = list(_chunks(duration, chunk_seconds, overlap_seconds))
    checkpoint = _load_checkpoint(
        checkpoint_path,
        fingerprint=str(request["fingerprint"]),
        plan=plan,
    )
    completed: dict[str, object] = dict(checkpoint.get("completed", {}))
    model = None
    temporary_directory = checkpoint_path.parent / f"chunks-{os.getpid()}"
    temporary_directory.mkdir(parents=True, exist_ok=True)
    try:
        for index, start, end in plan:
            key = str(index)
            if key in completed:
                continue
            if model is None:
                model = WhisperModel(
                    str(model_path),
                    device=str(request.get("device", "cuda")),
                    compute_type=str(request.get("compute_type", "float16")),
                    local_files_only=True,
                )
            chunk_path = temporary_directory / f"chunk-{index:05d}.wav"
            subprocess.run(
                [
                    str(request["ffmpeg_path"]),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-protocol_whitelist",
                    "file,crypto,data",
                    "-ss",
                    f"{start:.6f}",
                    "-i",
                    str(audio_path),
                    "-t",
                    f"{end - start:.6f}",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(chunk_path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=True,
                timeout=max(120, (end - start) * 2),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            vad_parameters = request.get("vad_parameters")
            if not isinstance(vad_parameters, dict):
                raise RuntimeError("ASR request has no valid VAD parameter set")
            segments, info = model.transcribe(
                str(chunk_path),
                language=request.get("language"),
                beam_size=int(request.get("beam_size", 5)),
                vad_filter=True,
                vad_parameters=vad_parameters,
                word_timestamps=True,
                condition_on_previous_text=False,
                chunk_length=30,
                multilingual=True,
            )
            owner_start = start if index == 0 else start + overlap_seconds / 2
            owner_end = end if index == len(plan) - 1 else end - overlap_seconds / 2
            kept: list[dict[str, object]] = []
            for segment in segments:
                source_start = max(start, start + float(segment.start))
                source_end = min(end, start + float(segment.end))
                midpoint = (source_start + source_end) / 2
                if not owner_start <= midpoint < owner_end:
                    continue
                words: list[dict[str, object]] = []
                for word in segment.words or ():
                    words.append(
                        {
                            "start": max(source_start, start + float(word.start)),
                            "end": min(source_end, start + float(word.end)),
                            "text": word.word,
                            "probability": word.probability,
                        }
                    )
                kept.append(
                    {
                        "start": source_start,
                        "end": source_end,
                        "text": segment.text.strip(),
                        "language": info.language,
                        "avg_log_probability": segment.avg_logprob,
                        "no_speech_probability": segment.no_speech_prob,
                        "words": words,
                    }
                )
            completed[key] = {
                "index": index,
                "start": start,
                "end": end,
                "language": info.language,
                "language_probability": info.language_probability,
                "segments": kept,
            }
            checkpoint["completed"] = completed
            _write_atomic(checkpoint_path, checkpoint)
            chunk_path.unlink(missing_ok=True)
    finally:
        model = None
        shutil.rmtree(temporary_directory, ignore_errors=True)
    ordered_chunks = [completed[str(index)] for index, _, _ in plan]
    all_segments = [segment for chunk in ordered_chunks for segment in chunk["segments"]]
    all_segments = _stitch_segments(all_segments, duration)
    return {
        "schema_version": 1,
        "segments": all_segments,
        "chunks": ordered_chunks,
        "checkpoint_path": str(checkpoint_path),
        "model_path": str(model_path),
        "device": request.get("device", "cuda"),
        "vad_parameters": request["vad_parameters"],
        "compute_type": request.get("compute_type", "float16"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    arguments = parser.parse_args()
    request = json.loads(arguments.request.read_text(encoding="utf-8"))
    output = Path(str(request["output_path"])).resolve()
    _write_atomic(output, transcribe(request))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

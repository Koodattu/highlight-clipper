from __future__ import annotations

import math
import re
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path

from ..domain import ProposalCategory, fingerprint

CATEGORY_TERMS: dict[ProposalCategory, frozenset[str]] = {
    ProposalCategory.REACTION: frozenset(
        {"wow", "whoa", "omg", "clutch", "scare", "what", "mitä", "huh", "apua", "säikähdin"}
    ),
    ProposalCategory.COMEDY: frozenset({"haha", "funny", "joke", "laugh", "lol", "hauska", "vitsi", "naur", "hups"}),
    ProposalCategory.STORY: frozenset(
        {"once", "remember", "story", "then", "finally", "kerran", "muistan", "sitten", "lopulta"}
    ),
    ProposalCategory.OPINION: frozenset(
        {
            "think",
            "believe",
            "should",
            "best",
            "worst",
            "mielestä",
            "uskon",
            "pitäisi",
            "paras",
            "huonoin",
        }
    ),
    ProposalCategory.EXPLANATION: frozenset(
        {
            "because",
            "means",
            "how",
            "reason",
            "therefore",
            "koska",
            "tarkoittaa",
            "miten",
            "syy",
            "joten",
        }
    ),
}

CATEGORY_EMBEDDING_QUERIES: dict[ProposalCategory, str] = {
    ProposalCategory.REACTION: (
        "A self-contained reaction, clutch, scare, surprising failure, or realization with a clear payoff; "
        "yllättävä reaktio, säikähdys, onnistuminen tai moka."
    ),
    ProposalCategory.COMEDY: (
        "Funny banter, misunderstanding, deadpan joke, escalating absurdity, or a punchline; "
        "hauska keskustelu, väärinkäsitys tai selkeä vitsi."
    ),
    ProposalCategory.STORY: (
        "A coherent story or anecdote with setup, escalation, and resolution; "
        "tarina tai anekdootti, jossa on alku, kehitys ja lopetus."
    ),
    ProposalCategory.OPINION: (
        "A strong understandable opinion, disagreement, recommendation, or quotable argument; "
        "vahva mielipide, perusteltu eriävä näkemys tai suositus."
    ),
    ProposalCategory.EXPLANATION: (
        "A useful explanation, strategy, causal insight, comparison, or concrete takeaway; "
        "hyödyllinen selitys, strategia, syy-seuraus tai opetus."
    ),
}


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFC", text).casefold()
    return " ".join(value.split())


def tokens(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[^\W_]+", normalize_text(text), flags=re.UNICODE))


@dataclass(frozen=True, slots=True)
class TranscriptWindow:
    start_us: int
    end_us: int
    text: str
    segment_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    anchor_us: int
    start_us: int | None
    end_us: int | None
    generator_name: str
    generator_version: str
    local_confidence: float
    category_hint: ProposalCategory | None
    evidence_ids: tuple[str, ...]
    idempotency_key: str


def transcript_window_key(window: TranscriptWindow) -> str:
    return fingerprint(
        {
            "start_us": window.start_us,
            "end_us": window.end_us,
            "segment_ids": window.segment_ids,
            "text": normalize_text(window.text),
        }
    )


def embedding_candidates(
    windows: tuple[TranscriptWindow, ...],
    document_keys: tuple[str, ...],
    query_keys: tuple[str, ...],
    similarities,
    source_end_us: int,
) -> list[CandidateDraft]:
    by_key = {transcript_window_key(window): window for window in windows}
    if set(document_keys) != set(by_key) or len(document_keys) != len(by_key):
        raise ValueError("Embedding documents do not match the deterministic transcript windows")
    source_hours = max(source_end_us / 3_600_000_000, 1 / 60)
    per_query = max(1, math.ceil(5 * source_hours))
    results: list[CandidateDraft] = []
    for query_index, query_key in enumerate(query_keys):
        category_value = query_key.removeprefix("category:")
        try:
            category = ProposalCategory(category_value)
        except ValueError:
            category = None
        ranked = sorted(
            ((float(similarities[query_index][index]), key) for index, key in enumerate(document_keys)),
            key=lambda item: (-item[0], by_key[item[1]].start_us, item[1]),
        )[:per_query]
        for score, key in ranked:
            window = by_key[key]
            results.append(
                CandidateDraft(
                    anchor_us=(window.start_us + window.end_us) // 2,
                    start_us=window.start_us,
                    end_us=window.end_us,
                    generator_name="multilingual-embedding-query",
                    generator_version="1",
                    local_confidence=score,
                    category_hint=category,
                    evidence_ids=window.segment_ids,
                    idempotency_key=fingerprint(
                        {
                            "query_key": query_key,
                            "window_key": key,
                            "embedding_generator_version": 1,
                        }
                    ),
                )
            )
    return results


def build_transcript_windows(
    segments: list[dict[str, object]], targets_seconds: tuple[int, ...] = (20, 45, 90, 180)
) -> tuple[TranscriptWindow, ...]:
    if not segments:
        return ()
    ordered = sorted(segments, key=lambda row: (int(row["start_us"]), str(row["id"])))
    windows: list[TranscriptWindow] = []
    seen: set[tuple[int, int, tuple[str, ...]]] = set()
    for target_seconds in targets_seconds:
        target_us = target_seconds * 1_000_000
        stride_us = max(1, target_us // 2)
        next_start = 0
        for index, segment in enumerate(ordered):
            segment_start = int(segment["start_us"])
            if index and segment_start < next_start:
                continue
            end_index = index
            while end_index + 1 < len(ordered) and int(ordered[end_index]["end_us"]) - segment_start < target_us:
                end_index += 1
            selected = ordered[index : end_index + 1]
            ids = tuple(str(item["id"]) for item in selected)
            key = (int(selected[0]["start_us"]), int(selected[-1]["end_us"]), ids)
            if key not in seen:
                seen.add(key)
                windows.append(
                    TranscriptWindow(
                        start_us=key[0],
                        end_us=key[1],
                        text=" ".join(str(item["content"]) for item in selected),
                        segment_ids=ids,
                    )
                )
            next_start = segment_start + stride_us
    return tuple(sorted(windows, key=lambda window: (window.start_us, window.end_us, window.segment_ids)))


def lexical_candidates(windows: tuple[TranscriptWindow, ...], source_end_us: int) -> list[CandidateDraft]:
    source_hours = max(source_end_us / 3_600_000_000, 1 / 60)
    hard_cap = max(1, math.ceil(50 * source_hours))
    per_category = max(1, hard_cap // len(ProposalCategory))
    candidates: list[CandidateDraft] = []
    for category, terms in CATEGORY_TERMS.items():
        scored: list[tuple[float, TranscriptWindow]] = []
        for window in windows:
            window_tokens = tokens(window.text)
            overlap = window_tokens & terms
            if not overlap:
                continue
            score = len(overlap) / math.sqrt(max(1, len(window_tokens)))
            scored.append((score, window))
        for score, window in sorted(scored, key=lambda item: (-item[0], item[1].start_us))[:per_category]:
            anchor = (window.start_us + window.end_us) // 2
            key_data = {
                "category": category.value,
                "start_us": window.start_us,
                "end_us": window.end_us,
                "evidence": window.segment_ids,
            }
            candidates.append(
                CandidateDraft(
                    anchor_us=anchor,
                    start_us=window.start_us,
                    end_us=window.end_us,
                    generator_name="lexical-category",
                    generator_version="1",
                    local_confidence=score,
                    category_hint=category,
                    evidence_ids=window.segment_ids,
                    idempotency_key=fingerprint(key_data),
                )
            )
    return sorted(
        candidates,
        key=lambda item: (
            item.category_hint.value if item.category_hint else "",
            -item.local_confidence,
            item.anchor_us,
        ),
    )[:hard_cap]


def novelty_candidates(windows: tuple[TranscriptWindow, ...], source_end_us: int) -> list[CandidateDraft]:
    if not windows:
        return []
    document_frequency: dict[str, int] = {}
    window_tokens: list[frozenset[str]] = []
    for window in windows:
        current = tokens(window.text)
        window_tokens.append(current)
        for token in current:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    scored: list[tuple[float, TranscriptWindow]] = []
    for window, current in zip(windows, window_tokens, strict=True):
        if not current:
            continue
        rarity = sum(1 / document_frequency[token] for token in current) / math.sqrt(len(current))
        scored.append((rarity, window))
    source_hours = max(source_end_us / 3_600_000_000, 1 / 60)
    limit = max(1, math.ceil(5 * source_hours))
    result: list[CandidateDraft] = []
    for score, window in sorted(scored, key=lambda item: (-item[0], item[1].start_us))[:limit]:
        result.append(
            CandidateDraft(
                anchor_us=(window.start_us + window.end_us) // 2,
                start_us=window.start_us,
                end_us=window.end_us,
                generator_name="transcript-novelty",
                generator_version="1",
                local_confidence=score,
                category_hint=None,
                evidence_ids=window.segment_ids,
                idempotency_key=fingerprint(
                    {
                        "start_us": window.start_us,
                        "end_us": window.end_us,
                        "evidence": window.segment_ids,
                    }
                ),
            )
        )
    return result


def speech_activity_candidates(
    segments: list[dict[str, object]],
    words: list[dict[str, object]],
    source_end_us: int,
    evidence_factory,
    *,
    bin_seconds: int = 5,
    normalization_window_seconds: int = 300,
) -> tuple[list[CandidateDraft], list[dict[str, object]]]:
    if bin_seconds <= 0 or normalization_window_seconds < bin_seconds:
        raise ValueError("Speech activity binning parameters are invalid")
    if source_end_us <= 0:
        return [], []
    bin_us = bin_seconds * 1_000_000
    bin_count = max(1, math.ceil(source_end_us / bin_us))
    speech_us = [0] * bin_count
    word_counts = [0] * bin_count
    pause_before_us = [0] * bin_count

    ordered_segments = sorted(
        segments,
        key=lambda item: (int(item["start_us"]), int(item["end_us"]), str(item.get("id", ""))),
    )
    previous_end_us = 0
    for segment in ordered_segments:
        start_us = max(0, int(segment["start_us"]))
        end_us = min(source_end_us, int(segment["end_us"]))
        if end_us <= start_us:
            continue
        if start_us > previous_end_us:
            pause_before_us[min(bin_count - 1, start_us // bin_us)] = max(
                pause_before_us[min(bin_count - 1, start_us // bin_us)],
                start_us - previous_end_us,
            )
        previous_end_us = max(previous_end_us, end_us)
        first_bin = start_us // bin_us
        last_bin = (end_us - 1) // bin_us
        for index in range(first_bin, last_bin + 1):
            bin_start_us = index * bin_us
            bin_end_us = min(source_end_us, bin_start_us + bin_us)
            speech_us[index] += max(0, min(end_us, bin_end_us) - max(start_us, bin_start_us))

    for word in words:
        midpoint_us = (int(word["start_us"]) + int(word["end_us"])) // 2
        if 0 <= midpoint_us < source_end_us:
            word_counts[min(bin_count - 1, midpoint_us // bin_us)] += 1

    raw: list[dict[str, object]] = []
    for index in range(bin_count):
        start_us = index * bin_us
        end_us = min(source_end_us, start_us + bin_us)
        speech_seconds = speech_us[index] / 1_000_000
        raw.append(
            {
                "start_us": start_us,
                "end_us": end_us,
                "speech_ratio": speech_us[index] / max(1, end_us - start_us),
                "word_count": word_counts[index],
                "speech_rate_words_per_second": word_counts[index] / speech_seconds if speech_seconds else 0.0,
                "pause_before_seconds": pause_before_us[index] / 1_000_000,
            }
        )

    bins_per_block = max(1, normalization_window_seconds // bin_seconds)
    scored: list[dict[str, object]] = []
    prior_ratio = 0.0
    for block_start in range(0, len(raw), bins_per_block):
        block = raw[block_start : block_start + bins_per_block]
        rates = sorted(float(item["speech_rate_words_per_second"]) for item in block)
        changes: list[float] = []
        for item in block:
            ratio = float(item["speech_ratio"])
            changes.append(abs(ratio - prior_ratio))
            prior_ratio = ratio
        ordered_changes = sorted(changes)
        rate_median = rates[len(rates) // 2]
        rate_deviations = sorted(abs(value - rate_median) for value in rates)
        rate_mad = rate_deviations[len(rate_deviations) // 2] or 0.25
        change_median = ordered_changes[len(ordered_changes) // 2]
        change_deviations = sorted(abs(value - change_median) for value in ordered_changes)
        change_mad = change_deviations[len(change_deviations) // 2] or 0.05
        for item, change in zip(block, changes, strict=True):
            rate_z = (float(item["speech_rate_words_per_second"]) - rate_median) / rate_mad
            change_z = (change - change_median) / change_mad
            pause_score = float(item["pause_before_seconds"]) / 2.0
            scored.append(
                {
                    **item,
                    "speech_rate_local_z": rate_z,
                    "speech_activity_change": change,
                    "speech_activity_change_local_z": change_z,
                    "local_score": max(rate_z, change_z, pause_score),
                }
            )

    source_hours = max(source_end_us / 3_600_000_000, 1 / 60)
    limit = max(1, math.ceil(8 * source_hours))
    selected = [item for item in scored if float(item["local_score"]) >= 1.0]
    selected.sort(key=lambda item: (-float(item["local_score"]), int(item["start_us"])))
    observations: list[dict[str, object]] = []
    candidates: list[CandidateDraft] = []
    for item in selected[:limit]:
        evidence_id = evidence_factory(item)
        observation = {**item, "evidence_id": evidence_id}
        observations.append(observation)
        candidates.append(
            CandidateDraft(
                anchor_us=(int(item["start_us"]) + int(item["end_us"])) // 2,
                start_us=int(item["start_us"]),
                end_us=int(item["end_us"]),
                generator_name="speech-activity-change",
                generator_version="1",
                local_confidence=float(item["local_score"]),
                category_hint=None,
                evidence_ids=(evidence_id,),
                idempotency_key=fingerprint(
                    {
                        "start_us": item["start_us"],
                        "end_us": item["end_us"],
                        "version": 1,
                    }
                ),
            )
        )
    return candidates, observations


def audio_peak_candidates(
    audio_path: Path,
    source_end_us: int,
    evidence_factory,
    *,
    normalization_window_seconds: int = 300,
) -> tuple[list[CandidateDraft], list[dict[str, object]]]:
    if normalization_window_seconds <= 0:
        raise ValueError("Audio normalization window must be positive")
    with wave.open(str(audio_path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            return [], []
        sample_rate = audio.getframerate()
        frames_per_window = sample_rate
        rms_values: list[tuple[int, float]] = []
        index = 0
        while raw := audio.readframes(frames_per_window):
            if len(raw) < 2:
                break
            samples = memoryview(raw).cast("h")
            mean_square = sum(int(value) * int(value) for value in samples) / len(samples)
            rms_values.append((index * 1_000_000, math.sqrt(mean_square)))
            index += 1
    if not rms_values:
        return [], []
    deltas = [0.0]
    deltas.extend(abs(current[1] - prior[1]) for prior, current in zip(rms_values, rms_values[1:], strict=False))
    scored_values: list[tuple[int, float, float, float, float, float, float, float]] = []
    for block_start in range(0, len(rms_values), normalization_window_seconds):
        block = rms_values[block_start : block_start + normalization_window_seconds]
        block_deltas = deltas[block_start : block_start + normalization_window_seconds]
        ordered_values = sorted(value for _, value in block)
        median = ordered_values[len(ordered_values) // 2]
        deviations = sorted(abs(value - median) for value in ordered_values)
        mad = deviations[len(deviations) // 2] or 1.0
        ordered_deltas = sorted(block_deltas)
        delta_median = ordered_deltas[len(ordered_deltas) // 2]
        delta_deviations = sorted(abs(value - delta_median) for value in ordered_deltas)
        delta_mad = delta_deviations[len(delta_deviations) // 2] or 1.0
        for offset, (start_us, rms) in enumerate(block):
            delta = block_deltas[offset]
            rms_z = (rms - median) / mad
            delta_z = (delta - delta_median) / delta_mad
            scored_values.append((start_us, rms, median, mad, delta, delta_median, delta_mad, max(rms_z, delta_z)))
    source_hours = max(source_end_us / 3_600_000_000, 1 / 60)
    limit = max(1, math.ceil(10 * source_hours))
    peaks = sorted(scored_values, key=lambda item: (-item[-1], item[0]))[:limit]
    candidates: list[CandidateDraft] = []
    observations: list[dict[str, object]] = []
    for start_us, rms, median, mad, delta, delta_median, delta_mad, score in peaks:
        end_us = min(source_end_us, start_us + 1_000_000)
        if end_us <= start_us:
            continue
        evidence_id = evidence_factory(start_us, end_us, rms, median, mad, delta, delta_median, delta_mad)
        observations.append(
            {
                "evidence_id": evidence_id,
                "start_us": start_us,
                "end_us": end_us,
                "rms": rms,
                "local_z": score,
                "rms_z": (rms - median) / mad,
                "energy_change": delta,
                "energy_change_z": (delta - delta_median) / delta_mad,
            }
        )
        candidates.append(
            CandidateDraft(
                anchor_us=(start_us + end_us) // 2,
                start_us=start_us,
                end_us=end_us,
                generator_name="audio-energy-peak",
                generator_version="2",
                local_confidence=score,
                category_hint=ProposalCategory.REACTION,
                evidence_ids=(evidence_id,),
                idempotency_key=fingerprint({"start_us": start_us, "end_us": end_us, "version": 2}),
            )
        )
    return candidates, observations

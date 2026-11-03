"""Shared DSP/cue scoring adapted from experiments/time-map/candidate_cues.py.

A phase anchor may be estimated; its review status belongs to TrackAnalysis.
No trained model or experiment module is imported here.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
import subprocess
import numpy as np

SAMPLE_RATE = 44_100
FEATURE_VERSION = "bar-cue-candidates-v2"
FRAME_SIZE = 2048
HOP_SIZE = 1024
CONTEXT_BARS = 4
MIN_SEPARATION_BARS = 8
DEFAULT_LIMIT = 5
REVIEW_CLIP_VERSION = "v2"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_mono(source: Path) -> np.ndarray:
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(source),
        "-f",
        "f32le",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "pipe:1",
    ]
    completed = subprocess.run(command, check=True, capture_output=True)
    return np.frombuffer(completed.stdout, dtype="<f4").copy()


def frame_features(audio: np.ndarray) -> dict[str, np.ndarray]:
    if len(audio) < FRAME_SIZE:
        raise ValueError("audio is shorter than one analysis frame")
    starts = np.arange(0, len(audio) - FRAME_SIZE + 1, HOP_SIZE)
    frequencies = np.fft.rfftfreq(FRAME_SIZE, 1 / SAMPLE_RATE)
    audible = (frequencies >= 35) & (frequencies < 8_000)
    low = (frequencies >= 35) & (frequencies < 200)
    window = np.hanning(FRAME_SIZE)
    rms_parts: list[np.ndarray] = []
    low_ratio_parts: list[np.ndarray] = []
    flux_parts: list[np.ndarray] = []
    previous_log_magnitude: np.ndarray | None = None

    for batch_start in range(0, len(starts), 512):
        batch_starts = starts[batch_start : batch_start + 512]
        frames = np.stack(
            [audio[start : start + FRAME_SIZE] for start in batch_starts]
        ).astype(np.float64, copy=False)
        rms_parts.append(np.sqrt(np.mean(np.square(frames), axis=1)))
        magnitude = np.abs(np.fft.rfft(frames * window, axis=1))
        power = np.square(magnitude)
        low_power = np.sum(power[:, low], axis=1)
        audible_power = np.sum(power[:, audible], axis=1)
        low_ratio_parts.append(low_power / np.maximum(audible_power, 1e-12))

        log_magnitude = np.log1p(magnitude[:, audible])
        if previous_log_magnitude is None:
            difference = np.diff(log_magnitude, axis=0, prepend=log_magnitude[:1])
        else:
            difference = np.diff(
                log_magnitude,
                axis=0,
                prepend=previous_log_magnitude[np.newaxis, :],
            )
        flux_parts.append(np.mean(np.maximum(difference, 0.0), axis=1))
        previous_log_magnitude = log_magnitude[-1]

    return {
        "seconds": (starts + FRAME_SIZE / 2) / SAMPLE_RATE,
        "rms": np.concatenate(rms_parts),
        "low_ratio": np.concatenate(low_ratio_parts),
        "positive_flux": np.concatenate(flux_parts),
    }


def robust_unit(values: np.ndarray) -> np.ndarray:
    low, high = np.percentile(values, [10, 90])
    if high - low < 1e-12:
        return np.zeros_like(values)
    return np.clip((values - low) / (high - low), 0.0, 1.0)


def downbeat_phase(analysis: dict, annotations: dict) -> tuple[int, dict]:
    anchors = annotations.get("downbeat_anchors", [])
    if not anchors:
        raise ValueError("a 4/4 downbeat anchor is required")
    anchor = anchors[0]
    if anchor.get("meter") != "4/4":
        raise ValueError("this experiment currently supports 4/4 tracks only")
    anchor_index = int(anchor["beat_index_zero_based"])
    beats = analysis["rhythm"]["beats_seconds"]
    if not 0 <= anchor_index < len(beats):
        raise ValueError("downbeat anchor beat index is outside the beat map")
    return anchor_index % 4, anchor


def aggregate_bars(
    analysis: dict,
    annotations: dict,
    features: dict[str, np.ndarray],
) -> list[dict]:
    beats = analysis["rhythm"]["beats_seconds"]
    phase, anchor = downbeat_phase(analysis, annotations)
    anchor_index = int(anchor["beat_index_zero_based"])
    downbeat_indices = list(range(phase, len(beats), 4))
    bars: list[dict] = []
    for beat_index, next_beat_index in zip(downbeat_indices, downbeat_indices[1:]):
        start = float(beats[beat_index])
        end = float(beats[next_beat_index])
        mask = (features["seconds"] >= start) & (features["seconds"] < end)
        if not np.any(mask):
            continue
        bars.append(
            {
                "bar_index": len(bars),
                "relative_bar": (beat_index - anchor_index) // 4,
                "beat_index": beat_index,
                "start_seconds": start,
                "end_seconds": end,
                "rms": float(np.mean(features["rms"][mask])),
                "low_ratio": float(np.mean(features["low_ratio"][mask])),
                "positive_flux": float(np.mean(features["positive_flux"][mask])),
            }
        )
    if len(bars) < CONTEXT_BARS * 2 + 1:
        raise ValueError("not enough complete bars for cue analysis")
    for name in ("rms", "low_ratio", "positive_flux"):
        normalized = robust_unit(np.array([bar[name] for bar in bars]))
        for bar, value in zip(bars, normalized):
            bar[f"{name}_normalized"] = float(value)
    return bars


def candidate_at(bars: list[dict], index: int) -> dict:
    before = bars[index - CONTEXT_BARS : index]
    after = bars[index : index + CONTEXT_BARS]

    def mean(rows: list[dict], key: str) -> float:
        return float(np.mean([row[key] for row in rows]))

    before_energy = mean(before, "rms_normalized")
    after_energy = mean(after, "rms_normalized")
    before_low = mean(before, "low_ratio_normalized")
    after_low = mean(after, "low_ratio_normalized")
    before_flux = mean(before, "positive_flux_normalized")
    after_flux = mean(after, "positive_flux_normalized")
    boundary = float(
        np.mean(
            [
                abs(after_energy - before_energy),
                abs(after_low - before_low),
                abs(
                    after_flux - before_flux
                ),
            ]
        )
    )
    later_energy = mean(after[1:], "rms_normalized")
    entry_growth = max(0.0, later_energy - after[0]["rms_normalized"])
    energy_rise = max(0.0, after_energy - before_energy)
    bass_rise = max(0.0, after_low - before_low)
    flux_rise = max(0.0, after_flux - before_flux)
    energy_drop = max(0.0, before_energy - after_energy)
    bass_drop = max(0.0, before_low - after_low)
    energy_values = np.array([bar["rms_normalized"] for bar in after])
    flux_values = np.array([bar["positive_flux_normalized"] for bar in after])
    energy_slope = float(
        np.clip(np.polyfit(np.arange(len(after)), energy_values, 1)[0] * 3, -1, 1)
    )
    flux_slope = float(
        np.clip(np.polyfit(np.arange(len(after)), flux_values, 1)[0] * 3, -1, 1)
    )
    energy_range = float(np.ptp(energy_values))
    blend_score = (
        0.25 * (1.0 - after_low)
        + 0.20 * after_flux
        + 0.20 * (1.0 - after_energy)
        + 0.20 * (1.0 - energy_range)
        + 0.15 * boundary
    )
    drop_score = (
        0.30 * boundary
        + 0.25 * after_flux
        + 0.20 * after_energy
        + 0.15 * energy_rise
        + 0.10 * bass_rise
    )
    build_score = (
        0.30 * max(0.0, energy_slope)
        + 0.20 * max(0.0, flux_slope)
        + 0.20 * (1.0 - after[0]["rms_normalized"])
        + 0.15 * (1.0 - after[0]["low_ratio_normalized"])
        + 0.15 * boundary
    )
    entry_scores = {
        "blend": blend_score,
        "drop": drop_score,
        "build": build_score,
    }
    entry_type = max(entry_scores, key=entry_scores.get)
    entry_score = entry_scores[entry_type]
    exit_score = (
        0.30 * boundary
        + 0.20 * energy_drop
        + 0.20 * bass_drop
        + 0.15 * after_flux
        + 0.15 * (1.0 - after_low)
    )
    row = bars[index]
    return {
        "bar_index": row["bar_index"],
        "relative_bar": row["relative_bar"],
        "beat_index": row["beat_index"],
        "time_seconds": row["start_seconds"],
        "context_bars": CONTEXT_BARS,
        "boundary_change": boundary,
        "before_energy": before_energy,
        "after_energy": after_energy,
        "before_low_ratio": before_low,
        "after_low_ratio": after_low,
        "after_positive_flux": after_flux,
        "entry_growth": entry_growth,
        "entry_type": entry_type,
        "blend_score": blend_score,
        "drop_score": drop_score,
        "build_score": build_score,
        "energy_rise": energy_rise,
        "bass_rise": bass_rise,
        "flux_rise": flux_rise,
        "future_energy_slope": energy_slope,
        "future_flux_slope": flux_slope,
        "future_energy_range": energy_range,
        "entry_scores": entry_scores,
        "primary_entry_type": entry_type,
        "energy_drop": energy_drop,
        "bass_drop": bass_drop,
        "entry_score": entry_score,
        "exit_score": exit_score,
        "semantic_label": None,
        "semantic_label_source": None,
        "phrase_boundary_confirmed": False,
    }


def select_diverse(
    candidates: list[dict], role: str, limit: int = DEFAULT_LIMIT
) -> list[dict]:
    score_key = f"{role}_score"
    ranked = sorted(candidates, key=lambda item: item[score_key], reverse=True)
    selected: list[dict] = []
    for candidate in ranked:
        if all(
            abs(candidate["bar_index"] - existing["bar_index"])
            >= MIN_SEPARATION_BARS
            for existing in selected
        ):
            selected.append(candidate)
            if len(selected) == limit:
                break
    return [
        {
            **candidate,
            "role": role,
            "score": candidate[score_key],
            "rank": rank,
            "selection_source": "heuristic_rank",
        }
        for rank, candidate in enumerate(selected, start=1)
    ]


def include_control_candidates(
    selected: list[dict], bars: list[dict], role: str, beat_indexes: list[int]
) -> list[dict]:
    by_beat = {bar["beat_index"]: index for index, bar in enumerate(bars)}
    score_key = f"{role}_score"
    for beat_index in beat_indexes:
        existing = next(
            (item for item in selected if item["beat_index"] == beat_index), None
        )
        if existing is not None:
            existing["selection_source"] = "heuristic_rank_and_explicit_control"
            continue
        if beat_index not in by_beat:
            raise ValueError(f"control beat {beat_index} is not a downbeat")
        bar_index = by_beat[beat_index]
        if not CONTEXT_BARS <= bar_index < len(bars) - CONTEXT_BARS:
            raise ValueError(f"control beat {beat_index} lacks four-bar context")
        candidate = candidate_at(bars, bar_index)
        selected.append(
            {
                **candidate,
                "role": role,
                "score": candidate[score_key],
                "rank": len(selected) + 1,
                "selection_source": "explicit_control",
            }
        )
    return selected


def build_candidates(
    bars: list[dict],
    limit: int = DEFAULT_LIMIT,
    include_entry_beats: list[int] | None = None,
    include_exit_beats: list[int] | None = None,
) -> dict:
    start = max(CONTEXT_BARS, round(len(bars) * 0.10))
    end = min(len(bars) - CONTEXT_BARS, round(len(bars) * 0.90))
    candidates = [candidate_at(bars, index) for index in range(start, end)]
    entry = select_diverse(candidates, "entry", limit)
    exit_candidates = select_diverse(candidates, "exit", limit)
    return {
        "entry": include_control_candidates(
            entry, bars, "entry", include_entry_beats or []
        ),
        "exit": include_control_candidates(
            exit_candidates, bars, "exit", include_exit_beats or []
        ),
    }

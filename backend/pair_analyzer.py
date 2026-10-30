"""Deterministically score analyzed exit/entry cue pairs for later AI selection."""

from __future__ import annotations

from statistics import mean
from typing import Any


PAIR_VERSION = "cue-pair-v5"
MAX_TEMPO_CHANGE_FRACTION = 0.06
TRANSITION_BAR_OPTIONS = (2, 4, 8)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _median_bar_seconds(track: dict[str, Any]) -> float:
    durations = sorted(
        float(bar["end_seconds"]) - float(bar["start_seconds"])
        for bar in track["bars"]
    )
    if not durations:
        raise ValueError("track has no complete bars")
    middle = len(durations) // 2
    if len(durations) % 2:
        return durations[middle]
    return (durations[middle - 1] + durations[middle]) / 2


def _valid_lengths(
    candidate: dict[str, Any],
    duration_seconds: float,
    bar_seconds: float,
) -> list[int]:
    return [
        bars
        for bars in TRANSITION_BAR_OPTIONS
        if float(candidate["time_seconds"]) + bars * bar_seconds <= duration_seconds
    ]


def _bar_index_for(track: dict[str, Any], beat_index: int) -> int:
    for index, bar in enumerate(track["bars"]):
        if int(bar["beat_index"]) == beat_index:
            return index
    raise ValueError(f"beat {beat_index} is not present in the bar map")


def _transition_metrics(
    source: dict[str, Any],
    exit_candidate: dict[str, Any],
    target: dict[str, Any],
    entry_candidate: dict[str, Any],
    transition_bars: int,
) -> dict[str, Any]:
    source_index = _bar_index_for(source, int(exit_candidate["beat_index"]))
    target_index = _bar_index_for(target, int(entry_candidate["beat_index"]))
    source_after = source["bars"][source_index : source_index + transition_bars + 1]
    target_after = target["bars"][target_index : target_index + transition_bars + 1]
    source_before = source["bars"][
        max(0, source_index - transition_bars) : source_index
    ]
    if (
        len(source_after) != transition_bars + 1
        or len(target_after) != transition_bars + 1
        or not source_before
    ):
        raise ValueError("candidate lacks enough bars for transition simulation")

    def values(rows: list[dict[str, Any]], key: str) -> list[float]:
        return [float(row[key]) for row in rows]

    source_energy_after = values(source_after, "rms_normalized")
    target_energy = values(target_after, "rms_normalized")
    source_low = values(source_after, "low_ratio_normalized")
    target_low = values(target_after, "low_ratio_normalized")
    source_flux_after = values(source_after, "positive_flux_normalized")
    target_flux = values(target_after, "positive_flux_normalized")
    source_energy = mean(values(source_before, "rms_normalized"))
    source_flux = mean(values(source_before, "positive_flux_normalized"))
    weights = [index / transition_bars for index in range(transition_bars + 1)]
    mixed_energy = [
        (1.0 - weight) * source_value + weight * target_value
        for weight, source_value, target_value in zip(
            weights, source_energy_after, target_energy
        )
    ]
    bass_conflict = [
        4.0 * (1.0 - weight) * source_value * weight * target_value
        for weight, source_value, target_value in zip(
            weights, source_low, target_low
        )
    ]
    energy_continuity = _clamp(
        1.0 - mean(abs(value - source_energy) for value in mixed_energy)
    )
    energy_valley_avoidance = _clamp(
        1.0 - max(0.0, source_energy - min(mixed_energy))
    )
    low_frequency_separation = _clamp(1.0 - mean(bass_conflict))
    drum_continuity = _clamp(1.0 - abs(source_flux - target_flux[-1]))
    rise_to_handoff = _clamp(target_energy[-1] - target_energy[0])
    boundary = _clamp(float(entry_candidate.get("boundary_change", 0.0)))
    strategy_scores = {
        "maintain_energy": _clamp(
            0.35 * energy_continuity
            + 0.25 * energy_valley_avoidance
            + 0.20 * drum_continuity
            + 0.20 * low_frequency_separation
        ),
        "clean_break": _clamp(
            0.30 * (1.0 - target_energy[-1])
            + 0.25 * (1.0 - target_low[-1])
            + 0.25 * low_frequency_separation
            + 0.20 * boundary
        ),
        "impact_at_handoff": _clamp(
            0.30 * target_energy[-1]
            + 0.25 * target_flux[-1]
            + 0.25 * rise_to_handoff
            + 0.20 * (1.0 - target_energy[0])
        ),
    }
    return {
        "transition_bars": transition_bars,
        "source_energy_before_exit": source_energy,
        "predicted_mixed_energy": mixed_energy,
        "energy_continuity": energy_continuity,
        "energy_valley_avoidance": energy_valley_avoidance,
        "low_frequency_separation": low_frequency_separation,
        "drum_continuity": drum_continuity,
        "strategy_scores": strategy_scores,
        "best_strategy": max(strategy_scores, key=strategy_scores.get),
    }


def build_pairs(
    source: dict[str, Any],
    target: dict[str, Any],
    limit: int = 25,
) -> dict[str, Any]:
    """Return bounded candidates. This function does not select a DJ plan."""
    if limit < 1:
        raise ValueError("pair limit must be positive")
    source_bpm = float(source["rhythm"]["bpm"])
    target_bpm = float(target["rhythm"]["bpm"])
    target_tempo_change = source_bpm / target_bpm - 1.0
    tempo_compatible = abs(target_tempo_change) <= MAX_TEMPO_CHANGE_FRACTION
    source_bar_seconds = _median_bar_seconds(source)
    target_bar_seconds = _median_bar_seconds(target)
    pairs: list[dict[str, Any]] = []
    clean_break_pairs: list[dict[str, Any]] = []

    for exit_candidate in source["candidates"]["exit"]:
        source_lengths = set(
            _valid_lengths(
                exit_candidate,
                float(source["duration_seconds"]),
                source_bar_seconds,
            )
        )
        for entry_candidate in target["candidates"]["entry"]:
            target_lengths = set(
                _valid_lengths(
                    entry_candidate,
                    float(target["duration_seconds"]),
                    target_bar_seconds,
                )
            )
            lengths = sorted(source_lengths & target_lengths)
            if not lengths:
                continue
            if not tempo_compatible:
                clean_break_pairs.append(
                    {
                        "a_exit": {
                            "rank": int(exit_candidate["rank"]),
                            "beat_index": int(exit_candidate["beat_index"]),
                            "time_seconds": float(exit_candidate["time_seconds"]),
                            "score": float(exit_candidate["score"]),
                        },
                        "b_entry": {
                            "rank": int(entry_candidate["rank"]),
                            "beat_index": int(entry_candidate["beat_index"]),
                            "time_seconds": float(entry_candidate["time_seconds"]),
                            "score": float(entry_candidate["score"]),
                        },
                        "transition_mode": "clean_break",
                        "handoff_options": ["phrase_boundary_cut"],
                        "transition_bar_options": [],
                        "transition_metrics_by_bars": {},
                        "b_tempo_change_fraction": target_tempo_change,
                        "best_strategy_for_two_bars": "clean_break",
                        "heuristic_score": float(
                            0.45 * float(exit_candidate["score"])
                            + 0.45 * float(entry_candidate["score"])
                            + 0.10 * float(entry_candidate.get("boundary_change", 0.0))
                        ),
                    }
                )
                continue
            try:
                metrics_by_bars = {
                    str(bars): _transition_metrics(
                        source,
                        exit_candidate,
                        target,
                        entry_candidate,
                        bars,
                    )
                    for bars in lengths
                }
            except ValueError:
                continue
            baseline = metrics_by_bars.get("2") or metrics_by_bars[str(lengths[0])]
            best_strategy = str(baseline["best_strategy"])
            candidate = {
                    "a_exit": {
                        "rank": int(exit_candidate["rank"]),
                        "beat_index": int(exit_candidate["beat_index"]),
                        "time_seconds": float(exit_candidate["time_seconds"]),
                        "score": float(exit_candidate["score"]),
                    },
                    "b_entry": {
                        "rank": int(entry_candidate["rank"]),
                        "beat_index": int(entry_candidate["beat_index"]),
                        "time_seconds": float(entry_candidate["time_seconds"]),
                        "score": float(entry_candidate["score"]),
                    },
                    "transition_bar_options": lengths,
                    "transition_metrics_by_bars": metrics_by_bars,
                    "b_tempo_change_fraction": target_tempo_change,
                    "transition_mode": (
                        "beat_sync_crossfade" if tempo_compatible else "clean_break"
                    ),
                    "best_strategy_for_two_bars": (
                        best_strategy if tempo_compatible else "clean_break"
                    ),
                    "heuristic_score": float(
                        baseline["strategy_scores"][
                            best_strategy if tempo_compatible else "clean_break"
                        ]
                    ),
                }
            pairs.append(candidate)

    ranked = sorted(
        pairs,
        key=lambda item: float(item["heuristic_score"]),
        reverse=True,
    )
    kept = ranked[:limit]
    for rank, pair in enumerate(kept, start=1):
        pair["rank"] = rank
    clean_break_kept = sorted(
        clean_break_pairs,
        key=lambda item: float(item["heuristic_score"]),
        reverse=True,
    )[:limit]
    for rank, pair in enumerate(clean_break_kept, start=1):
        pair["rank"] = rank
    return {
        "tempo": {
            "a_bpm": source_bpm,
            "b_bpm": target_bpm,
            "target_bpm": source_bpm,
            "b_tempo_change_fraction": target_tempo_change,
            "maximum_tempo_change_fraction": MAX_TEMPO_CHANGE_FRACTION,
            "compatible": tempo_compatible,
        },
        "pairs": kept,
        "clean_break_pairs": clean_break_kept,
        "evaluated_pair_count": len(source["candidates"]["exit"])
        * len(target["candidates"]["entry"]),
    }

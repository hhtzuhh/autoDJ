#!/usr/bin/env python3
"""Replan the queued B-to-C leg from an authoritative live beat."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from plan_lifecycle import PerformanceSnapshot
from queue_service import AnalysisCatalog, QueueSnapshot, load_pair_builder
from meta_planner import ALLOWED_INTENTS as INTENTS, _pair_score as intent_score

BEATS_PER_BAR = 4


def pair_id(pair):
    return (f"pair-{int(pair['rank']):02d}"
            f"-a{int(pair['a_exit']['beat_index'])}"
            f"-b{int(pair['b_entry']['beat_index'])}")


DEFAULT_PLANNING_LEAD_BEATS = 8
DEFAULT_CANDIDATE_LIMIT = 10


@dataclass(frozen=True)
class LiveReplanCandidate:
    candidate_id: str
    pair_id: str
    source_exit_beat_index: int
    target_entry_beat_index: int
    transition_bars: int
    transition_beats: int
    intent_score: float
    source_candidate_provenance: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def expanded_late_pairing(
    source_analysis: dict[str, Any],
    target_analysis: dict[str, Any],
    *,
    limit: int = 100,
) -> tuple[dict[str, Any], set[int]]:
    """Pair normal exits plus analyzed downbeats usable as a late fallback."""
    source = deepcopy(source_analysis)
    original_exit_beats = {
        int(candidate["beat_index"])
        for candidate in source["candidates"]["exit"]
    }
    expanded = list(source["candidates"]["exit"])
    next_rank = len(expanded) + 1
    for candidate in source["candidates"]["entry"]:
        beat = int(candidate["beat_index"])
        if beat in original_exit_beats:
            continue
        fallback = deepcopy(candidate)
        fallback["role"] = "exit"
        fallback["rank"] = next_rank
        fallback["score"] = float(candidate.get("exit_score", candidate["score"]))
        fallback["selection_source"] = "late_replan_analyzed_downbeat_fallback"
        expanded.append(fallback)
        next_rank += 1
    source["candidates"]["exit"] = expanded

    pair_version, builder = load_pair_builder()
    built = builder(source, target_analysis, limit)
    pairing = {
        "schema_version": 1,
        "pair_version": pair_version,
        "provenance": {
            "selection": "deterministic_live_replan_with_late_downbeat_fallback",
            "executable_plan": False,
        },
        "a": {
            "audio_sha256": source_analysis["audio_sha256"],
            "analysis_cache_key": source_analysis["analysis_cache_key"],
        },
        "b": {
            "audio_sha256": target_analysis["audio_sha256"],
            "analysis_cache_key": target_analysis["analysis_cache_key"],
        },
        **built,
    }
    return pairing, original_exit_beats


def enumerate_future_candidates(
    pairing: dict[str, Any],
    *,
    current_source_beat: int,
    source_beat_count: int,
    intent: str,
    planning_lead_beats: int = DEFAULT_PLANNING_LEAD_BEATS,
    original_exit_beats: set[int] | None = None,
) -> tuple[LiveReplanCandidate, ...]:
    if intent not in INTENTS:
        raise ValueError(f"unsupported intent: {intent}")
    if planning_lead_beats < 1:
        raise ValueError("planning lead must be positive")
    earliest_exit = current_source_beat + planning_lead_beats
    original_exit_beats = original_exit_beats or {
        int(pair["a_exit"]["beat_index"]) for pair in pairing["pairs"]
    }
    candidates = []
    for pair in pairing["pairs"]:
        source_exit = int(pair["a_exit"]["beat_index"])
        if source_exit < earliest_exit:
            continue
        provenance = (
            "pair_cache_exit"
            if source_exit in original_exit_beats
            else "late_replan_analyzed_downbeat_fallback"
        )
        for bars_value in pair["transition_bar_options"]:
            bars = int(bars_value)
            beats = bars * BEATS_PER_BAR
            if source_exit + beats >= source_beat_count:
                continue
            identity = {
                "pair_id": pair_id(pair),
                "source_exit_beat_index": source_exit,
                "target_entry_beat_index": int(pair["b_entry"]["beat_index"]),
                "transition_bars": bars,
                "current_source_beat": current_source_beat,
            }
            digest = hashlib.sha256(
                json.dumps(identity, sort_keys=True).encode("utf-8")
            ).hexdigest()[:12]
            candidates.append(
                LiveReplanCandidate(
                    candidate_id=f"live-replan-{digest}",
                    pair_id=identity["pair_id"],
                    source_exit_beat_index=source_exit,
                    target_entry_beat_index=identity["target_entry_beat_index"],
                    transition_bars=bars,
                    transition_beats=beats,
                    intent_score=intent_score(pair, bars, intent),
                    source_candidate_provenance=provenance,
                )
            )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                -item.intent_score,
                item.source_exit_beat_index,
                item.target_entry_beat_index,
                item.transition_bars,
            ),
        )
    )


def build_live_replan_selection(
    previous_selection: dict[str, Any],
    queue: QueueSnapshot,
    performance: PerformanceSnapshot,
    catalog: AnalysisCatalog,
    *,
    intent: str = "maintain_energy",
    planning_lead_beats: int = DEFAULT_PLANNING_LEAD_BEATS,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    planning_started = time.perf_counter()
    if queue.revision != int(previous_selection["queue_revision"]):
        raise ValueError("queue revision changed")
    draft = previous_selection["following_feasible_draft"]
    source_track_id = str(draft["source_track_id"])
    target_track_id = str(draft["target_track_id"])
    adjacent = tuple(zip(queue.ordered_track_ids, queue.ordered_track_ids[1:]))
    if (source_track_id, target_track_id) not in adjacent:
        raise ValueError("following tracks are no longer adjacent in queue")
    source_ref = catalog.tracks.get(source_track_id)
    target_ref = catalog.tracks.get(target_track_id)
    if source_ref is None or target_ref is None:
        raise ValueError("following track analysis is unavailable")

    source_decks = [
        deck
        for deck in performance.decks.values()
        if deck.track_id == source_track_id
        and deck.analysis_cache_key == source_ref.analysis_cache_key
    ]
    if len(source_decks) != 1:
        raise ValueError("live source track is not uniquely loaded")
    source = source_decks[0]
    if source.play is not True:
        raise ValueError("live source deck is not playing")
    if source.beat_index is None:
        raise ValueError("live source beat is unknown")
    target_deck = 2 if source.deck == 1 else 1

    source_analysis = json.loads(
        source_ref.analysis_path.read_text(encoding="utf-8")
    )
    target_analysis = json.loads(
        target_ref.analysis_path.read_text(encoding="utf-8")
    )
    pairing, original_exit_beats = expanded_late_pairing(
        source_analysis, target_analysis
    )
    candidates = enumerate_future_candidates(
        pairing,
        current_source_beat=source.beat_index,
        source_beat_count=int(source_analysis["rhythm"]["beat_count"]),
        intent=intent,
        planning_lead_beats=planning_lead_beats,
        original_exit_beats=original_exit_beats,
    )
    if not candidates:
        raise ValueError("no future B-to-C transition remains after the live beat")
    if candidate_limit < 1:
        raise ValueError("candidate limit must be positive")
    selected = candidates[0]
    transition_start = selected.source_exit_beat_index
    transition_end = transition_start + selected.transition_beats
    plan_identity = {
        "queue_revision": queue.revision,
        "performance_revision": performance.revision,
        "source_track_id": source_track_id,
        "target_track_id": target_track_id,
        "source_deck": source.deck,
        "target_deck": target_deck,
        "candidate_id": selected.candidate_id,
    }
    plan_digest = hashlib.sha256(
        json.dumps(plan_identity, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    old_exit = int(draft["source_exit_beat_index"])
    old_exit_expired = old_exit < source.beat_index + planning_lead_beats
    window_id = f"live-window-{plan_digest}"
    planning_latency_ms = round(
        (time.perf_counter() - planning_started) * 1000,
        3,
    )
    return {
        "schema_version": 1,
        "source_window_id": window_id,
        "queue_revision": queue.revision,
        "selection": "deterministic_live_replan_not_ai",
        "intent": intent,
        "replan_context": {
            "performance_revision": performance.revision,
            "current_source_beat_index": source.beat_index,
            "planning_lead_beats": planning_lead_beats,
            "old_source_exit_beat_index": old_exit,
            "old_exit_expired": old_exit_expired,
            "never_seek_source_backward": True,
            "candidate_count": len(candidates),
            "planning_latency_ms": planning_latency_ms,
            "ranked_candidates": [
                candidate.to_dict() for candidate in candidates[:candidate_limit]
            ],
            "selected_candidate": selected.to_dict(),
        },
        "next_executable_plan": {
            "schema_version": 1,
            "schema_uri": "schemas/dj-transition-plan-v1.schema.json",
            "plan_id": f"live-q{queue.revision}-{plan_digest}",
            "source": "deterministic_live_replanner_not_ai",
            "source_start_mode": "continue_playing",
            "completion_mode": "continue_target",
            "deck_a": {
                "audio_sha256": source_track_id,
                "analysis_cache_key": source_ref.analysis_cache_key,
                "analysis_filename": "track-analysis-v1.json",
                "deck": source.deck,
                "play_start_beat_index": transition_start - planning_lead_beats,
                "transition_start_beat_index": transition_start,
                "transition_end_beat_index": transition_end,
            },
            "deck_b": {
                "audio_sha256": target_track_id,
                "analysis_cache_key": target_ref.analysis_cache_key,
                "analysis_filename": "track-analysis-v1.json",
                "deck": target_deck,
                "cue_beat_index": selected.target_entry_beat_index,
            },
            "mix": {
                "beats": selected.transition_beats,
                "bars": selected.transition_bars,
                "beats_per_bar": BEATS_PER_BAR,
                "crossfader_curve": "linear",
                "steps": selected.transition_beats * 4 + 1,
                "tail_beats": 8,
                "tempo_source": "deck_a_sync_leader",
            },
        },
    }

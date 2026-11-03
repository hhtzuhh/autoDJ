#!/usr/bin/env python3
"""Validate or execute a beat-aligned fixed Mixxx transition plan."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time


from runtime_paths import RUNTIME_ROOT, ANALYSIS_ROOT as DEFAULT_ARTIFACTS

CANCEL_FILE = RUNTIME_ROOT / "cancel"
TAKEOVER_FILE = RUNTIME_ROOT / "takeover.json"
REBASE_FILE = RUNTIME_ROOT / "transition-rebase.json"


class TransitionCancelled(Exception):
    """Raised when the external cancel command invalidates this schedule."""


class TransitionTakenOver(Exception):
    """Raised when a human takes over and current playback must be preserved."""


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_track(plan_track: dict, analysis: dict, beat_fields: list[str]) -> None:
    # Legacy analysis.json nests the audio hash and calls the cache key
    # cache_key. The unified track-analysis-v1 contract keeps both at top level.
    analysis_audio_hash = analysis.get("audio_sha256")
    if analysis_audio_hash is None:
        analysis_audio_hash = analysis.get("source", {}).get("sha256")
    analysis_cache_key = analysis.get("analysis_cache_key")
    if analysis_cache_key is None:
        analysis_cache_key = analysis.get("cache_key")
    if plan_track["audio_sha256"] != analysis_audio_hash:
        raise ValueError("plan audio hash does not match analysis")
    if plan_track["analysis_cache_key"] != analysis_cache_key:
        raise ValueError("plan cache key does not match analysis")
    beats = analysis["rhythm"]["beats_seconds"]
    for field in beat_fields:
        index = plan_track[field]
        if not isinstance(index, int) or index < 0 or index >= len(beats):
            raise ValueError(f"{field} is outside the analyzed beat grid")


def analysis_path(artifacts: Path, plan_track: dict) -> Path:
    filename = plan_track.get("analysis_filename", "analysis.json")
    if Path(filename).name != filename or not filename.endswith(".json"):
        raise ValueError("analysis filename must be a local JSON basename")
    return artifacts / plan_track["audio_sha256"] / filename


def resolve_plan(plan_path: Path, artifacts: Path = DEFAULT_ARTIFACTS) -> dict:
    plan = load_json(plan_path)
    if plan.get("schema_version") != 1:
        raise ValueError("unsupported transition plan schema")
    source_deck = int(plan["deck_a"]["deck"])
    target_deck = int(plan["deck_b"]["deck"])
    if {source_deck, target_deck} != {1, 2}:
        raise ValueError("source and target must use distinct Mixxx decks 1 and 2")
    source_start_mode = plan.get("source_start_mode", "seek_and_start")
    if source_start_mode not in {"seek_and_start", "continue_playing"}:
        raise ValueError("unsupported source start mode")
    completion_mode = plan.get("completion_mode", "stop_and_reset")
    if completion_mode not in {"stop_and_reset", "continue_target"}:
        raise ValueError("unsupported completion mode")

    transition_mode = plan.get("transition_mode", "beat_sync_crossfade")
    if transition_mode not in {"beat_sync_crossfade", "clean_break"}:
        raise ValueError("unsupported transition mode")

    analysis_a = load_json(analysis_path(artifacts, plan["deck_a"]))
    analysis_b = load_json(analysis_path(artifacts, plan["deck_b"]))
    if (plan.get("tempo_routine") or plan.get("in_track_actions")) and "performance_actions" not in plan:
        raise ValueError("tempo routine requires validated performance actions")
    if "performance_actions" in plan:
        from routine_executor import validate_routine_plan
        validate_routine_plan(plan, analysis_a, analysis_b)
    if transition_mode == "clean_break":
        validate_track(
            plan["deck_a"],
            analysis_a,
            ["play_start_beat_index", "transition_start_beat_index"],
        )
        validate_track(plan["deck_b"], analysis_b, ["cue_beat_index"])
        a = plan["deck_a"]
        if a["play_start_beat_index"] >= a["transition_start_beat_index"]:
            raise ValueError("clean-break source cue must precede its exit")
        mix = plan["mix"]
        if mix.get("handoff") != "target_start_then_crossfader_cut_then_source_stop":
            raise ValueError("unsupported clean-break handoff")
        if mix.get("tempo_source") != "native_file_bpm_no_sync":
            raise ValueError("clean break must preserve native tempos")
        fade_out = mix.get("fade_out_seconds", 0.0)
        if not isinstance(fade_out, (int, float)) or isinstance(fade_out, bool) or not math.isfinite(fade_out) or fade_out not in (0.0, 0.5):
            raise ValueError("unsupported clean-break fade duration")
        technique = mix.get("technique", "clean_cut")
        if technique not in {"clean_cut", "fade_out_cut"} or (technique == "fade_out_cut") != (fade_out == 0.5):
            raise ValueError("clean-break technique does not match its controls")
        if any(name in mix for name in ("bass_swap", "bass_dip")):
            raise ValueError("clean break cannot use beat-sync EQ techniques")
        beats_a = analysis_a["rhythm"]["beats_seconds"]
        beats_b = analysis_b["rhythm"]["beats_seconds"]
        start = beats_a[a["play_start_beat_index"]]
        transition_start = beats_a[a["transition_start_beat_index"]]
        if transition_start + fade_out > beats_a[-1]:
            raise ValueError("source has insufficient analyzed audio for fade-out")
        return {
            "plan": plan,
            "source_beats_seconds": beats_a,
            "target_beats_seconds": beats_b,
            "deck_a_start_seconds": start,
            "deck_a_transition_start_seconds": transition_start,
            "deck_a_transition_end_seconds": transition_start + fade_out,
            "deck_b_cue_seconds": beats_b[plan["deck_b"]["cue_beat_index"]],
            "lead_seconds": transition_start - start,
            "fade_seconds": float(fade_out),
            "tail_seconds": 0.0,
            "target_bpm": float(analysis_b["rhythm"]["bpm"]),
            "sample_rate_a": analysis_a["audio"]["sample_rate"],
            "sample_rate_b": analysis_b["audio"]["sample_rate"],
            "source_deck": source_deck,
            "target_deck": target_deck,
            "source_start_mode": source_start_mode,
            "completion_mode": completion_mode,
            "transition_mode": transition_mode,
        }
    validate_track(
        plan["deck_a"],
        analysis_a,
        [
            "play_start_beat_index",
            "transition_start_beat_index",
            "transition_end_beat_index",
        ],
    )
    validate_track(plan["deck_b"], analysis_b, ["cue_beat_index"])

    a = plan["deck_a"]
    mix = plan["mix"]
    if not (
        a["play_start_beat_index"]
        < a["transition_start_beat_index"]
        < a["transition_end_beat_index"]
    ):
        raise ValueError("deck A beat indexes must increase")
    if a["transition_end_beat_index"] - a["transition_start_beat_index"] != mix["beats"]:
        raise ValueError("mix beat count does not match deck A beat indexes")
    if mix["bars"] * mix["beats_per_bar"] != mix["beats"]:
        raise ValueError("mix bars do not match beat count")
    if mix["crossfader_curve"] not in {"linear", "late_cut"} or not isinstance(mix["steps"], int) or not 2 <= mix["steps"] <= 129:
        raise ValueError("executor supports linear/late-cut fades with 2..129 steps")
    validate_mix_technique(mix)
    bass_swap = mix.get("bass_swap")
    if bass_swap is not None:
        start_beat = bass_swap.get("start_beat")
        end_beat = bass_swap.get("end_beat")
        if (
            not isinstance(start_beat, int)
            or not isinstance(end_beat, int)
            or start_beat < 0
            or start_beat >= end_beat
            or end_beat > mix["beats"]
        ):
            raise ValueError("bass swap range must be inside the transition")

    beats_a = analysis_a["rhythm"]["beats_seconds"]
    beats_b = analysis_b["rhythm"]["beats_seconds"]
    start = beats_a[a["play_start_beat_index"]]
    transition_start = beats_a[a["transition_start_beat_index"]]
    transition_end = beats_a[a["transition_end_beat_index"]]
    beat_seconds = (transition_end - transition_start) / mix["beats"]
    resolved = {
        "plan": plan,
        "deck_a_start_seconds": start,
        "deck_a_transition_start_seconds": transition_start,
        "deck_a_transition_end_seconds": transition_end,
        "deck_b_cue_seconds": beats_b[plan["deck_b"]["cue_beat_index"]],
        "lead_seconds": transition_start - start,
        "fade_seconds": transition_end - transition_start,
        "tail_seconds": beat_seconds * mix["tail_beats"],
        "target_bpm": 60.0 / beat_seconds,
        "sample_rate_a": analysis_a["audio"]["sample_rate"],
        "sample_rate_b": analysis_b["audio"]["sample_rate"],
        "source_deck": source_deck,
        "target_deck": target_deck,
        "source_start_mode": source_start_mode,
        "completion_mode": completion_mode,
        "transition_mode": transition_mode,
    }
    if plan.get("tempo_routine"):
        resolved["source_beats_seconds"] = beats_a
        resolved["target_beats_seconds"] = beats_b
        resolved["target_bpm"] = plan["tempo_routine"]["common_bpm"]
    return resolved


def validate_mix_technique(mix: dict) -> None:
    """Reject combinations the compiler never emits, including stale EQ flags."""
    technique = mix.get("technique")
    if technique is not None:
        if technique not in {"crossfade", "bass_swap", "bass_dip", "late_cut"}:
            raise ValueError("unsupported beat-sync technique")
        if (technique == "bass_swap") != ("bass_swap" in mix):
            raise ValueError("technique does not match bass swap controls")
        if (technique == "bass_dip") != (mix.get("bass_dip") is True):
            raise ValueError("technique does not match bass dip controls")
        if (technique == "late_cut") != (mix["crossfader_curve"] == "late_cut"):
            raise ValueError("technique does not match fader controls")
    if "bass_dip" in mix and mix["bass_dip"] is not True:
        raise ValueError("bass dip must be true when supplied")
    if mix.get("bass_dip") and mix.get("bass_swap"):
        raise ValueError("bass dip and bass swap are mutually exclusive")


def mix_controls(mix: dict, progress: float) -> dict:
    """Normalized fader and EQ controls at an already scheduled progress point."""
    progress = max(0.0, min(1.0, progress))
    fader = max(0.0, (progress - (1.0 - 1.0 / mix["beats"])) * mix["beats"]) if mix["crossfader_curve"] == "late_cut" else progress
    row = {"crossfader": min(1.0, fader)}
    if mix.get("bass_dip"):
        region = mix.get("bass_dip_range", {"start_beat": mix["beats"] / 4, "end_beat": mix["beats"] * 3 / 4})
        phase = (progress * mix["beats"] - region["start_beat"]) / (region["end_beat"] - region["start_beat"])
        row.update(source_bass=0.5 * (1 - min(1.0, max(0.0, phase * 2))),
                   target_bass=0.5 * min(1.0, max(0.0, (phase - .5) * 2)))
    elif mix.get("bass_swap"):
        swap = mix["bass_swap"]
        exchanged = min(1.0, max(0.0, (progress * mix["beats"] - swap["start_beat"]) / (swap["end_beat"] - swap["start_beat"])))
        row.update(source_bass=0.5 * (1 - exchanged), target_bass=0.5 * exchanged)
    return row


def mix_progress_points(mix: dict) -> list[float]:
    points = {index / (mix["steps"] - 1) for index in range(mix["steps"])}
    if mix["crossfader_curve"] == "late_cut":
        # Eight subdivisions of the final beat even for an eight-bar mix.
        points.update(1.0 - (1.0 - index / 8) / mix["beats"] for index in range(9))
    return sorted(points)


class RunLog:
    def __init__(self, path: Path, **context):
        self.path = path
        self.context = context

    def write(self, event: str, **details) -> None:
        entry = {
            "event": event,
            "monotonic": time.monotonic(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **self.context,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as destination:
            destination.write(json.dumps(entry, sort_keys=True) + "\n")


def takeover_requested(plan_id: str, path: Path = TAKEOVER_FILE) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("plan_id") == plan_id
        and payload.get("preserve_playback") is True
    )


def wait_until(
    deadline: float,
    *,
    plan_id: str | None = None,
    cancel_path: Path = CANCEL_FILE,
    takeover_path: Path = TAKEOVER_FILE,
) -> None:
    while True:
        if plan_id and takeover_requested(plan_id, takeover_path):
            raise TransitionTakenOver
        if cancel_path.exists():
            raise TransitionCancelled
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.02, remaining))


def wait_until_rebasable_transition(
    deadline: float,
    *,
    plan_id: str,
    transition_position_seconds: float,
    rebase_path: Path = REBASE_FILE,
    cancel_path: Path = CANCEL_FILE,
    takeover_path: Path = TAKEOVER_FILE,
) -> float:
    """Wait for a transition while allowing a safe pre-window source seek."""
    last_observed_monotonic = -1.0
    while True:
        if takeover_requested(plan_id, takeover_path):
            raise TransitionTakenOver
        if cancel_path.exists():
            raise TransitionCancelled
        if rebase_path.exists():
            try:
                payload = load_json(rebase_path)
                observed_at = float(payload["source_observed_monotonic"])
                if (
                    payload.get("plan_id") == plan_id
                    and observed_at > last_observed_monotonic
                ):
                    deadline = observed_at + (
                        transition_position_seconds
                        - float(payload["source_position_seconds"])
                    )
                    last_observed_monotonic = observed_at
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return deadline
        time.sleep(min(0.02, remaining))


def validate_native_status(deck_a: dict, deck_b: dict, tolerance: float = 0.05) -> None:
    for name, status in (("A", deck_a), ("B", deck_b)):
        if status["sync_mode"] != 0:
            raise RuntimeError(f"deck {name} remained in sync mode before preparation")
        if abs(status["bpm"] - status["file_bpm"]) > tolerance:
            raise RuntimeError(f"deck {name} did not return to its file BPM")


def validate_beatmatch_status(
    deck_a: dict, deck_b: dict, tolerance: float = 0.05
) -> None:
    if deck_a["sync_mode"] != 2:
        raise RuntimeError("deck A did not become sync leader")
    if deck_b["sync_mode"] != 1:
        raise RuntimeError("deck B did not become sync follower")
    if abs(deck_a["bpm"] - deck_b["bpm"]) > tolerance:
        raise RuntimeError("deck BPMs do not match after sync preparation")


def crossfader_value(source_deck: int, progress: float) -> float:
    if source_deck not in (1, 2):
        raise ValueError("source deck must be 1 or 2")
    if not 0.0 <= progress <= 1.0:
        raise ValueError("crossfader progress must be between zero and one")
    return progress if source_deck == 1 else 1.0 - progress


def preserve_live_playback_after_failure(
    probe_api: object,
    output: object,
    source_deck: int,
    target_deck: int,
    *,
    source_stopped: bool,
) -> int:
    """Keep one audible deck running when live transition preparation fails."""
    source_target = str(source_deck)
    target_target = str(target_deck)
    probe_api.send_sync_value(output, source_deck, "enabled", False)
    probe_api.send_sync_value(output, target_deck, "enabled", False)
    probe_api.send_value(output, source_target, "rate", 0.0)
    probe_api.send_value(output, target_target, "rate", 0.0)
    probe_api.send_value(output, source_target, "bass", 0.5)
    probe_api.send_value(output, target_target, "bass", 0.5)
    if source_stopped:
        probe_api.send_value(output, target_target, "play", 1.0)
        probe_api.send_value(
            output,
            "master",
            "crossfader",
            crossfader_value(source_deck, 1.0),
        )
        return target_deck
    probe_api.send_value(output, source_target, "play", 1.0)
    probe_api.send_value(output, target_target, "play", 0.0)
    probe_api.send_value(
        output,
        "master",
        "crossfader",
        crossfader_value(source_deck, 0.0),
    )
    return source_deck


def continued_transition_deadline(
    resolved: dict,
    *,
    source_position_seconds: float,
    source_observed_monotonic: float,
    now: float | None = None,
) -> float:
    remaining_track_seconds = (
        float(resolved["deck_a_transition_start_seconds"])
        - source_position_seconds
    )
    deadline = source_observed_monotonic + remaining_track_seconds
    if deadline <= (time.monotonic() if now is None else now):
        raise RuntimeError("continued source already passed the transition start")
    return deadline


def execute_clean_break(
    resolved: dict,
    artifacts: Path,
    record: bool,
    *,
    source_position_seconds: float | None = None,
    source_observed_monotonic: float | None = None,
) -> Path:
    """Execute an instantaneous phrase-boundary handoff without tempo sync."""
    import probe

    plan = resolved["plan"]
    source_deck = int(resolved["source_deck"])
    target_deck = int(resolved["target_deck"])
    continue_source = resolved["source_start_mode"] == "continue_playing"
    if continue_source:
        if source_position_seconds is None or source_observed_monotonic is None:
            raise ValueError("continue_playing requires an observed source position and time")
        transition_start = continued_transition_deadline(
            resolved,
            source_position_seconds=source_position_seconds,
            source_observed_monotonic=source_observed_monotonic,
        )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_directory = artifacts / "runs" / run_id
    run_directory.mkdir(parents=True)
    log = RunLog(run_directory / "transition-events.jsonl", set_run_id=plan.get("set_run_id"), plan_id=plan["plan_id"])
    (run_directory / "resolved-plan.json").write_text(
        json.dumps(resolved, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    CANCEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    CANCEL_FILE.unlink(missing_ok=True)
    log.write("run_started", plan_id=plan["plan_id"], recording=record, transition_mode="clean_break")
    if takeover_requested(plan["plan_id"]):
        TAKEOVER_FILE.unlink(missing_ok=True)
        log.write("run_taken_over_before_start", preserve_playback=True)
        log.write("run_finished")
        return run_directory
    TAKEOVER_FILE.unlink(missing_ok=True)
    human_takeover = False
    transition_completed = False
    source_stopped = False
    if record:
        probe.recording("start")
        log.write("recording_started")
    try:
        if not continue_source:
            probe.precise_seek(source_deck, resolved["deck_a_start_seconds"], resolved["sample_rate_a"], False)
            log.write("deck_a_seeked", seconds=resolved["deck_a_start_seconds"])
        probe.precise_seek(target_deck, resolved["deck_b_cue_seconds"], resolved["sample_rate_b"], False)
        log.write("deck_b_seeked", seconds=resolved["deck_b_cue_seconds"])
        with probe.mido.open_output(probe.port_name()) as output:
            for deck in (source_deck, target_deck):
                probe.send_sync_value(output, deck, "enabled", False)
                probe.send_value(output, str(deck), "rate", 0.0)
                probe.send_value(output, str(deck), "volume", 0.75)
                probe.send_value(output, str(deck), "bass", 0.5)
            probe.send_value(output, "master", "crossfader", crossfader_value(source_deck, 0.0))
            time.sleep(0.05)
            validate_native_status(
                probe.deck_status(source_deck, output=output),
                probe.deck_status(target_deck, output=output),
            )
            if not continue_source:
                probe.send_value(output, str(source_deck), "play", 1.0)
                started = time.monotonic()
                transition_start = started + resolved["lead_seconds"]
                log.write("deck_a_started", scheduled_monotonic=started)
            transition_start = wait_until_rebasable_transition(
                transition_start,
                plan_id=plan["plan_id"],
                transition_position_seconds=float(
                    resolved["deck_a_transition_start_seconds"]
                ),
            )
            fade_out = float(plan["mix"].get("fade_out_seconds", 0.0))
            if fade_out:
                # Target is still stopped: crossfading towards it fades source
                # to silence without overlapping unmatched beats.
                for index in range(17):
                    progress = index / 16
                    wait_until(transition_start + fade_out * progress, plan_id=plan["plan_id"])
                    probe.send_value(output, "master", "crossfader", crossfader_value(source_deck, progress))
                    log.write("fade_out_step", progress=progress)
            probe.send_value(output, str(target_deck), "play", 1.0)
            probe.send_value(output, "master", "crossfader", crossfader_value(source_deck, 1.0))
            probe.send_value(output, str(source_deck), "play", 0.0)
            source_stopped = True
            log.write(
                "clean_break_handoff",
                scheduled_monotonic=transition_start,
                order=["target_started", "crossfader_cut", "source_stopped"],
                technique=plan["mix"].get("technique", "clean_cut"),
                fade_out_seconds=fade_out,
            )
            transition_completed = True
    except TransitionTakenOver:
        human_takeover = True
        log.write("run_taken_over", preserve_playback=True)
    except (TransitionCancelled, probe.TransitionCancelled, KeyboardInterrupt):
        log.write("run_cancelled")
    finally:
        if human_takeover:
            log.write("automation_cleanup_skipped", preserve_playback=True)
        elif transition_completed and resolved["completion_mode"] == "continue_target":
            with probe.mido.open_output(probe.port_name()) as output:
                for deck in (source_deck, target_deck):
                    probe.send_sync_value(output, deck, "enabled", False)
                    probe.send_value(output, str(deck), "rate", 0.0)
                    probe.send_value(output, str(deck), "bass", 0.5)
                probe.send_value(output, "master", "crossfader", crossfader_value(source_deck, 1.0))
            log.write("continuous_target_cleanup", target_playback_preserved=True)
        elif continue_source:
            with probe.mido.open_output(probe.port_name()) as output:
                preserve_live_playback_after_failure(probe, output, source_deck, target_deck, source_stopped=source_stopped)
            log.write("failed_live_transition_cleanup", source_stopped=source_stopped)
        else:
            with probe.mido.open_output(probe.port_name()) as output:
                probe.send_value(output, "1", "play", 0.0)
                probe.send_value(output, "2", "play", 0.0)
        if record:
            probe.recording("stop")
            log.write("recording_stopped")
        CANCEL_FILE.unlink(missing_ok=True)
        TAKEOVER_FILE.unlink(missing_ok=True)
        log.write("run_finished")
    print(f"run output={run_directory}")
    return run_directory


def execute(
    resolved: dict,
    artifacts: Path,
    record: bool,
    *,
    source_position_seconds: float | None = None,
    source_observed_monotonic: float | None = None,
) -> Path:
    if resolved["plan"].get("tempo_routine") or resolved["plan"].get("in_track_actions"):
        from routine_executor import execute as execute_routine
        return execute_routine(resolved, artifacts, record)
    if resolved.get("transition_mode") == "clean_break":
        return execute_clean_break(
            resolved,
            artifacts,
            record,
            source_position_seconds=source_position_seconds,
            source_observed_monotonic=source_observed_monotonic,
        )
    import probe

    plan = resolved["plan"]
    source_deck = int(resolved["source_deck"])
    target_deck = int(resolved["target_deck"])
    source_target = str(source_deck)
    target_target = str(target_deck)
    continue_source = resolved["source_start_mode"] == "continue_playing"
    if continue_source:
        if source_position_seconds is None or source_observed_monotonic is None:
            raise ValueError(
                "continue_playing requires an observed source position and time"
            )
        transition_start = continued_transition_deadline(
            resolved,
            source_position_seconds=source_position_seconds,
            source_observed_monotonic=source_observed_monotonic,
        )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run_directory = artifacts / "runs" / run_id
    run_directory.mkdir(parents=True)
    log = RunLog(run_directory / "transition-events.jsonl", set_run_id=plan.get("set_run_id"), plan_id=plan["plan_id"])
    (run_directory / "resolved-plan.json").write_text(
        json.dumps(resolved, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    CANCEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    CANCEL_FILE.unlink(missing_ok=True)
    log.write("run_started", plan_id=plan["plan_id"], recording=record)
    if takeover_requested(plan["plan_id"]):
        TAKEOVER_FILE.unlink(missing_ok=True)
        log.write("run_taken_over_before_start", preserve_playback=True)
        log.write("run_finished")
        print("transition already taken over; no controls sent", flush=True)
        print(f"run output={run_directory}")
        return run_directory
    TAKEOVER_FILE.unlink(missing_ok=True)
    human_takeover = False
    transition_completed = False
    source_stopped = False

    if record:
        probe.recording("start")
        log.write("recording_started")
    try:
        if not continue_source:
            probe.precise_seek(
                source_deck,
                resolved["deck_a_start_seconds"],
                resolved["sample_rate_a"],
                False,
            )
            log.write("deck_a_seeked", seconds=resolved["deck_a_start_seconds"])
        else:
            log.write(
                "deck_a_continued",
                deck=source_deck,
                observed_position_seconds=source_position_seconds,
                observed_monotonic=source_observed_monotonic,
                transition_start_monotonic=transition_start,
            )
        probe.precise_seek(
            target_deck,
            resolved["deck_b_cue_seconds"],
            resolved["sample_rate_b"],
            False,
        )
        log.write("deck_b_seeked", seconds=resolved["deck_b_cue_seconds"])

        with probe.mido.open_output(probe.port_name()) as output:
            probe.send_value(output, source_target, "volume", 0.75)
            probe.send_value(output, target_target, "volume", 0.75)
            bass_swap = plan["mix"].get("bass_swap")
            probe.send_value(output, source_target, "bass", 0.5)
            probe.send_value(
                output, target_target, "bass", 0.0 if bass_swap or plan["mix"].get("bass_dip") else 0.5
            )
            probe.send_sync_value(output, 1, "enabled", False)
            probe.send_sync_value(output, 2, "enabled", False)
            probe.send_value(output, "1", "rate", 0.0)
            probe.send_value(output, "2", "rate", 0.0)
            probe.send_value(
                output,
                "master",
                "crossfader",
                crossfader_value(source_deck, 0.0),
            )
            time.sleep(0.05)
            native_a = probe.deck_status(source_deck, output=output)
            native_b = probe.deck_status(target_deck, output=output)
            validate_native_status(native_a, native_b)
            log.write("pre_sync_status", deck_a=native_a, deck_b=native_b)

            probe.send_sync_value(output, source_deck, "leader", True)
            probe.send_sync_value(output, target_deck, "enabled", True)
            time.sleep(0.05)
            prepared_a = probe.deck_status(source_deck, output=output)
            prepared_b = probe.deck_status(target_deck, output=output)
            try:
                validate_beatmatch_status(prepared_a, prepared_b)
            except RuntimeError as error:
                if plan.get("allow_clean_cut") is False:
                    log.write("runtime_clean_cut_blocked", reason=str(error))
                    raise RuntimeError("Sync failed; clean-cut fallback is disabled. Source playback is preserved.") from error
                log.write(
                    "beatmatch_downgraded_to_clean_break",
                    reason=str(error),
                    deck_a=prepared_a,
                    deck_b=prepared_b,
                )
                probe.send_sync_value(output, source_deck, "enabled", False)
                probe.send_sync_value(output, target_deck, "enabled", False)
                probe.send_value(output, source_target, "rate", 0.0)
                probe.send_value(output, target_target, "rate", 0.0)
                probe.send_value(output, source_target, "bass", 0.5)
                probe.send_value(output, target_target, "bass", 0.5)
                if not continue_source:
                    probe.send_value(output, source_target, "play", 1.0)
                    transition_start = time.monotonic() + resolved["lead_seconds"]
                transition_start = wait_until_rebasable_transition(
                    transition_start,
                    plan_id=plan["plan_id"],
                    transition_position_seconds=float(
                        resolved["deck_a_transition_start_seconds"]
                    ),
                )
                probe.send_value(output, target_target, "play", 1.0)
                probe.send_value(
                    output,
                    "master",
                    "crossfader",
                    crossfader_value(source_deck, 1.0),
                )
                probe.send_value(output, source_target, "play", 0.0)
                source_stopped = True
                transition_completed = True
                log.write(
                    "runtime_clean_break_completed",
                    source_deck=source_deck,
                    target_deck=target_deck,
                    target_playback_preserved=True,
                )
                return run_directory
            log.write("prepared_sync_status", deck_a=prepared_a, deck_b=prepared_b)

            if not continue_source:
                probe.send_value(output, source_target, "play", 1.0)
                a_started = time.monotonic()
                log.write("deck_a_started", scheduled_monotonic=a_started)
                transition_start = a_started + resolved["lead_seconds"]
            transition_start = wait_until_rebasable_transition(
                transition_start,
                plan_id=plan["plan_id"],
                transition_position_seconds=float(
                    resolved["deck_a_transition_start_seconds"]
                ),
            )
            probe.send_value(output, target_target, "play", 1.0)
            probe.send_sync_value(output, target_deck, "enabled", True)
            log.write("deck_b_started", scheduled_monotonic=transition_start)

            fade_seconds = resolved["fade_seconds"]
            for index, progress in enumerate(mix_progress_points(plan["mix"])):
                scheduled = transition_start + fade_seconds * progress
                wait_until(scheduled, plan_id=plan["plan_id"])
                controls = mix_controls(plan["mix"], progress)
                if "source_bass" in controls:
                    probe.send_value(
                        output,
                        source_target,
                        "bass",
                        controls["source_bass"],
                    )
                    probe.send_value(
                        output, target_target, "bass", controls["target_bass"]
                    )
                    log.write(
                        "bass_ramp_step",
                        progress=progress,
                        deck_a=controls["source_bass"],
                        deck_b=controls["target_bass"],
                    )
                fader_value = crossfader_value(source_deck, controls["crossfader"])
                probe.send_value(output, "master", "crossfader", fader_value)
                log.write(
                    "crossfader_step",
                    index=index,
                    value=fader_value,
                    scheduled_monotonic=scheduled,
                )
            status_a = probe.deck_status(source_deck, output=output)
            status_b = probe.deck_status(target_deck, output=output)
            validate_beatmatch_status(status_a, status_b)
            log.write("beatmatch_status", deck_a=status_a, deck_b=status_b)
            probe.send_value(output, source_target, "play", 0.0)
            source_stopped = True
            log.write("deck_a_stopped")
            wait_until(
                transition_start + fade_seconds + resolved["tail_seconds"],
                plan_id=plan["plan_id"],
            )
            log.write("tail_completed")
            transition_completed = True
    except TransitionTakenOver:
        human_takeover = True
        log.write("run_taken_over", preserve_playback=True)
        print("transition taken over; preserving playback", flush=True)
    except (TransitionCancelled, probe.TransitionCancelled, KeyboardInterrupt):
        log.write("run_cancelled")
        print("transition cancelled", flush=True)
    finally:
        if human_takeover:
            log.write("automation_cleanup_skipped", preserve_playback=True)
        elif (
            transition_completed
            and resolved["completion_mode"] == "continue_target"
        ):
            with probe.mido.open_output(probe.port_name()) as output:
                probe.send_sync_value(output, source_deck, "enabled", False)
                probe.send_sync_value(output, target_deck, "enabled", False)
                probe.send_value(output, source_target, "rate", 0.0)
                probe.send_value(output, target_target, "rate", 0.0)
                probe.send_value(output, source_target, "bass", 0.5)
                probe.send_value(output, target_target, "bass", 0.5)
                probe.send_value(
                    output,
                    "master",
                    "crossfader",
                    crossfader_value(source_deck, 1.0),
                )
            log.write(
                "continuous_target_cleanup",
                source_deck=source_deck,
                target_deck=target_deck,
                target_playback_preserved=True,
            )
        elif not transition_completed and continue_source:
            with probe.mido.open_output(probe.port_name()) as output:
                preserved_deck = preserve_live_playback_after_failure(
                    probe,
                    output,
                    source_deck,
                    target_deck,
                    source_stopped=source_stopped,
                )
            log.write(
                "failed_live_transition_cleanup",
                preserved_deck=preserved_deck,
                source_stopped=source_stopped,
            )
        else:
            with probe.mido.open_output(probe.port_name()) as output:
                probe.send_value(output, "1", "play", 0.0)
                probe.send_value(output, "2", "play", 0.0)
                probe.send_sync_value(output, 2, "enabled", False)
                probe.send_sync_value(output, 1, "enabled", False)
                probe.send_value(output, "1", "rate", 0.0)
                probe.send_value(output, "2", "rate", 0.0)
                probe.send_value(output, "1", "bass", 0.5)
                probe.send_value(output, "2", "bass", 0.5)
                probe.send_value(output, "master", "crossfader", 0.5)
        if record:
            probe.recording("stop")
            log.write("recording_stopped")
        CANCEL_FILE.unlink(missing_ok=True)
        TAKEOVER_FILE.unlink(missing_ok=True)
        log.write("run_finished")
    print(f"run output={run_directory}")
    return run_directory


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--source-position-seconds", type=float)
    parser.add_argument("--source-observed-monotonic", type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    resolved = resolve_plan(args.plan, args.artifacts)
    print(json.dumps(resolved, indent=2, sort_keys=True))
    if args.execute:
        execute(
            resolved,
            args.artifacts,
            args.record,
            source_position_seconds=args.source_position_seconds,
            source_observed_monotonic=args.source_observed_monotonic,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

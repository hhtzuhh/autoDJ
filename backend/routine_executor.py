"""Run parameterized DJ actions against observed file position, never a BPM clock.

Uses the product action contract. No model runs inside this scheduler.
"""
from bisect import bisect_right
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time


def validate_routine_plan(plan: dict, source: dict, target: dict) -> None:
    from performance_actions import compile_routine, tempo_bounds, validate_edge_actions
    from in_track_actions import compile_in_track, validate_in_track_actions
    edge = {
        "mode": plan.get("transition_mode", "beat_sync_crossfade"),
        "source_exit_beat_index": plan["deck_a"]["transition_start_beat_index"],
        "target_entry_beat_index": plan["deck_b"]["cue_beat_index"],
        "transition_bars": plan["mix"].get("bars", 0),
        "target_beat_count": len(target["rhythm"]["beats_seconds"]),
        "tempo_bounds": tempo_bounds(float(source["rhythm"]["bpm"]), float(target["rhythm"]["bpm"])),
    }
    if edge["mode"] == "clean_break" and ("tempo_routine" in plan or
            ("automation_start_beat_index" in plan and not plan.get("in_track_actions"))):
        raise ValueError("native-tempo cut cannot carry a tempo routine")
    routine = validate_edge_actions(edge, plan["performance_actions"])
    expected = compile_routine(deepcopy(plan), edge, routine)
    if "in_track_actions" in plan:
        validate_in_track_actions([edge], [routine], plan["in_track_actions"], [plan["deck_a"]["audio_sha256"]])
        compile_in_track(expected, routine["in_track_actions"])
    for key in ("mix", "tempo_routine", "automation_start_beat_index", "deck_a", "in_track_actions"):
        if expected.get(key) != plan.get(key):
            raise ValueError(f"compiled {key} does not match performance actions")


def beat_at(seconds: float, grid: list[float]) -> float:
    index = max(0, min(len(grid) - 2, bisect_right(grid, seconds) - 1))
    duration = grid[index + 1] - grid[index]
    if duration <= 0:
        raise ValueError("beat grid must increase")
    return index + (seconds - grid[index]) / duration


def execute(resolved: dict, artifacts: Path, record: bool) -> Path:
    import transition as engine
    import probe
    plan, mix = resolved["plan"], resolved["plan"]["mix"]
    clean = plan.get("transition_mode") == "clean_break"
    tempo = plan.get("tempo_routine", {})
    source, target = resolved["source_deck"], resolved["target_deck"]
    source_grid, target_grid = resolved["source_beats_seconds"], resolved["target_beats_seconds"]
    source_exit, target_entry = plan["deck_a"]["transition_start_beat_index"], plan["deck_b"]["cue_beat_index"]
    start_beat = plan["automation_start_beat_index"]
    common = tempo.get("common_bpm")
    source_ratio = common / tempo["source_native_bpm"] if not clean else 1.
    target_ratio = common / tempo["target_native_bpm"] if not clean else 1.
    run = artifacts / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    run.mkdir(parents=True)
    log = engine.RunLog(run / "transition-events.jsonl", set_run_id=plan.get("set_run_id"), plan_id=plan["plan_id"])
    (run / "resolved-plan.json").write_text(json.dumps(resolved, indent=2))
    stopped, completed, takeover, touched, recording = False, False, False, False, False
    shaping_control = None
    engine.CANCEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    # The supervisor clears obsolete signals before launch; do not erase a new
    # cancellation/takeover that races with startup.
    def check():
        engine.wait_until(time.monotonic(), plan_id=plan["plan_id"])

    def position(deck, grid, output):
        check()
        seconds = probe.read_position(deck, resolved["sample_rate_a"] if deck == source else resolved["sample_rate_b"], output)
        return beat_at(seconds, grid)

    def follow(deck, grid, begin, end, callback, output, *, allow_safe_seek=False, previous_beat=None):
        previous, last_movement = previous_beat, time.monotonic()
        while True:
            current = position(deck, grid, output)
            if previous is not None and (current < previous - .5 or current > previous + 2):
                # The source may be repositioned while waiting for the first
                # action. Follow its new file position, not the old deadline.
                # Both ends must precede automation_start (which may be an
                # in-track effect or tempo ramp well before the crossfade).
                # This permission is never enabled again once that wait ends.
                if allow_safe_seek and deck == source and previous < start_beat and current < start_beat:
                    last_movement = time.monotonic()
                    log.write("routine_safe_seek", deck=deck, previous_beat=previous,
                              beat=current, automation_start_beat=start_beat)
                else:
                    raise engine.TransitionTakenOver("unexpected playback jump during routine")
            if previous is None or current > previous + .001:
                last_movement = time.monotonic()
            if time.monotonic() - last_movement > 2:
                raise RuntimeError("routine position stopped advancing")
            previous = current
            if current >= begin:
                callback(min(1.0, max(0.0, (current - begin) / max(.001, end - begin))))
            if current >= end:
                return current
            engine.wait_until(time.monotonic() + .02, plan_id=plan["plan_id"])

    def verify_ratio(deck, expected, output):
        status = probe.deck_status(deck, output=output)
        if float(status["file_bpm"]) <= 0 or abs(float(status["bpm"]) / float(status["file_bpm"]) - expected) > .003:
            raise RuntimeError("Mixxx did not apply requested playback tempo")

    log.write("run_started", plan_id=plan["plan_id"], performance_actions=plan["performance_actions"])
    try:
        check()
        with probe.feedback_session(probe.port_name()), probe.mido.open_output(probe.port_name()) as output:
            probe.require_routine_protocol(output)
            check()
            for deck in (source, target):
                status = probe.deck_status(deck, output=output)
                if float(status["file_bpm"]) <= 0 or int(status["sync_mode"]) != 0 or abs(float(status["bpm"]) / float(status["file_bpm"]) - 1) > .003:
                    raise RuntimeError("routine requires native speed and sync off at start; preserve current playback")
            current = position(source, source_grid, output)
            if current >= start_beat:
                raise RuntimeError("routine start has already passed")
            probe.precise_seek(target, resolved["deck_b_cue_seconds"], resolved["sample_rate_b"], False)
            check()
            if record:
                probe.recording("start")
                recording = True
            # Wait untouched until the first musical action, following actual position.
            follow(source, source_grid, start_beat, start_beat, lambda _: None, output,
                   allow_safe_seek=True, previous_beat=current)
            # In-track actions run on the audible source alone, well before the
            # handoff. The same cancellation checks and authoritative beat clock
            # own both stages; there is no competing background effect worker.
            from in_track_actions import control_value
            for action in plan.get("in_track_actions", []):
                begin, end = action["start_beat"], action["start_beat"] + action["duration_beats"]
                follow(source, source_grid, begin, begin, lambda _: None, output)
                shaping_control = "volume" if action["function"] == "volume_duck" else "bass"
                log.write("in_track_action_started", action=action, deck=source)
                def shape(progress):
                    control, value = control_value(action, progress)
                    probe.send_value(output, str(source), control, value)
                follow(source, source_grid, begin, end, shape, output)
                shaping_control = None
                log.write("in_track_action_completed", action=action, deck=source)
            transition_begin = source_exit - tempo.get("pre_beats", 0)
            follow(source, source_grid, transition_begin, transition_begin, lambda _: None, output)
            touched = True
            for deck in (source, target):
                probe.send_value(output, str(deck), "volume", .75)
                probe.send_value(output, str(deck), "bass", .5)
            probe.send_value(output, "master", "crossfader", engine.crossfader_value(source, 0))
            probe.send_tempo_ratio(output, target, target_ratio)
            verify_ratio(target, target_ratio, output)
            if mix.get("bass_swap") or mix.get("bass_dip"):
                probe.send_value(output, str(target), "bass", 0)
            if tempo.get("pre_beats"):
                def ramp_source(progress):
                    probe.send_tempo_ratio(output, source, 1 + (source_ratio - 1) * progress)
                follow(source, source_grid, transition_begin, source_exit, ramp_source, output)
            else:
                follow(source, source_grid, source_exit, source_exit, lambda _: None, output)
            verify_ratio(source, source_ratio, output)
            check()
            # Explicit playback ratios align analyzed grids; do not invoke Mixxx
            # sync's half/double-tempo interpretation or alter file BPM metadata.
            if clean and mix.get("fade_out_seconds"):
                started = time.monotonic()
                while time.monotonic() - started < mix["fade_out_seconds"]:
                    check()
                    progress = min(1., (time.monotonic() - started) / mix["fade_out_seconds"])
                    probe.send_value(output, str(source), "volume", .75 * (1 - progress))
                    engine.wait_until(time.monotonic() + .02, plan_id=plan["plan_id"])
                check()
                probe.send_value(output, str(source), "volume", 0)
            probe.send_value(output, str(target), "play", 1)
            log.write("routine_handoff_started", common_bpm=common)
            def blend(progress):
                controls = engine.mix_controls(mix, progress)
                if "source_bass" in controls:
                    probe.send_value(output, str(source), "bass", controls["source_bass"])
                    probe.send_value(output, str(target), "bass", controls["target_bass"])
                probe.send_value(output, "master", "crossfader", engine.crossfader_value(source, controls["crossfader"]))
            if clean:
                probe.send_value(output, "master", "crossfader", engine.crossfader_value(source, 1))
            else:
                follow(source, source_grid, source_exit, source_exit + mix["beats"], blend, output)
            probe.send_value(output, str(source), "play", 0)
            stopped = True
            probe.send_value(output, str(target), "bass", .5)
            if tempo.get("post_beats"):
                begin = target_entry + mix["beats"]
                def ramp_target(progress):
                    probe.send_tempo_ratio(output, target, target_ratio + (1 - target_ratio) * progress)
                follow(target, target_grid, begin, begin + tempo["post_beats"], ramp_target, output)
            verify_ratio(target, 1.0, output)
            probe.send_tempo_ratio(output, source, 1.0)
            completed = True
            log.write("routine_completed", target_native_bpm=tempo.get("target_native_bpm", resolved["target_bpm"]))
    except engine.TransitionTakenOver as error:
        takeover = True
        log.write("run_taken_over", preserve_playback=True, reason=str(error))
    except (engine.TransitionCancelled, probe.TransitionCancelled, KeyboardInterrupt):
        log.write("run_cancelled")
    finally:
        if shaping_control and not takeover:
            with probe.mido.open_output(probe.port_name()) as output:
                probe.send_value(output, str(source), shaping_control, .75 if shaping_control == "volume" else .5)
            log.write("in_track_cleanup", control=shaping_control)
        # On human takeover, write nothing. On cancellation/error, preserve one
        # audible deck at its current speed instead of snapping tempo to native.
        if touched and not takeover:
            with probe.mido.open_output(probe.port_name()) as output:
                live = target if stopped else source
                idle = source if stopped else target
                probe.send_value(output, str(idle), "play", 0)
                for deck in (source, target): probe.send_value(output, str(deck), "bass", .5)
                probe.send_value(output, "master", "crossfader", engine.crossfader_value(source, 1 if stopped else 0))
            log.write("routine_cleanup", preserved_deck=live, completed=completed)
        if recording: probe.recording("stop")
        log.write("run_finished", completed=completed)
    if not completed:
        raise RuntimeError("routine interrupted; playback preserved, plan not completed")
    return run

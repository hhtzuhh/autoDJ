"""Bounded musical actions: Meta supplies parameters; local code validates them.

All beat offsets are relative to source exit / target entry. Playing-side
embellishments are optional. Each edge must contain one audible handoff.
"""
from copy import deepcopy
import math

MAX_TEMPO_FRACTION = 0.20
MAX_RAMP_BEATS = 128
MIN_DWELL_BEATS = 32
MAX_RAMP_FRACTION_PER_BAR = .025

ACTION_CATALOG = {
    "ramp_tempo": {
        "purpose": "Gradually change playback speed before or after a beat-synced handoff.",
        "parameters": {"deck": "source or target", "start_beat": "integer cue-relative beat offset",
                       "duration_beats": "8..128, multiple of 4", "target_bpm": "within supplied common BPM bounds (source), or target native BPM (target)"},
        "timing": "Source ramp must end at beat 0. Target ramp starts at mix end and restores native BPM. Minimum duration = max(8, 4 * ceil(abs(end_bpm / start_bpm - 1) / 0.025)) beats. Target ramp starts at common BPM, not target native BPM.",
        "tradeoff": "Changes groove and possibly pitch; use longer ramps for larger changes. Not an effect to add automatically.",
        "optional": True,
    },
    "crossfade": {
        "purpose": "Bring the next song in while removing the current song at a common tempo.",
        "parameters": {"start_beat": 0, "duration_beats": "exact candidate transition_bars * 4", "curve": "linear or late_cut",
                       "target_bpm": "numeric BPM within candidate tempo_bounds"},
        "timing": "Target starts at its analyzed entry cue. late_cut keeps it inaudible until the final beat.",
        "tradeoff": "Linear allows longer musical overlap; late_cut makes a short handoff after a silent target lead-in. Avoid conflicting vocals or bass.",
        "optional": False,
    },
    "bass_swap": {
        "purpose": "Exchange low EQ so two bass lines do not compete throughout the blend.",
        "parameters": {"start_beat": "0 or later within crossfade", "duration_beats": "positive integer, ends within crossfade"},
        "timing": "Incoming bass starts cut; outgoing bass decreases and incoming bass increases during this interval.",
        "tradeoff": "Only low EQ, not source separation. It may remove wanted kick/bass. Omit when no supported need exists.",
        "optional": True,
    },
    "bass_dip": {
        "purpose": "Create a deliberate bass-light moment then restore the incoming low end.",
        "parameters": {"start_beat": "within crossfade", "duration_beats": "positive even integer, ends within crossfade"},
        "timing": "First half removes source bass, second half restores target bass; cannot combine with bass_swap.",
        "tradeoff": "Creates contrast but can reduce momentum. Not automatically better than leaving EQ neutral.",
        "optional": True,
    },
    "cut_to_next": {
        "purpose": "Switch between native tempos without overlapping mismatched rhythms.",
        "parameters": {"start_beat": 0, "fade_out_seconds": "0 for direct cut or 0.5 for fade then cut"},
        "timing": "Start target at supplied entry after the optional source fade; stop source.",
        "tradeoff": "Can suit an intentional contrast or sparse boundary. Explain the musical reason; do not choose solely because a local heuristic score is highest.",
        "optional": False,
    },
}

# Exact wire shapes, separate from the human-readable parameter descriptions.
# These are examples, not a complete routine or a substitute for cue validation.
ACTION_SHAPE_EXAMPLES = [
    {"function": "ramp_tempo", "deck": "source", "start_beat": -32, "duration_beats": 32, "target_bpm": 130},
    {"function": "ramp_tempo", "deck": "target", "start_beat": 16, "duration_beats": 32, "target_bpm": 140},
    {"function": "crossfade", "start_beat": 0, "duration_beats": 16, "curve": "linear", "target_bpm": 130},
    {"function": "bass_swap", "start_beat": 0, "duration_beats": 8},
    {"function": "bass_dip", "start_beat": 0, "duration_beats": 8},
    {"function": "cut_to_next", "start_beat": 0, "fade_out_seconds": 0},
]


def tempo_bounds(a: float, b: float) -> dict | None:
    if not all(math.isfinite(x) and x > 0 for x in (a, b)):
        raise ValueError("native BPM must be positive and finite")
    lower, upper = max(a, b) * (1 - MAX_TEMPO_FRACTION), min(a, b) * (1 + MAX_TEMPO_FRACTION)
    if lower > upper:
        return None
    lower, upper = math.ceil(lower * 10000) / 10000, math.floor(upper * 10000) / 10000
    return {"minimum_bpm": lower, "maximum_bpm": upper,
            "source_native_bpm": a, "target_native_bpm": b,
            "suggested_common_bpm": round(min(upper, max(lower, (a + b) / 2)), 4),
            "max_change_fraction_per_deck": MAX_TEMPO_FRACTION}


def minimum_ramp_beats(start_bpm: float, end_bpm: float) -> int:
    """Whole bars at the same linear fractional-change limit as the executor.

    Tolerance only removes floating point noise at exact bar boundaries.
    """
    bars = math.ceil(abs(end_bpm / start_bpm - 1) / MAX_RAMP_FRACTION_PER_BAR - 1e-9)
    return max(8, bars * 4)


def tempo_ramp_options(edge: dict) -> list[dict]:
    """Arithmetic aids, not validated whole-window plans or required choices."""
    bounds = edge.get("tempo_bounds")
    if edge.get("mode") == "clean_break" or not bounds:
        return []
    common_bpms = sorted({bounds["minimum_bpm"], bounds["maximum_bpm"], bounds["suggested_common_bpm"]})
    return [{"common_bpm": bpm,
             "minimum_source_ramp_beats": minimum_ramp_beats(bounds["source_native_bpm"], bpm) if abs(bpm-bounds["source_native_bpm"]) > .001 else 0,
             "minimum_target_return_beats": minimum_ramp_beats(bpm, bounds["target_native_bpm"]) if abs(bpm-bounds["target_native_bpm"]) > .001 else 0}
            for bpm in common_bpms]


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def integer(value, label):
    number(value, label)
    if not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def validate_edge_actions(edge: dict, actions: list) -> dict:
    if not isinstance(actions, list) or not 1 <= len(actions) <= 4:
        raise ValueError("each transition requires 1..4 actions, including one handoff")
    grouped = {}
    for action in actions:
        if not isinstance(action, dict) or action.get("function") not in ACTION_CATALOG:
            raise ValueError("unknown performance action")
        fn = action["function"]
        fields = set(ACTION_CATALOG[fn]["parameters"]) | {"function"}
        if set(action) != fields:
            raise ValueError(f"{fn} parameters do not match its contract: "
                             f"missing={sorted(fields-set(action))}, unexpected={sorted(set(action)-fields)}; "
                             "use a flat action object with exactly the documented keys")
        key = (fn, action.get("deck")) if fn == "ramp_tempo" else (fn,)
        if key in grouped:
            raise ValueError("duplicate action controls")
        grouped[key] = action
        integer(action["start_beat"], "start_beat")
    handoffs = [a for a in actions if a["function"] in {"crossfade", "cut_to_next"}]
    if len(handoffs) != 1 or handoffs[0]["start_beat"] != 0:
        raise ValueError("exactly one handoff must start at cue beat 0")
    handoff = handoffs[0]
    if edge["mode"] == "clean_break":
        if len(actions) != 1 or handoff["function"] != "cut_to_next" or number(handoff["fade_out_seconds"], "fade") not in (0, .5):
            raise ValueError("native-tempo cut only supports its optional bounded fade")
        return {"actions": deepcopy(actions), "technique": "fade_out_cut" if handoff["fade_out_seconds"] else "clean_cut",
                "pre_beats": 0, "post_beats": 0}
    beats = edge["transition_bars"] * 4
    if handoff["function"] != "crossfade" or handoff["curve"] not in {"linear", "late_cut"} or integer(handoff["duration_beats"], "duration") != beats:
        raise ValueError("crossfade must match the supplied cue window")
    bounds = edge.get("tempo_bounds")
    if not bounds:
        raise ValueError("action candidate lacks measured tempo bounds")
    bpm = number(handoff["target_bpm"], "target_bpm")
    if not bounds["minimum_bpm"] <= bpm <= bounds["maximum_bpm"]:
        raise ValueError("common BPM outside per-deck playback bounds")
    pre, post = 0, 0
    for a in actions:
        if a["function"] == "ramp_tempo":
            duration = integer(a["duration_beats"], "ramp duration")
            if not 8 <= duration <= MAX_RAMP_BEATS or duration % 4:
                raise ValueError("ramp duration must be 8..128 beats in whole bars")
            end_bpm = number(a["target_bpm"], "ramp target BPM")
            if a["deck"] == "source" and a["start_beat"] == -duration and abs(end_bpm - bpm) < .001:
                pre = duration
                start_bpm = bounds["source_native_bpm"]
            elif a["deck"] == "target" and a["start_beat"] == beats and abs(end_bpm - bounds["target_native_bpm"]) < .001:
                post = duration
                start_bpm = bpm
            else:
                raise ValueError("tempo ramps must meet the common BPM then restore target native BPM")
            minimum = minimum_ramp_beats(start_bpm, end_bpm)
            if duration < minimum:
                raise ValueError(f"{a['deck']} tempo ramp {start_bpm:g} -> {end_bpm:g} BPM over {duration} beats exceeds 2.5 percent per bar; requires at least {minimum} beats")
    if abs(bpm - bounds["source_native_bpm"]) > .001 and not pre:
        raise ValueError("changing source tempo requires a pre-handoff ramp")
    if abs(bpm - bounds["target_native_bpm"]) > .001 and not post:
        raise ValueError("changed target tempo requires an explicit return-to-native ramp")
    if edge["source_exit_beat_index"] - pre < edge.get("earliest_source_action_beat", 8):
        raise ValueError("source ramp has insufficient live runway")
    if edge["target_entry_beat_index"] + beats + post >= edge.get("target_beat_count", float("inf")):
        raise ValueError("target ramp exceeds analyzed audio")
    eq = [a for a in actions if a["function"] in {"bass_swap", "bass_dip"}]
    if len(eq) > 1:
        raise ValueError("choose at most one low-EQ action")
    for a in eq:
        duration = integer(a["duration_beats"], "EQ duration")
        if duration <= 0 or a["start_beat"] < 0 or a["start_beat"] + duration > beats:
            raise ValueError("low-EQ action must fit within crossfade")
        if a["function"] == "bass_dip" and duration % 2:
            raise ValueError("bass dip requires an even duration")
        if handoff["curve"] == "late_cut":
            raise ValueError("late cut keeps target hidden; use neutral EQ")
    technique = eq[0]["function"] if eq else "late_cut" if handoff["curve"] == "late_cut" else "crossfade"
    return {"actions": deepcopy(actions), "technique": technique, "pre_beats": pre, "post_beats": post,
            "common_bpm": bpm, "source_native_bpm": bounds["source_native_bpm"], "target_native_bpm": bounds["target_native_bpm"]}


def validate_window_actions(edges: list[dict], action_sets: list) -> list[dict]:
    if not isinstance(action_sets, list) or len(action_sets) != len(edges):
        raise ValueError("one action list is required per transition")
    routines = []
    for index, (edge, actions) in enumerate(zip(edges, action_sets), start=1):
        try:
            routines.append(validate_edge_actions(edge, actions))
        except ValueError as error:
            raise ValueError(f"transition {index}: {error}") from error
    for left, right, before, after in zip(edges, edges[1:], routines, routines[1:]):
        ready = left["target_entry_beat_index"] + left["transition_bars"] * 4 + before["post_beats"] + MIN_DWELL_BEATS
        if right["source_exit_beat_index"] - after["pre_beats"] < ready:
            raise ValueError("tempo actions overlap the next transition or consume minimum dwell")
    return routines


def compile_routine(plan: dict, edge: dict, routine: dict) -> dict:
    from transition_techniques import apply_technique
    apply_technique(plan, routine["technique"])
    plan["performance_actions"] = deepcopy(routine["actions"])
    if edge["mode"] == "clean_break":
        return plan
    mix = plan["mix"]
    mix["tempo_source"] = "explicit_playback_ratio"
    for action in routine["actions"]:
        if action["function"] == "bass_swap":
            mix["bass_swap"] = {"start_beat": action["start_beat"], "end_beat": action["start_beat"] + action["duration_beats"]}
        elif action["function"] == "bass_dip":
            mix["bass_dip_range"] = {"start_beat": action["start_beat"], "end_beat": action["start_beat"] + action["duration_beats"]}
    plan["tempo_routine"] = {k: v for k, v in routine.items() if k not in {"actions", "technique"}}
    start = edge["source_exit_beat_index"] - routine["pre_beats"]
    plan["automation_start_beat_index"] = start
    plan["deck_a"]["play_start_beat_index"] = max(0, start - 8)
    return plan

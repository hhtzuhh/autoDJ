"""Optional source-song shaping, scheduled in absolute analyzed song beats."""
from copy import deepcopy
from performance_actions import integer, number, MIN_DWELL_BEATS

IN_TRACK_CATALOG = {
    "bass_pulse": {
        "purpose": "Gradually reduce the playing song's low EQ, then restore it for contrast.",
        "tradeoff": "Also reduces kick energy; use sparingly and never assume a measured drop.",
    },
    "bass_reveal": {
        "purpose": "Start a bass-light passage, then gradually restore the playing song's low end.",
        "tradeoff": "The initial low-EQ reduction is intentional; it may weaken an already sparse passage.",
    },
    "volume_duck": {
        "purpose": "Gently lower the playing song's volume, then restore it without stopping playback.",
        "tradeoff": "Reduces the entire mix, including vocals; avoid unnecessary energy dips.",
    },
}
for name, spec in IN_TRACK_CATALOG.items():
    spec.update(parameters={"track_id": "source track ID", "start_beat": "absolute song beat, multiple of 4",
                            "duration_beats": "8..32, multiple of 4", "depth": f"fractional reduction: 0.1..{0.3 if name == 'volume_duck' else 0.6}"},
                timing="Within supplied source-song bounds; finish 8 beats before the transition routine. Max 2 per song; 8-beat gap. Return to the executor baseline (low EQ 0.5, channel volume 0.75).",
                optional=True)


def validate_in_track_actions(edges, routines, actions, track_ids):
    if not isinstance(routines, list) or len(routines) != len(edges) or len(track_ids) < len(edges):
        raise ValueError("in-track actions require validated transition routines and source identities")
    if not isinstance(actions, list) or len(actions) > 2 * len(edges):
        raise ValueError("in_track_actions must be an array; [] means leave songs unchanged")
    by_source = {track_ids[i]: [] for i in range(len(edges))}
    for action in actions:
        if not isinstance(action, dict) or action.get("function") not in IN_TRACK_CATALOG:
            raise ValueError("unknown in-track action")
        if set(action) != {"function", "track_id", "start_beat", "duration_beats", "depth"}:
            raise ValueError("in-track action parameters do not match its contract")
        track = action["track_id"]
        if not isinstance(track, str) or track not in by_source:
            raise ValueError("in-track action must target a source song in this window")
        start = integer(action["start_beat"], "in-track start_beat")
        duration = integer(action["duration_beats"], "in-track duration_beats")
        depth = number(action["depth"], "in-track depth")
        if start < 8 or start % 4 or not 8 <= duration <= 32 or duration % 4:
            raise ValueError("in-track timing must use whole bars, duration 8..32 beats")
        if not .1 <= depth <= (.3 if action["function"] == "volume_duck" else .6):
            raise ValueError("in-track depth out of bounds")
        by_source[track].append(deepcopy(action))
    for i, (edge, routine) in enumerate(zip(edges, routines)):
        selected = sorted(by_source[track_ids[i]], key=lambda a: a["start_beat"])
        earliest = max(8, edge.get("earliest_source_action_beat", 8))
        if i:
            previous = edges[i - 1]
            earliest = max(earliest, previous["target_entry_beat_index"] + previous["transition_bars"] * 4
                           + routines[i - 1]["post_beats"] + MIN_DWELL_BEATS)
        latest = edge["source_exit_beat_index"] - routine["pre_beats"] - 8
        if len(selected) > 2:
            raise ValueError("at most two in-track actions per song")
        for action in selected:
            if action["start_beat"] < earliest or action["start_beat"] + action["duration_beats"] > latest:
                raise ValueError("in-track action overlaps entry, transition, tempo ramp or another action")
            earliest = action["start_beat"] + action["duration_beats"] + 8
        routine["in_track_actions"] = selected
    return routines


def compile_in_track(plan, actions):
    plan["in_track_actions"] = deepcopy(actions)
    if actions:
        first = min(a["start_beat"] for a in actions)
        plan["automation_start_beat_index"] = first
        plan["deck_a"]["play_start_beat_index"] = first - 8
    return plan


def control_value(action, progress):
    """Bounded reduction only; all shapes end at the engine's neutral baseline."""
    progress = min(1., max(0., progress))
    shape = 1 - progress if action["function"] == "bass_reveal" else 1 - abs(2 * progress - 1)
    control, baseline = ("volume", .75) if action["function"] == "volume_duck" else ("bass", .5)
    return control, baseline * (1 - action["depth"] * shape)

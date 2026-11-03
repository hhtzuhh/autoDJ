"""Executable technique vocabulary shared by the planner and plan compiler.

Names describe control operations, not a guarantee of musical suitability.
Timing and tempo feasibility remain properties of each supplied cue candidate.
"""
from typing import Any


TECHNIQUES = {
    "crossfade": {
        "mode": "beat_sync_crossfade",
        "description": "Linear beat-synced blend across the selected bars; both low EQs stay neutral.",
        "tradeoff": "Smooth overlap; simultaneous bass or vocals can clash.",
    },
    "bass_swap": {
        "mode": "beat_sync_crossfade",
        "description": "Linear blend; exchange low EQ gradually between 25% and 75% of the mix.",
        "tradeoff": "Useful when two bass parts compete; does not separate vocals or stems.",
    },
    "bass_dip": {
        "mode": "beat_sync_crossfade",
        "description": "Blend while removing outgoing bass from 25%-50%, then restoring incoming bass from 50%-75%.",
        "tradeoff": "Creates a deliberate bass-light midpoint then recovery; can reduce momentum.",
    },
}


def technique_options(mode: str) -> list[str]:
    return [name for name, spec in TECHNIQUES.items() if spec["mode"] == mode]


def validate_selection_policy(selection: dict) -> None:
    """Reject saved direct cuts as well as new ones; never rewrite a reviewed plan."""
    rows = [selection.get("next_executable_plan", {}), *selection.get("transition_buffer", [])]
    for row in rows:
        mix, routine = row.get("mix", {}), row.get("routine", {})
        technique = mix.get("technique", row.get("technique", routine.get("technique")))
        mode = row.get("transition_mode", row.get("mode"))
        actions = [*row.get("performance_actions", []), *routine.get("actions", [])]
        if (technique in {"clean_cut", "fade_out_cut", "late_cut"}
                or mode == "clean_break" or mix.get("crossfader_curve") == "late_cut"
                or any(a.get("function") == "cut_to_next" or a.get("curve") == "late_cut" for a in actions)):
            raise ValueError("Cut transitions are disabled for this demo. Build a new performance plan using crossfade, bass swap or bass dip.")


def apply_technique(plan: dict[str, Any], technique: str) -> dict[str, Any]:
    mode = plan.get("transition_mode", "beat_sync_crossfade")
    if technique not in technique_options(mode):
        raise ValueError("technique is not executable for this transition mode")
    mix = plan["mix"]
    mix.pop("bass_swap", None)
    mix.pop("bass_dip", None)
    mix.pop("fade_out_seconds", None)
    mix["technique"] = technique
    mix["crossfader_curve"] = "linear"
    beats = mix["beats"]
    if technique == "bass_swap":
        mix["bass_swap"] = {"start_beat": beats // 4, "end_beat": beats * 3 // 4}
    elif technique == "bass_dip":
        mix["bass_dip"] = True
    return plan


def selected_techniques(decision: Any, edges: list[dict[str, Any]]) -> list[str]:
    """Keep old saved decisions readable; new API window decisions are strict."""
    selected = getattr(decision, "transition_techniques", None)
    if selected is None:
        selected = [
            "clean_cut" if edge["mode"] == "clean_break" else
            "bass_swap" if getattr(decision, "use_bass_swap", False) else "crossfade"
            for edge in edges
        ]
    if len(selected) != len(edges):
        raise ValueError("one technique is required for every transition")
    for name, edge in zip(selected, edges):
        if name not in technique_options(edge["mode"]) or name not in edge.get("technique_options", technique_options(edge["mode"])):
            raise ValueError("Meta selected an unavailable transition technique")
    return list(selected)

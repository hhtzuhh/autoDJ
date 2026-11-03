"""Compile clean-break pair metadata into the existing lifecycle plan envelope."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from transition_pipeline import AnalysisCatalog


CLEAN_BREAK_SYSTEM = """
Choose exactly one supplied clean-break DJ candidate. Local code has already
bounded track identity, analyzed exit/entry beats, and queue revision. Never
invent IDs or timing. Return exactly one JSON object containing only
selected_candidate_id, confidence, and reason.
The handoff uses fade_out_cut: fade the source to silence over 0.5 seconds,
then start the target at native tempo. Direct clean cuts are disabled.
""".strip()


def select_clean_break_with_meta(
    candidates: list[dict[str, Any]],
    *,
    queue_revision: int,
    source_id: str,
    target_id: str,
    transport: Callable[[str, str], str],
) -> tuple[dict[str, Any], float, str]:
    bounded = candidates[:10]
    if not bounded:
        raise ValueError("no clean-break candidates")
    supplied = [
        {
            "candidate_id": f"clean-{index}",
            "exit_beat": int(candidate["a_exit"]["beat_index"]),
            "entry_beat": int(candidate["b_entry"]["beat_index"]),
            "heuristic_score": float(candidate["heuristic_score"]),
        }
        for index, candidate in enumerate(bounded, start=1)
    ]
    response = transport(
        CLEAN_BREAK_SYSTEM,
        json.dumps(
            {
                "task": "select_clean_break",
                "queue_revision": queue_revision,
                "source_track_id": source_id,
                "target_track_id": target_id,
                "candidates": supplied,
            },
            separators=(",", ":"),
        ),
    ).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)
    payload = json.loads(response)
    if set(payload) != {"selected_candidate_id", "confidence", "reason"}:
        raise ValueError("clean-break Meta response does not match strict schema")
    allowed = {row["candidate_id"]: index for index, row in enumerate(supplied)}
    selected_id = str(payload["selected_candidate_id"])
    if selected_id not in allowed:
        raise ValueError("Meta selected an unknown clean-break candidate")
    confidence = float(payload["confidence"])
    reason = payload["reason"]
    if not 0 <= confidence <= 1 or not isinstance(reason, str) or not reason.strip():
        raise ValueError("invalid clean-break confidence or reason")
    return bounded[allowed[selected_id]], confidence, reason.strip()


def compile_clean_break_plan(
    *,
    source_id: str,
    target_id: str,
    pair: dict[str, Any],
    catalog: AnalysisCatalog,
    queue_revision: int,
    source_deck: int,
    target_deck: int,
    model: str,
) -> dict[str, Any]:
    if pair.get("transition_mode") != "clean_break":
        raise ValueError("candidate is not a clean break")
    if {source_deck, target_deck} != {1, 2}:
        raise ValueError("clean break requires distinct Mixxx decks")
    source = catalog.tracks[source_id]
    target = catalog.tracks[target_id]
    exit_beat = int(pair["a_exit"]["beat_index"])
    entry_beat = int(pair["b_entry"]["beat_index"])
    return {
        "schema_version": 1,
        "schema_uri": "schemas/dj-transition-plan-v1.schema.json",
        "plan_id": f"clean-q{queue_revision}-{source_id[:8]}-{target_id[:8]}-{exit_beat}-{entry_beat}",
        "source": f"meta_model_api_{model.replace('-', '_').replace('.', '_')}",
        "transition_mode": "clean_break",
        "source_start_mode": "continue_playing",
        "completion_mode": "continue_target",
        "allow_clean_cut": False,
        "deck_a": {
            "audio_sha256": source_id,
            "analysis_cache_key": source.analysis_cache_key,
            "analysis_filename": "track-analysis-v1.json",
            "deck": source_deck,
            "play_start_beat_index": max(0, exit_beat - 8),
            "transition_start_beat_index": exit_beat,
        },
        "deck_b": {
            "audio_sha256": target_id,
            "analysis_cache_key": target.analysis_cache_key,
            "analysis_filename": "track-analysis-v1.json",
            "deck": target_deck,
            "cue_beat_index": entry_beat,
        },
        "mix": {
            "technique": "fade_out_cut",
            "fade_out_seconds": 0.5,
            "handoff": "target_start_then_crossfader_cut_then_source_stop",
            "tempo_source": "native_file_bpm_no_sync",
            "tail_beats": 0,
        },
    }

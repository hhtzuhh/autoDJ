#!/usr/bin/env python3
"""Build one DJ-track analysis from All-In-One rhythm/structure and local DSP."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
import shutil
import json
from pathlib import Path
import subprocess
import sys

from analysis_features import (
    aggregate_bars,
    build_candidates,
    decode_mono,
    frame_features,
    sha256_file,
)


from runtime_paths import DATA_ROOT
from allin1_normalize import normalize_beats
DEFAULT_ARTIFACTS = DATA_ROOT / "artifacts/time-map"
ANALYSIS_VERSION = "dj-track-analysis-v1"
ENERGY_DIRECTION_THRESHOLD = 0.12
ENERGY_LOW_THRESHOLD = 0.33
ENERGY_HIGH_THRESHOLD = 0.67
REVIEWED_CUE_TOLERANCE_SECONDS = 0.75


def audio_metadata(source: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels,codec_name:format=duration",
        "-of",
        "json",
        str(source),
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(completed.stdout)
    stream = payload["streams"][0]
    return {
        "duration_seconds": float(payload["format"]["duration"]),
        "sample_rate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
        "codec": stream["codec_name"],
    }


def run_allin1(source: Path, work_directory: Path, device: str) -> Path:
    executable = os.environ.get("DJ_ALLIN1_CLI") or shutil.which("all-in-one-infer")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("Optional All-In-One requires DJ_ALLIN1_CLI pointing to all-in-one-infer")
    input_directory = work_directory / "input"
    struct_directory = work_directory / "allin1-raw"
    input_directory.mkdir(parents=True, exist_ok=True)
    struct_directory.mkdir(parents=True, exist_ok=True)
    wav = input_directory / "source.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-ar",
            "44100",
            "-ac",
            "2",
            str(wav),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(executable),
            str(wav),
            "--out-dir",
            str(struct_directory),
            "--device",
            device,
            "--demix-dir",
            str(work_directory / "demix"),
            "--spec-dir",
            str(work_directory / "spec"),
            "--no-multiprocess",
        ],
        check=True,
    )
    result = struct_directory / "source.json"
    if not result.is_file():
        raise RuntimeError("All-In-One did not create the expected result")
    return result


def energy_direction(candidate: dict) -> str:
    difference = float(candidate["after_energy"]) - float(candidate["before_energy"])
    if difference >= ENERGY_DIRECTION_THRESHOLD:
        return "rising"
    if difference <= -ENERGY_DIRECTION_THRESHOLD:
        return "falling"
    return "stable"


def energy_state(value: float) -> str:
    if value <= ENERGY_LOW_THRESHOLD:
        return "low"
    if value >= ENERGY_HIGH_THRESHOLD:
        return "high"
    return "medium"


def nearest_section_boundary(time_seconds: float, segments: list[dict]) -> dict | None:
    if not segments:
        return None
    segment = min(segments, key=lambda item: abs(float(item["start"]) - time_seconds))
    return {
        "time_seconds": float(segment["start"]),
        "distance_seconds": abs(float(segment["start"]) - time_seconds),
        "weak_label_after": segment["label"],
        "source": "automatic_all_in_one",
        "used_in_candidate_score": False,
    }


def enrich_candidates(candidate_sets: dict, segments: list[dict]) -> dict:
    for candidates in candidate_sets.values():
        for candidate in candidates:
            candidate["boundary_direction"] = energy_direction(candidate)
            candidate["pre_cue_energy_state"] = energy_state(
                float(candidate["before_energy"])
            )
            candidate["post_cue_energy_state"] = energy_state(
                float(candidate["after_energy"])
            )
            candidate["section_boundary"] = nearest_section_boundary(
                float(candidate["time_seconds"]), segments
            )
    return candidate_sets


def reviewed_seconds_to_beats(
    bars: list[dict], reviewed_seconds: list[float]
) -> list[int]:
    beats = []
    for requested in reviewed_seconds:
        nearest = min(bars, key=lambda bar: abs(float(bar["start_seconds"]) - requested))
        error = abs(float(nearest["start_seconds"]) - requested)
        if error > REVIEWED_CUE_TOLERANCE_SECONDS:
            raise ValueError(
                f"reviewed cue {requested:.6f}s is not near a normalized downbeat"
            )
        beats.append(int(nearest["beat_index"]))
    return beats


def attach_human_reviews(
    candidate_sets: dict,
    entry_seconds: list[float],
    exit_seconds: list[float],
) -> None:
    for role, reviewed in (("entry", entry_seconds), ("exit", exit_seconds)):
        for requested in reviewed:
            candidate = min(
                candidate_sets[role],
                key=lambda item: abs(float(item["time_seconds"]) - requested),
            )
            if abs(float(candidate["time_seconds"]) - requested) > REVIEWED_CUE_TOLERANCE_SECONDS:
                raise ValueError("reviewed cue was not retained in candidate output")
            candidate["human_review"] = {
                "status": "preferred_in_blind_transition",
                "reference_time_seconds": requested,
                "timing_basis": "nearest_normalized_downbeat",
            }


def validate_normalized_rhythm(result: dict, duration_seconds: float) -> None:
    beats = result["beats"]
    positions = result["beat_positions"]
    if len(beats) != len(positions) or len(beats) < 2:
        raise ValueError("invalid normalized beat grid")
    if any(float(right) <= float(left) for left, right in zip(beats, beats[1:])):
        raise ValueError("normalized beats must be strictly increasing")
    expected = [((int(positions[0]) - 1 + index) % 4) + 1 for index in range(len(positions))]
    if positions != expected:
        raise ValueError("normalized beat positions must cycle through 4/4")
    if float(beats[-1]) > duration_seconds:
        raise ValueError("normalized beat grid exceeds source duration")


def build_analysis(
    source: Path,
    raw_result_path: Path,
    timing_review_status: str,
    limit: int = 5,
    reviewed_entry_seconds: list[float] | None = None,
    reviewed_exit_seconds: list[float] | None = None,
) -> dict:
    source = source.resolve()
    source_hash = sha256_file(source)
    metadata = audio_metadata(source)
    raw_bytes = raw_result_path.read_bytes()
    raw_result = json.loads(raw_bytes)
    normalized, normalization = normalize_beats(raw_result)
    normalization["source_audio_sha256"] = source_hash
    validate_normalized_rhythm(normalized, metadata["duration_seconds"])

    beats = [float(value) for value in normalized["beats"]]
    positions = [int(value) for value in normalized["beat_positions"]]
    analysis_for_bars = {"rhythm": {"beats_seconds": beats}}
    annotations = {
        "downbeat_anchors": [
            {"beat_index_zero_based": positions.index(1), "meter": "4/4"}
        ]
    }
    features = frame_features(decode_mono(source))
    bars = aggregate_bars(analysis_for_bars, annotations, features)
    reviewed_entry_seconds = reviewed_entry_seconds or []
    reviewed_exit_seconds = reviewed_exit_seconds or []
    candidates = build_candidates(
        bars,
        limit,
        reviewed_seconds_to_beats(bars, reviewed_entry_seconds),
        reviewed_seconds_to_beats(bars, reviewed_exit_seconds),
    )
    attach_human_reviews(
        candidates, reviewed_entry_seconds, reviewed_exit_seconds
    )
    candidates = enrich_candidates(candidates, normalized.get("segments", []))

    cache_payload = {
        "analysis_version": ANALYSIS_VERSION,
        "source_sha256": source_hash,
        "allin1_result_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "settings": {
            "candidate_limit": limit,
            "energy_direction_threshold": ENERGY_DIRECTION_THRESHOLD,
            "energy_low_threshold": ENERGY_LOW_THRESHOLD,
            "energy_high_threshold": ENERGY_HIGH_THRESHOLD,
            "timing_review_status": timing_review_status,
            "reviewed_entry_seconds": reviewed_entry_seconds,
            "reviewed_exit_seconds": reviewed_exit_seconds,
        },
    }
    cache_key = hashlib.sha256(
        json.dumps(cache_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    segments = [
        {
            "start_seconds": float(segment["start"]),
            "end_seconds": float(segment["end"]),
            "weak_label": segment["label"],
            "label_source": "automatic_all_in_one",
            "label_used_in_candidate_score": False,
        }
        for segment in normalized.get("segments", [])
    ]
    return {
        "schema_version": ANALYSIS_VERSION,
        "schema_uri": "schemas/dj-track-analysis-v1.schema.json",
        "analysis_cache_key": cache_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "audio_sha256": source_hash,
        "source_filename": source.name,
        "duration_seconds": metadata["duration_seconds"],
        "audio": metadata,
        "rhythm": {
            "bpm": float(normalized["bpm"]),
            "meter": "4/4",
            "beat_count": len(beats),
            "beats_seconds": beats,
            "beat_positions": positions,
            "downbeats_seconds": [float(value) for value in normalized["downbeats"]],
            "source": "automatic_all_in_one",
            "timing_review_status": timing_review_status,
            "normalization": normalization,
        },
        "structure": {
            "segments": segments,
            "labels_are_weak_metadata": True,
            "labels_used_in_candidate_score": False,
        },
        "settings": cache_payload["settings"],
        "bar_count": len(bars),
        "bars": bars,
        "candidates": candidates,
        "provenance": {
            "beat_grid": "automatic_all_in_one_with_deterministic_octave_repair",
            "beat_grid_human_review": timing_review_status,
            "downbeat_phase": "automatic_all_in_one_not_separately_human_confirmed",
            "bar_features": "automatic_dsp",
            "cue_scores": "deterministic_heuristic",
            "semantic_section_labels": "automatic_weak_metadata_not_scored",
            "phrase_boundaries": "not_confirmed",
            "ai_transition_selection": "not_run",
        },
    }


def validate_output(result: dict) -> None:
    required = {
        "schema_version",
        "analysis_cache_key",
        "audio_sha256",
        "duration_seconds",
        "rhythm",
        "structure",
        "bars",
        "candidates",
        "provenance",
    }
    missing = required - result.keys()
    if missing:
        raise ValueError(f"analysis output is missing fields: {sorted(missing)}")
    if result["schema_version"] != ANALYSIS_VERSION:
        raise ValueError("unexpected analysis schema version")
    if result["rhythm"]["timing_review_status"] not in {
        "unreviewed",
        "human_confirmed",
    }:
        raise ValueError("invalid timing review status")
    for role in ("entry", "exit"):
        if not isinstance(result["candidates"].get(role), list):
            raise ValueError(f"candidate role {role} must be a list")
        for candidate in result["candidates"][role]:
            for field in (
                "beat_index",
                "time_seconds",
                "boundary_direction",
                "post_cue_energy_state",
                "selection_source",
            ):
                if field not in candidate:
                    raise ValueError(f"candidate is missing {field}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--allin1-result", type=Path)
    parser.add_argument("--work-directory", type=Path,
                        help="Isolated directory for all intermediate audio, neural results and features")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--timing-review-status",
        choices=("unreviewed", "human_confirmed"),
        default="unreviewed",
    )
    parser.add_argument(
        "--reviewed-entry-seconds", action="append", type=float, default=[]
    )
    parser.add_argument(
        "--reviewed-exit-seconds", action="append", type=float, default=[]
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 1:
        raise ValueError("--limit must be positive")
    source_hash = sha256_file(args.source)
    work_directory = (args.work_directory or DEFAULT_ARTIFACTS / source_hash).resolve()
    raw_result = args.allin1_result or run_allin1(args.source, work_directory, args.device)
    result = build_analysis(
        args.source,
        raw_result,
        args.timing_review_status,
        args.limit,
        args.reviewed_entry_seconds,
        args.reviewed_exit_seconds,
    )
    validate_output(result)
    output = args.output or work_directory / "track-analysis-v1.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        f"analyzed {args.source.name} beats={result['rhythm']['beat_count']} "
        f"bars={result['bar_count']} entries={len(result['candidates']['entry'])} "
        f"exits={len(result['candidates']['exit'])} output={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Normalize All-In-One tempo-octave switches to one DJ beat grid."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path


def normalize_beats(result: dict) -> tuple[dict, dict]:
    beats = result["beats"]
    positions = result["beat_positions"]
    bpm = float(result["bpm"])
    if len(beats) < 2 or len(beats) != len(positions) or bpm <= 0:
        raise ValueError("invalid All-In-One beat data")

    target_period = 60.0 / bpm
    normalized = [float(beats[0])]
    inserted = 0
    for left, right in zip(beats, beats[1:]):
        gap = float(right) - float(left)
        if gap <= 0:
            raise ValueError("beats must be strictly increasing")
        steps = max(1, round(gap / target_period))
        # Only repair credible integer tempo-octave gaps. Large untracked
        # regions remain explicit rather than manufacturing a long fake grid.
        if steps > 4 or abs(gap / steps - target_period) > target_period * 0.20:
            steps = 1
        for step in range(1, steps + 1):
            normalized.append(float(left) + gap * step / steps)
        inserted += steps - 1

    first_position = int(positions[0])
    normalized_positions = [
        ((first_position - 1 + index) % 4) + 1 for index in range(len(normalized))
    ]
    downbeats = [
        beat
        for beat, position in zip(normalized, normalized_positions, strict=True)
        if position == 1
    ]

    output = deepcopy(result)
    output["beats"] = normalized
    output["beat_positions"] = normalized_positions
    output["downbeats"] = downbeats
    output["normalization"] = {
        "method": "integer_tempo_octave_gap_interpolation_v1",
        "target_bpm": bpm,
        "target_period_seconds": target_period,
        "original_beat_count": len(beats),
        "normalized_beat_count": len(normalized),
        "inserted_beat_count": inserted,
        "preserves_original_beat_timestamps": True,
        "human_reviewed_reason": "half_tempo_switch_in_sparse_bridge",
    }
    return output, output["normalization"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--source-sha256",
        help="bind the converted WAV result to its original source audio",
    )
    args = parser.parse_args()
    result = json.loads(args.input.read_text(encoding="utf-8"))
    normalized, diagnostics = normalize_beats(result)
    if args.source_sha256:
        if len(args.source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in args.source_sha256
        ):
            raise ValueError("--source-sha256 must be 64 lowercase hex characters")
        diagnostics["source_audio_sha256"] = args.source_sha256
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(normalized, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(diagnostics, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

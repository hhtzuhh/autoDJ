"""Interchangeable audio-file -> dj-track-analysis-v1 adapters.

DSP is product-local. The optional All-In-One adapter retains the existing
workspace implementation and its isolated model environment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from analysis_config import BACKENDS, DSP_VERSION, analysis_root, selected_backend


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_analysis(result: dict) -> None:
    """Validate cross-field timing invariants used by pair scoring/execution."""
    if result["schema_version"] != "dj-track-analysis-v1":
        raise ValueError("unsupported track analysis schema")
    duration = float(result["duration_seconds"])
    rhythm = result["rhythm"]
    beats, positions = rhythm["beats_seconds"], rhythm["beat_positions"]
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(rhythm["bpm"]) or rhythm["bpm"] <= 0:
        raise ValueError("invalid duration or BPM")
    if len(beats) < 16 or len(beats) != len(positions) or rhythm["beat_count"] != len(beats):
        raise ValueError("invalid beat count")
    if any(not math.isfinite(t) or t < 0 or t > duration for t in beats) or any(b <= a for a, b in zip(beats, beats[1:])):
        raise ValueError("beats must increase within source duration")
    if positions != [((positions[0] - 1 + i) % 4) + 1 for i in range(len(beats))]:
        raise ValueError("invalid 4/4 beat positions")
    if rhythm["downbeats_seconds"] != [t for t, p in zip(beats, positions) if p == 1]:
        raise ValueError("downbeats do not match beat positions")
    bars = {bar["beat_index"]: bar for bar in result["bars"]}
    if not bars:
        raise ValueError("no complete bars")
    for bar in bars.values():
        i = bar["beat_index"]
        if i < 0 or i + 4 >= len(beats) or positions[i] != 1 or bar["start_seconds"] != beats[i] or bar["end_seconds"] != beats[i + 4]:
            raise ValueError("bar does not match beat grid")
    for role in ("entry", "exit"):
        if not result["candidates"][role]:
            raise ValueError(f"no {role} candidates")
        for cue in result["candidates"][role]:
            i = cue["beat_index"]
            if i not in bars or cue["time_seconds"] != beats[i]:
                raise ValueError("cue does not match bar grid")
    json.dumps(result, allow_nan=False)


def dsp_analysis(source: Path, work: Path, limit: int, workspace: Path) -> dict:
    from importlib.metadata import version
    from analysis_features import decode_mono, frame_features, aggregate_bars, build_candidates
    from analysis_downbeat import beat_features, rank_phases

    source_hash = file_hash(source)
    settings = {"backend": "essentia_dsp", "version": DSP_VERSION,
                "essentia_version": version("essentia"), "numpy_version": version("numpy"),
                "sample_rate": 44100, "method": "multifeature", "meter": "4/4",
                "candidate_limit": limit, "decoder": "ffmpeg_f32le_mono"}
    key = digest({"audio_sha256": source_hash, "settings": settings})
    output = work / "track-analysis-v1.json"
    if output.exists():
        try:
            cached = json.loads(output.read_text())
            if cached.get("analysis_cache_key") == key and cached.get("audio_sha256") == source_hash:
                validate_analysis(cached)
                cached["source_filename"] = source.name
                return cached
        except (ValueError, KeyError, TypeError):
            pass  # A corrupt cache is recomputed, never treated as ready.

    import essentia.standard as es
    audio = decode_mono(source)
    duration = len(audio) / 44100
    bpm, raw_beats, confidence, _, _ = es.RhythmExtractor2013(method="multifeature")(audio)
    beats = [float(t) for t in raw_beats]
    if len(beats) < 40:
        raise ValueError("insufficient tracked beats for bar/cue analysis")
    ranking = rank_phases(beat_features(audio, beats))
    phase = ranking["predicted_beat_index_modulo"]
    positions = [((i - phase) % 4) + 1 for i in range(len(beats))]
    rhythm = {"bpm": float(bpm), "meter": "4/4", "beat_count": len(beats),
              "beats_seconds": beats, "beat_positions": positions,
              "downbeats_seconds": [t for t, p in zip(beats, positions) if p == 1],
              "source": "automatic_essentia_dsp", "timing_review_status": "unreviewed",
              "downbeat_review_status": "estimated", "confidence": float(confidence),
              "confidence_is_calibrated_probability": False,
              "downbeat_estimation": {**ranking, "auto_accept": False, "method": "acoustic_accent_v1"},
              "normalization": {"method": "none", "preserves_original_beat_timestamps": True,
                                "half_or_double_tempo_reviewed": False}}
    bars = aggregate_bars({"rhythm": rhythm}, {"downbeat_anchors": [
        {"beat_index_zero_based": phase, "meter": "4/4", "source": "estimated"}
    ]}, frame_features(audio))
    candidates = build_candidates(bars, limit)
    def state(energy):
        return "low" if energy <= .33 else "high" if energy >= .67 else "medium"
    for rows in candidates.values():
        for cue in rows:
            change = cue["after_energy"] - cue["before_energy"]
            cue.update(boundary_direction="rising" if change >= .12 else "falling" if change <= -.12 else "stable",
                       pre_cue_energy_state=state(cue["before_energy"]),
                       post_cue_energy_state=state(cue["after_energy"]), section_boundary=None)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels,codec_name", "-of", "json", str(source)],
        check=True, capture_output=True, text=True)
    stream = json.loads(probe.stdout)["streams"][0]
    result = {"schema_version": "dj-track-analysis-v1",
              "schema_uri": "schemas/dj-track-analysis-v1.schema.json",
              "analysis_cache_key": key, "created_at": datetime.now(timezone.utc).isoformat(),
              "audio_sha256": source_hash, "source_filename": source.name,
              "duration_seconds": duration,
              "audio": {"duration_seconds": duration, "sample_rate": int(stream["sample_rate"]),
                        "channels": int(stream["channels"]), "codec": stream["codec_name"]},
              "rhythm": rhythm, "structure": {"segments": [], "labels_are_weak_metadata": True,
                                               "labels_used_in_candidate_score": False},
              "settings": settings, "bar_count": len(bars), "bars": bars, "candidates": candidates,
              "provenance": {"backend": "essentia_dsp", "analysis_version": DSP_VERSION,
                  "model_based": False, "beat_grid": "essentia_multifeature",
                  "beat_grid_human_review": "unreviewed", "downbeat_phase": "acoustic_accent_estimate_not_human_confirmed",
                  "bar_features": "automatic_dsp", "cue_scores": "deterministic_heuristic",
                  "semantic_section_labels": "unavailable", "phrase_boundaries": "not_confirmed",
                  "ai_transition_selection": "not_run"},
              "warnings": ["Downbeat phase is a provisional 4/4 estimate; audition before performance.",
                           "Half/double tempo and beat timing are unreviewed; semantic sections unavailable."]}
    validate_analysis(result)
    return result


def all_in_one_analysis(source: Path, work: Path, limit: int, workspace: Path) -> dict:
    script = workspace / "experiments/time-map/unified_analysis.py"
    python = workspace / "experiments/time-map/.venv/bin/python"
    if not script.is_file() or not python.is_file():
        raise RuntimeError("Optional All-In-One backend needs the existing workspace experiment environments")
    staging = work / "allin1-adapter-result.json"
    subprocess.run([str(python), str(script), str(source), "--work-directory", str(work),
                    "--output", str(staging), "--device", "cpu", "--limit", str(limit)], check=True)
    result = json.loads(staging.read_text())
    result["provenance"].update(backend="all_in_one", model_based=True)
    validate_analysis(result)
    return result


BACKEND_RUNNERS = {"essentia_dsp": dsp_analysis, "all_in_one": all_in_one_analysis}


def analyze(source: Path, output: Path, backend: str, workspace: Path, limit: int = 5) -> dict:
    if backend not in BACKENDS or limit < 1:
        raise ValueError("invalid analyzer backend or candidate limit")
    source, output = source.resolve(), output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    result = BACKEND_RUNNERS[backend](source, output.parent, limit, workspace)
    if result["audio_sha256"] != file_hash(source):
        raise ValueError("analyzer returned a different audio identity")
    validate_analysis(result)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(output)
    return result


def run_for_product(source: Path, output: Path, backend: str, workspace: Path) -> dict:
    """Run heavy imports in an isolated process, keeping the bridge responsive."""
    old_python = workspace / "experiments/time-map/.venv/bin/python"
    python = os.environ.get("DJ_ANALYSIS_PYTHON") or (str(old_python) if old_python.is_file() else sys.executable)
    neural_cached = (output.parent / "allin1-raw/source.json").is_file()
    output.parent.mkdir(parents=True, exist_ok=True)
    with (output.parent / "analyzer.log").open("a") as log:
        subprocess.run([python, str(Path(__file__).resolve()), str(source), "--output", str(output),
                        "--backend", backend, "--workspace", str(workspace)],
                       check=True, stdout=log, stderr=subprocess.STDOUT)
    return {"neuralNet": "not_used" if backend == "essentia_dsp" else "cache" if neural_cached else "computed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--backend", choices=BACKENDS, default=selected_backend())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    output = args.output or analysis_root(Path(__file__).resolve().parents[1], args.backend) / file_hash(args.source) / "track-analysis-v1.json"
    result = analyze(args.source, output, args.backend, args.workspace, args.limit)
    print(json.dumps({"backend": args.backend, "output": str(output), "bpm": result["rhythm"]["bpm"],
                      "beats": result["rhythm"]["beat_count"], "bars": result["bar_count"]}))

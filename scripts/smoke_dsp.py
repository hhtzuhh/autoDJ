"""Exercise real DSP -> pair candidates -> executor validation, without playback."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / "backend"))
from track_analyzer import file_hash, validate_analysis
from pair_analyzer import build_pairs


def synthesize(path, bpm, tone):
    """Self-created kick/hat pulses; a test signal, not a curated demo song."""
    rate, seconds = 44100, 96
    audio = np.zeros(rate * seconds)
    rng = np.random.default_rng(42)
    for beat in range(int(seconds * bpm / 60)):
        start = round(beat * 60 / bpm * rate)
        t = np.arange(round(.18 * rate)) / rate
        kick = np.sin(2 * np.pi * tone * t) * np.exp(-t * 30)
        kick *= .65 if beat % 4 == 0 else .45
        audio[start:start + len(kick)] += kick[:len(audio) - start]
        hat_start = start + round(30 / bpm * rate)
        hat = rng.normal(0, .08, round(.035 * rate)) * np.exp(-np.arange(round(.035 * rate)) / 350)
        audio[hat_start:hat_start + len(hat)] += hat[:max(0, len(audio) - hat_start)]
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        stream.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def main():
    with tempfile.TemporaryDirectory(prefix="dj-dsp-smoke-") as raw:
        root = Path(raw)
        cache = root / "artifacts/time-map/essentia-dsp-v1"
        env = {**os.environ, "DJ_DATA_ROOT": str(root), "DJ_ANALYSIS_ROOT": str(cache),
               "DJ_RUNTIME_DIR": str(root / "artifacts/mixxx-runtime"),
               "DJ_ANALYSIS_BACKEND": "essentia_dsp"}
        tracks = []
        for name, bpm, tone in (("a", 120, 60), ("b", 122, 65)):
            source = root / f"{name}.wav"
            synthesize(source, bpm, tone)
            output = cache / file_hash(source) / "track-analysis-v1.json"
            subprocess.run([sys.executable, str(PRODUCT / "backend/track_analyzer.py"),
                            str(source), "--backend", "essentia_dsp", "--output", str(output)],
                           cwd=root, env=env, check=True)
            track = json.loads(output.read_text())
            validate_analysis(track)
            assert track["provenance"]["model_based"] is False
            tracks.append(track)
        pairing = build_pairs(*tracks, 10)
        assert pairing["pairs"], "No feasible pair candidates from synthetic tracks"
        pair = next(p for p in pairing["pairs"] if p.get("transition_metrics_by_bars"))
        bars = min(int(n) for n in pair["transition_metrics_by_bars"])
        start, entry = pair["a_exit"]["beat_index"], pair["b_entry"]["beat_index"]
        plan = {"schema_version": 1, "plan_id": "synthetic-smoke-not-ai",
                "source": "synthetic_smoke_not_ai", "completion_mode": "continue_target",
                "deck_a": {"deck": 1, "play_start_beat_index": start - 8,
                           "transition_start_beat_index": start, "transition_end_beat_index": start + bars * 4},
                "deck_b": {"deck": 2, "cue_beat_index": entry},
                "mix": {"bars": bars, "beats": bars * 4, "beats_per_bar": 4,
                        "crossfader_curve": "linear", "steps": bars * 16 + 1,
                        "tail_beats": 8, "tempo_source": "deck_a_sync_leader"}}
        for key, track in zip(("deck_a", "deck_b"), tracks):
            plan[key].update(audio_sha256=track["audio_sha256"],
                             analysis_cache_key=track["analysis_cache_key"],
                             analysis_filename="track-analysis-v1.json")
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps(plan))
        result = subprocess.run([sys.executable, str(PRODUCT / "backend/transition.py"), str(plan_path)],
                                env=env, cwd=root, check=True, capture_output=True, text=True)
        assert json.loads(result.stdout)["plan"]["plan_id"] == plan["plan_id"]
        print(json.dumps({"status": "ok", "analyzer": "essentia_dsp", "tracks": len(tracks),
                          "pair_candidates": len(pairing["pairs"]), "executor": "dry_run",
                          "meta_calls": 0, "playback_started": False}))


if __name__ == "__main__":
    main()

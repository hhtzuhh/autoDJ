"""Legacy acoustic-accent phase estimator, explicitly uncalibrated.

Adapted from experiments/time-map/auto_downbeat.py. This baseline previously
matched only one of two reviewed tracks. Never label its output confirmed.
"""
import numpy as np
SAMPLE_RATE = 44_100
MIN_CONFIDENCE_MARGIN = 0.18

def band_energy(spectrum: np.ndarray, frequencies: np.ndarray, low: int, high: int) -> float:
    selected = spectrum[(frequencies >= low) & (frequencies < high)]
    return float(np.mean(np.square(selected, dtype=np.float64)))


def spectrum(audio: np.ndarray, start: int, frame_size: int) -> np.ndarray:
    frame = np.zeros(frame_size, dtype=np.float64)
    source_start = max(0, start)
    source_end = min(len(audio), start + frame_size)
    if source_end > source_start:
        destination_start = source_start - start
        frame[destination_start : destination_start + source_end - source_start] = audio[
            source_start:source_end
        ]
    frame *= np.hanning(frame_size)
    return np.abs(np.fft.rfft(frame))


def robust_zscore(values: np.ndarray) -> np.ndarray:
    median = np.median(values)
    deviation = np.median(np.abs(values - median))
    if deviation < 1e-12:
        standard_deviation = np.std(values)
        if standard_deviation < 1e-12:
            return np.zeros_like(values)
        return np.clip((values - np.mean(values)) / standard_deviation, -4.0, 4.0)
    return np.clip((values - median) / (1.4826 * deviation), -4.0, 4.0)


def beat_features(audio: np.ndarray, beats: list[float]) -> list[dict[str, float]]:
    frame_size = 4096
    frequencies = np.fft.rfftfreq(frame_size, 1 / SAMPLE_RATE)
    pre_offset = round(-0.105 * SAMPLE_RATE)
    post_offset = round(-0.010 * SAMPLE_RATE)
    rows = []
    for index, beat in enumerate(beats):
        beat_sample = round(beat * SAMPLE_RATE)
        before = spectrum(audio, beat_sample + pre_offset, frame_size)
        after = spectrum(audio, beat_sample + post_offset, frame_size)
        before_low = band_energy(before, frequencies, 35, 180)
        after_low = band_energy(after, frequencies, 35, 180)
        before_full = band_energy(before, frequencies, 35, 8_000)
        after_full = band_energy(after, frequencies, 35, 8_000)
        positive_flux = float(
            np.mean(np.square(np.maximum(after - before, 0.0), dtype=np.float64))
        )
        rows.append(
            {
                "beat_index": index,
                "seconds": beat,
                "low_energy": np.log1p(after_low),
                "low_onset": np.log1p(max(0.0, after_low - before_low)),
                "full_onset": np.log1p(max(0.0, after_full - before_full)),
                "positive_flux": np.log1p(positive_flux),
            }
        )
    return rows


def rank_phases(rows: list[dict[str, float]]) -> dict:
    if len(rows) < 16:
        raise ValueError("at least 16 beats are required for phase ranking")
    feature_names = ["low_energy", "low_onset", "full_onset", "positive_flux"]
    weights = np.array([0.30, 0.30, 0.15, 0.25])
    matrix = np.array([[row[name] for name in feature_names] for row in rows])
    normalized = np.column_stack(
        [robust_zscore(matrix[:, column]) for column in range(matrix.shape[1])]
    )
    accent = normalized @ weights

    phase_results = []
    for phase in range(4):
        selected = accent[phase::4]
        others = np.concatenate([accent[other::4] for other in range(4) if other != phase])
        score = float(np.mean(selected) - np.mean(others))
        phase_results.append(
            {
                "phase": phase + 1,
                "beat_index_modulo": phase,
                "score": score,
                "accent_mean": float(np.mean(selected)),
                "beat_count": int(len(selected)),
            }
        )

    ranked = sorted(phase_results, key=lambda item: item["score"], reverse=True)
    margin = ranked[0]["score"] - ranked[1]["score"]
    return {
        "predicted_phase": ranked[0]["phase"],
        "predicted_beat_index_modulo": ranked[0]["beat_index_modulo"],
        "confidence_margin": margin,
        "auto_accept": margin >= MIN_CONFIDENCE_MARGIN,
        "minimum_accept_margin": MIN_CONFIDENCE_MARGIN,
        "phases": phase_results,
    }

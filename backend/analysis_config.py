"""Analyzer selection and separate caches; selection is fixed at bridge startup."""
import os
from pathlib import Path

BACKENDS = ("essentia_dsp", "all_in_one")
DSP_VERSION = "essentia-dsp-v1"


def selected_backend() -> str:
    name = os.environ.get("DJ_ANALYSIS_BACKEND", "essentia_dsp")
    if name not in BACKENDS:
        raise ValueError(f"DJ_ANALYSIS_BACKEND must be one of {BACKENDS}")
    return name


def analysis_root(data_root: Path, backend: str | None = None) -> Path:
    name = backend or selected_backend()
    if name not in BACKENDS:
        raise ValueError(f"unknown analysis backend: {name}")
    base = data_root / "artifacts/time-map"
    # Keep the original All-In-One artifacts in place and usable on switchback.
    return base if name == "all_in_one" else base / DSP_VERSION

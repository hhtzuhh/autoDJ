"""Product-local defaults shared by the bridge and its executor subprocess."""
import os
from pathlib import Path
from analysis_config import analysis_root

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path(os.environ.get("DJ_DATA_ROOT", PRODUCT_ROOT)).resolve()
RUNTIME_ROOT = Path(os.environ.get("DJ_RUNTIME_DIR", DATA_ROOT / "artifacts/mixxx-runtime")).resolve()
ANALYSIS_ROOT = Path(os.environ.get("DJ_ANALYSIS_ROOT", analysis_root(DATA_ROOT))).resolve()

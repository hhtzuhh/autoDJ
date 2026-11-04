"""Create local folders or archive generated data for a fresh pipeline run."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import socket

PRODUCT_ROOT = Path(__file__).resolve().parents[1]


def initialize(root: Path) -> None:
    (root / "local-assets/samples-v3").mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir(parents=True, exist_ok=True)


def fresh_outputs(root: Path) -> Path | None:
    # Do not archive state while a bridge/analyzer or camera job owns it.
    for port in (8765, 8767):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                raise RuntimeError(f"Stop the service on port {port} before starting a fresh run.")
        except (ConnectionRefusedError, TimeoutError):
            pass
    initialize(root)
    entries = [p for p in (root / "artifacts").iterdir() if p.name != "README.md"]
    if not entries:
        return None
    archive = root / ".local/archives" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") / "artifacts"
    archive.mkdir(parents=True)
    for entry in entries:
        entry.rename(archive / entry.name)
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true", help="Archive outputs; keep songs")
    args = parser.parse_args()
    initialize(PRODUCT_ROOT)
    if args.fresh:
        archive = fresh_outputs(PRODUCT_ROOT)
        print(f"Previous outputs archived in {archive}" if archive else "Outputs already empty.")
    print(f"Songs: {PRODUCT_ROOT / 'local-assets/samples-v3'}")
    print(f"Outputs: {PRODUCT_ROOT / 'artifacts'}")


if __name__ == "__main__":
    main()

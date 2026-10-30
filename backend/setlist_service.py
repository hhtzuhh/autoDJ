"""Resolve an analyzed performance queue into an exact Mixxx playlist."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable

from transition_pipeline import AnalysisCatalog


@dataclass(frozen=True)
class SetListTrack:
    track_id: str
    source_filename: str
    source_path: str


@dataclass(frozen=True)
class SetListManifest:
    schema_version: int
    queue_revision: int
    ordered_tracks: tuple[SetListTrack, ...]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "queue_revision": self.queue_revision,
            "ordered_tracks": [asdict(track) for track in self.ordered_tracks],
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_set_list(
    catalog: AnalysisCatalog,
    queue_payload: dict,
    library_roots: Iterable[Path],
) -> SetListManifest:
    roots = tuple(path.resolve() for path in library_roots)
    if not roots:
        raise ValueError("at least one local library root is required")
    ordered: list[SetListTrack] = []
    for value in queue_payload["ordered_track_ids"]:
        track_id = str(value)
        analysis = catalog.tracks.get(track_id)
        if analysis is None:
            raise ValueError(f"set list references unanalyzed track: {track_id}")
        matches = tuple(
            candidate.resolve()
            for root in roots
            for candidate in root.rglob(analysis.source_filename)
            if candidate.is_file()
        )
        if len(matches) != 1:
            raise ValueError(
                f"expected one local file for {analysis.source_filename}, found {len(matches)}"
            )
        source = matches[0]
        if sha256_file(source) != track_id:
            raise ValueError(f"audio identity mismatch: {analysis.source_filename}")
        ordered.append(
            SetListTrack(
                track_id=track_id,
                source_filename=analysis.source_filename,
                source_path=str(source),
            )
        )
    return SetListManifest(
        schema_version=1,
        queue_revision=int(queue_payload["revision"]),
        ordered_tracks=tuple(ordered),
    )


def write_mixxx_playlist(
    manifest: SetListManifest,
    playlist_path: Path,
    manifest_path: Path,
) -> None:
    playlist_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    playlist_path.write_text(
        "#EXTM3U\n"
        + "".join(f"{track.source_path}\n" for track in manifest.ordered_tracks),
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-root", type=Path, required=True)
    parser.add_argument("--queue-state", type=Path, required=True)
    parser.add_argument("--library-root", type=Path, action="append", required=True)
    parser.add_argument("--playlist", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog = AnalysisCatalog.from_root(args.analysis_root)
    queue_payload = json.loads(args.queue_state.read_text(encoding="utf-8"))
    manifest = resolve_set_list(catalog, queue_payload, args.library_root)
    write_mixxx_playlist(manifest, args.playlist, args.manifest)
    print(
        json.dumps(
            {
                "event": "mixxx_set_list_written",
                "queue_revision": manifest.queue_revision,
                "track_count": len(manifest.ordered_tracks),
                "playlist": str(args.playlist.resolve()),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

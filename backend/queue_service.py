"""Persistent queue contract for lifecycle supervision and live recovery.

Pair generation is shared with the product transition pipeline.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable

QUEUE_SCHEMA_VERSION = 1

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

@dataclass(frozen=True)
class TrackRef:
    track_id: str
    analysis_cache_key: str
    source_filename: str
    analysis_path: Path


class AnalysisCatalog:
    def __init__(self, tracks: dict[str, TrackRef]) -> None:
        self.tracks = tracks

    @classmethod
    def from_root(cls, root: Path) -> "AnalysisCatalog":
        tracks: dict[str, TrackRef] = {}
        for path in sorted(root.glob("*/track-analysis-v1.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            track_id = str(payload["audio_sha256"])
            if track_id in tracks:
                raise ValueError(f"duplicate track analysis for {track_id}")
            tracks[track_id] = TrackRef(
                track_id=track_id,
                analysis_cache_key=str(payload["analysis_cache_key"]),
                source_filename=str(payload["source_filename"]),
                analysis_path=path,
            )
        return cls(tracks)

    def resolve(self, identifier: str) -> TrackRef:
        exact = self.tracks.get(identifier)
        if exact:
            return exact
        matches = [track for key, track in self.tracks.items() if key.startswith(identifier)]
        if not matches:
            raise ValueError(f"track is not analyzed: {identifier}")
        if len(matches) > 1:
            raise ValueError(f"track prefix is ambiguous: {identifier}")
        return matches[0]

    def resolve_many(self, identifiers: Iterable[str]) -> tuple[str, ...]:
        return tuple(self.resolve(identifier).track_id for identifier in identifiers)


@dataclass(frozen=True)
class QueueSnapshot:
    revision: int
    ordered_track_ids: tuple[str, ...]
    updated_at: str
    schema_version: int = QUEUE_SCHEMA_VERSION

    @classmethod
    def empty(cls) -> "QueueSnapshot":
        return cls(revision=0, ordered_track_ids=(), updated_at=utc_now())

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "QueueSnapshot":
        if payload.get("schema_version") != QUEUE_SCHEMA_VERSION:
            raise ValueError("unsupported queue schema version")
        return cls(
            revision=int(payload["revision"]),
            ordered_track_ids=tuple(payload["ordered_track_ids"]),
            updated_at=str(payload["updated_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ordered_track_ids"] = list(self.ordered_track_ids)
        return payload


class QueueStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> QueueSnapshot:
        if not self.path.exists():
            return QueueSnapshot.empty()
        return QueueSnapshot.from_dict(
            json.loads(self.path.read_text(encoding="utf-8"))
        )

    def save(self, snapshot: QueueSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                snapshot.to_dict(), indent=2, ensure_ascii=False, sort_keys=True
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def load_pair_builder():
    from pair_analyzer import PAIR_VERSION, build_pairs
    return PAIR_VERSION, build_pairs

"""Versioned queue and adjacency-scoped transition candidate cache."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from pair_analyzer import PAIR_VERSION, build_pairs


MAX_QUEUE_TRACKS = 50
DEFAULT_PAIR_LIMIT = 25
PAIR_CACHE_SCHEMA_VERSION = 1
TransitionStatus = Literal[
    "pending",
    "analyzing",
    "analyzed",
    "planning",
    "ready",
    "feasible",
    "blocked",
    "invalidated",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def adjacent_pairs(track_ids: Iterable[str]) -> tuple[tuple[str, str], ...]:
    ordered = tuple(track_ids)
    return tuple(zip(ordered, ordered[1:]))


@dataclass(frozen=True)
class AnalysisTrack:
    track_id: str
    analysis_cache_key: str
    source_filename: str
    analysis_path: Path


class AnalysisCatalog:
    def __init__(self, tracks: dict[str, AnalysisTrack]) -> None:
        self.tracks = tracks

    @classmethod
    def from_root(cls, root: Path) -> "AnalysisCatalog":
        tracks: dict[str, AnalysisTrack] = {}
        for path in sorted(root.glob("*/track-analysis-v1.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            track_id = str(payload["audio_sha256"])
            if track_id in tracks:
                raise ValueError(f"duplicate track analysis: {track_id}")
            tracks[track_id] = AnalysisTrack(
                track_id=track_id,
                analysis_cache_key=str(payload["analysis_cache_key"]),
                source_filename=str(payload["source_filename"]),
                analysis_path=path,
            )
        return cls(tracks)


@dataclass(frozen=True)
class QueueSnapshot:
    revision: int
    ordered_track_ids: tuple[str, ...]
    updated_at: str

    @classmethod
    def empty(cls) -> "QueueSnapshot":
        return cls(0, (), utc_now())


@dataclass(frozen=True)
class QueueChange:
    changed: bool
    before: QueueSnapshot
    after: QueueSnapshot
    invalidated_pairs: tuple[tuple[str, str], ...]
    required_pairs: tuple[tuple[str, str], ...]
    retained_pairs: tuple[tuple[str, str], ...]


class VersionedQueue:
    def __init__(
        self,
        catalog: AnalysisCatalog,
        snapshot: QueueSnapshot | None = None,
        *,
        max_tracks: int = MAX_QUEUE_TRACKS,
    ) -> None:
        self.catalog = catalog
        self.snapshot = snapshot or QueueSnapshot.empty()
        self.max_tracks = max_tracks
        self._validate(self.snapshot.ordered_track_ids)

    def _validate(self, track_ids: tuple[str, ...]) -> None:
        if len(track_ids) > self.max_tracks:
            raise ValueError(f"queue supports at most {self.max_tracks} tracks")
        if len(set(track_ids)) != len(track_ids):
            raise ValueError("duplicate track identities are not supported")
        missing = [track_id for track_id in track_ids if track_id not in self.catalog.tracks]
        if missing:
            raise ValueError(f"queue references unanalyzed track: {missing[0]}")

    def set_order(self, track_ids: Iterable[str]) -> QueueChange:
        requested = tuple(track_ids)
        self._validate(requested)
        before = self.snapshot
        old_pairs = adjacent_pairs(before.ordered_track_ids)
        new_pairs = adjacent_pairs(requested)
        old_set = set(old_pairs)
        new_set = set(new_pairs)
        changed = requested != before.ordered_track_ids
        after = (
            QueueSnapshot(before.revision + 1, requested, utc_now())
            if changed
            else before
        )
        change = QueueChange(
            changed=changed,
            before=before,
            after=after,
            invalidated_pairs=tuple(pair for pair in old_pairs if pair not in new_set),
            required_pairs=tuple(pair for pair in new_pairs if pair not in old_set),
            retained_pairs=tuple(pair for pair in new_pairs if pair in old_set),
        )
        self.snapshot = after
        return change


@dataclass(frozen=True)
class PairResult:
    source_track_id: str
    target_track_id: str
    cache_key: str
    candidate_count: int
    cache_hit: bool


@dataclass(frozen=True)
class TransitionEdge:
    id: str
    source_track_id: str
    target_track_id: str
    status: TransitionStatus
    candidate_count: int
    selected_by: str | None
    reason: str | None
    queue_revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sourceTrackId": self.source_track_id,
            "targetTrackId": self.target_track_id,
            "status": self.status,
            "candidateCount": self.candidate_count,
            "selectedBy": self.selected_by,
            "reason": self.reason,
            "queueRevision": self.queue_revision,
        }

    def with_plan(
        self,
        *,
        status: TransitionStatus,
        selected_by: str | None,
        reason: str | None = None,
    ) -> "TransitionEdge":
        return replace(
            self,
            status=status,
            selected_by=selected_by,
            reason=reason,
        )


class PairAnalysisCache:
    def __init__(
        self,
        root: Path,
        catalog: AnalysisCatalog,
        *,
        limit: int = DEFAULT_PAIR_LIMIT,
        builder: Callable[[dict[str, Any], dict[str, Any], int], dict[str, Any]] = build_pairs,
        pair_version: str = PAIR_VERSION,
    ) -> None:
        if limit < 1:
            raise ValueError("pair limit must be positive")
        self.root = root
        self.catalog = catalog
        self.limit = limit
        self.builder = builder
        self.pair_version = pair_version

    def key_for(self, source_track_id: str, target_track_id: str) -> str:
        source = self.catalog.tracks[source_track_id]
        target = self.catalog.tracks[target_track_id]
        return canonical_hash(
            {
                "source_analysis_cache_key": source.analysis_cache_key,
                "target_analysis_cache_key": target.analysis_cache_key,
                "limit": self.limit,
                "pair_version": self.pair_version,
            }
        )

    def ensure(self, source_track_id: str, target_track_id: str) -> PairResult:
        source = self.catalog.tracks[source_track_id]
        target = self.catalog.tracks[target_track_id]
        cache_key = self.key_for(source_track_id, target_track_id)
        cache_path = self.root / f"{cache_key}.json"
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if payload.get("pair_cache_key") != cache_key:
                raise ValueError(f"pair cache key mismatch: {cache_path}")
            return PairResult(
                source_track_id,
                target_track_id,
                cache_key,
                len(payload["pairs"]),
                True,
            )

        source_payload = json.loads(source.analysis_path.read_text(encoding="utf-8"))
        target_payload = json.loads(target.analysis_path.read_text(encoding="utf-8"))
        pairing = self.builder(source_payload, target_payload, self.limit)
        result = {
            "schema_version": PAIR_CACHE_SCHEMA_VERSION,
            "pair_cache_key": cache_key,
            "pair_version": self.pair_version,
            "created_at": utc_now(),
            "parameters": {"limit": self.limit},
            "provenance": {
                "candidate_generation": "deterministic_heuristic_not_ai",
                "ai_selection": "not_run",
                "executable_plan": False,
            },
            "source": asdict(source) | {"analysis_path": str(source.analysis_path)},
            "target": asdict(target) | {"analysis_path": str(target.analysis_path)},
            **pairing,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(cache_path)
        return PairResult(
            source_track_id,
            target_track_id,
            cache_key,
            len(result["pairs"]),
            False,
        )

    def payload(self, source_track_id: str, target_track_id: str) -> dict[str, Any]:
        result = self.ensure(source_track_id, target_track_id)
        cache_path = self.root / f"{result.cache_key}.json"
        return json.loads(cache_path.read_text(encoding="utf-8"))


class TransitionPipeline:
    def __init__(self, queue: VersionedQueue, cache: PairAnalysisCache) -> None:
        self.queue = queue
        self.cache = cache

    def set_queue(
        self,
        track_ids: Iterable[str],
    ) -> tuple[QueueChange, tuple[TransitionEdge, ...]]:
        change = self.queue.set_order(track_ids)
        for source_id, target_id in change.required_pairs:
            self.cache.ensure(source_id, target_id)
        edges = tuple(
            self._edge_for(source_id, target_id, change.after.revision)
            for source_id, target_id in adjacent_pairs(change.after.ordered_track_ids)
        )
        return change, edges

    def _edge_for(
        self,
        source_track_id: str,
        target_track_id: str,
        queue_revision: int,
    ) -> TransitionEdge:
        result = self.cache.ensure(source_track_id, target_track_id)
        status: TransitionStatus = (
            "analyzed" if result.candidate_count > 0 else "blocked"
        )
        reason = None if result.candidate_count > 0 else "no_compatible_candidates"
        return TransitionEdge(
            id=f"edge-{result.cache_key[:16]}",
            source_track_id=source_track_id,
            target_track_id=target_track_id,
            status=status,
            candidate_count=result.candidate_count,
            selected_by=None,
            reason=reason,
            queue_revision=queue_revision,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare adjacency-scoped transition candidates for a queue."
    )
    parser.add_argument("--analysis-root", type=Path, required=True)
    parser.add_argument("--pair-root", type=Path, required=True)
    parser.add_argument("--pair-limit", type=int, default=DEFAULT_PAIR_LIMIT)
    parser.add_argument("tracks", nargs="+")
    args = parser.parse_args()

    catalog = AnalysisCatalog.from_root(args.analysis_root)
    pipeline = TransitionPipeline(
        VersionedQueue(catalog),
        PairAnalysisCache(args.pair_root, catalog, limit=args.pair_limit),
    )
    change, edges = pipeline.set_queue(args.tracks)
    print(
        json.dumps(
            {
                "queueRevision": change.after.revision,
                "requiredPairCount": len(change.required_pairs),
                "retainedPairCount": len(change.retained_pairs),
                "invalidatedPairCount": len(change.invalidated_pairs),
                "transitions": [edge.to_dict() for edge in edges],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

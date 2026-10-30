from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from pair_analyzer import build_pairs
from transition_pipeline import (
    AnalysisCatalog,
    PairAnalysisCache,
    TransitionPipeline,
    VersionedQueue,
)


def analysis_payload(track_id: str, index: int, *, bpm: float = 130.0) -> dict:
    bar_seconds = 240.0 / bpm
    bars = [
        {
            "beat_index": bar_index * 4,
            "start_seconds": bar_index * bar_seconds,
            "end_seconds": (bar_index + 1) * bar_seconds,
            "rms_normalized": 0.55 + (bar_index % 3) * 0.04,
            "low_ratio_normalized": 0.45,
            "positive_flux_normalized": 0.52,
        }
        for bar_index in range(20)
    ]
    return {
        "audio_sha256": track_id,
        "analysis_cache_key": f"analysis-{index:02d}",
        "source_filename": f"synthetic-{index:02d}.wav",
        "duration_seconds": 20 * bar_seconds,
        "rhythm": {"bpm": bpm},
        "bars": bars,
        "candidates": {
            "exit": [
                {
                    "rank": 1,
                    "beat_index": 16,
                    "time_seconds": 4 * bar_seconds,
                    "score": 0.8,
                }
            ],
            "entry": [
                {
                    "rank": 1,
                    "beat_index": 8,
                    "time_seconds": 2 * bar_seconds,
                    "score": 0.75,
                    "boundary_change": 0.4,
                }
            ],
        },
    }


def write_catalog(root: Path, count: int = 10) -> tuple[str, ...]:
    track_ids = []
    for index in range(count):
        track_id = f"{index + 1:064x}"
        track_ids.append(track_id)
        directory = root / f"track-{index:02d}"
        directory.mkdir(parents=True)
        (directory / "track-analysis-v1.json").write_text(
            json.dumps(analysis_payload(track_id, index)),
            encoding="utf-8",
        )
    return tuple(track_ids)


class TransitionPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.analysis_root = self.root / "analysis"
        self.track_ids = write_catalog(self.analysis_root)
        self.catalog = AnalysisCatalog.from_root(self.analysis_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ten_tracks_create_only_nine_adjacent_pair_analyses(self) -> None:
        calls: list[tuple[str, str]] = []

        def counted_builder(source: dict, target: dict, limit: int) -> dict:
            calls.append((source["audio_sha256"], target["audio_sha256"]))
            return build_pairs(source, target, limit)

        cache = PairAnalysisCache(
            self.root / "pairs",
            self.catalog,
            builder=counted_builder,
        )
        pipeline = TransitionPipeline(VersionedQueue(self.catalog), cache)

        change, edges = pipeline.set_queue(self.track_ids)

        self.assertEqual(change.after.revision, 1)
        self.assertEqual(len(change.required_pairs), 9)
        self.assertEqual(len(calls), 9)
        self.assertEqual(len(edges), 9)
        self.assertTrue(all(edge.status == "analyzed" for edge in edges))
        self.assertTrue(all(edge.candidate_count == 1 for edge in edges))

        _, cached_edges = pipeline.set_queue(self.track_ids)
        self.assertEqual(len(calls), 9)
        self.assertEqual(len(cached_edges), 9)

    def test_reorder_only_builds_new_adjacent_edges(self) -> None:
        calls = 0

        def counted_builder(source: dict, target: dict, limit: int) -> dict:
            nonlocal calls
            calls += 1
            return build_pairs(source, target, limit)

        pipeline = TransitionPipeline(
            VersionedQueue(self.catalog),
            PairAnalysisCache(
                self.root / "pairs",
                self.catalog,
                builder=counted_builder,
            ),
        )
        pipeline.set_queue(self.track_ids)
        reordered = (
            self.track_ids[0],
            self.track_ids[1],
            self.track_ids[3],
            self.track_ids[2],
            *self.track_ids[4:],
        )

        change, edges = pipeline.set_queue(reordered)

        self.assertEqual(change.after.revision, 2)
        self.assertEqual(len(change.invalidated_pairs), 3)
        self.assertEqual(len(change.required_pairs), 3)
        self.assertEqual(len(change.retained_pairs), 6)
        self.assertEqual(calls, 12)
        self.assertEqual(len(edges), 9)

    def test_incompatible_tempo_prepares_clean_break_without_enabling_sync_pair(self) -> None:
        target_id = self.track_ids[1]
        target_path = self.catalog.tracks[target_id].analysis_path
        target = json.loads(target_path.read_text(encoding="utf-8"))
        target["rhythm"]["bpm"] = 90.0
        target_path.write_text(json.dumps(target), encoding="utf-8")
        catalog = AnalysisCatalog.from_root(self.analysis_root)
        pipeline = TransitionPipeline(
            VersionedQueue(catalog),
            PairAnalysisCache(self.root / "blocked-pairs", catalog),
        )

        _, edges = pipeline.set_queue(self.track_ids[:2])

        self.assertEqual(edges[0].status, "analyzed")
        self.assertEqual(edges[0].candidate_count, 1)
        self.assertEqual(edges[0].reason, "clean_break_required")
        payload = pipeline.cache.payload(self.track_ids[0], target_id)
        self.assertEqual(payload["pairs"], [])
        self.assertEqual(payload["clean_break_pairs"][0]["transition_mode"], "clean_break")

    def test_queue_supports_more_than_ten_tracks(self) -> None:
        analysis_root = self.root / "large-analysis"
        track_ids = write_catalog(analysis_root, count=12)
        catalog = AnalysisCatalog.from_root(analysis_root)
        pipeline = TransitionPipeline(
            VersionedQueue(catalog),
            PairAnalysisCache(self.root / "large-pairs", catalog),
        )

        _, edges = pipeline.set_queue(track_ids)

        self.assertEqual(len(edges), 11)


if __name__ == "__main__":
    unittest.main()

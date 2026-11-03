from __future__ import annotations

import json
from pathlib import Path
from analysis_config import analysis_root
from tempfile import TemporaryDirectory
import time
import unittest

from music_features import MusicFeatures, CACHE_KEY, MODEL
from set_builder import SetBuilderService, parse_curator_response, sha256_file


def metadata(path: Path) -> dict:
    return {
        "title": path.stem,
        "artist": "Fixture Artist",
        "genre": "house",
        "durationSeconds": 180.0,
    }


def write_music_result(root: Path, track_id: str) -> None:
    """Preexisting synthetic Muse cache for tests focused on other stages."""
    MusicFeatures(root, root).save(track_id, {"audioSha256": track_id,
        "cacheKey": CACHE_KEY, "model": MODEL, "status": "ready", "finishReason": "stop",
        "content": "Synthetic music description; genre uncertain."})


def write_analyzed_library(
    root: Path, count: int = 5, bpms: tuple[float, ...] | None = None
) -> tuple[str, ...]:
    library = root / "local-assets/samples"
    fixture_analysis_root = analysis_root(root)
    library.mkdir(parents=True)
    track_ids = []
    for index in range(count):
        source = library / f"house-{index}.wav"
        source.write_bytes(f"fixture-{index}".encode())
        track_id = sha256_file(source)
        track_ids.append(track_id)
        write_music_result(root, track_id)
        output = fixture_analysis_root / track_id / "track-analysis-v1.json"
        output.parent.mkdir(parents=True)
        output.write_text(
            json.dumps(
                {
                    "audio_sha256": track_id,
                    "analysis_cache_key": f"analysis-{index}",
                    "source_filename": source.name,
                    "duration_seconds": 180,
                    "rhythm": {"bpm": bpms[index] if bpms else 124 + index},
                }
            ),
            encoding="utf-8",
        )
    return tuple(track_ids)


class SetBuilderTest(unittest.TestCase):
    def test_curator_response_is_strict_and_bounded(self) -> None:
        ids = {"a", "b", "c"}
        ordered, reason = parse_curator_response(
            '{"ordered_track_ids":["b","a"],"reason":"A smooth arc."}',
            allowed_ids=ids,
            count=2,
        )
        self.assertEqual(ordered, ("b", "a"))
        self.assertEqual(reason, "A smooth arc.")
        with self.assertRaisesRegex(ValueError, "unknown"):
            parse_curator_response(
                '{"ordered_track_ids":["b","x"],"reason":"No."}',
                allowed_ids=ids,
                count=2,
            )

    def test_build_uses_curator_order_and_schedules_only_adjacent_pairs(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            track_ids = write_analyzed_library(root)
            requested_order = tuple(reversed(track_ids))

            def curator(tracks, prompt, count):
                self.assertEqual(prompt, "ten house tracks")
                self.assertEqual(count, 5)
                self.assertEqual(len(tracks), 5)
                return requested_order, "Meta fixture order"

            service = SetBuilderService(
                root,
                metadata_reader=metadata, prepare_transitions=True,
                curator=curator,
                start_worker=False,
            )
            state = service.build("ten house tracks", 5)

            self.assertEqual(state["orderedTrackIds"], list(requested_order))
            self.assertEqual(state["queueRevision"], 1)
            self.assertEqual(len(state["transitions"]), 4)
            self.assertEqual(
                service.queued_jobs,
                {("pair", left, right) for left, right in zip(requested_order, requested_order[1:])},
            )

    def test_reorder_retains_edges_and_schedules_only_new_adjacencies(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            track_ids = write_analyzed_library(root)
            service = SetBuilderService(
                root,
                metadata_reader=metadata, prepare_transitions=True,
                curator=lambda tracks, prompt, count: (track_ids, "fixture"),
                start_worker=False,
            )
            service.build("house", 5)
            for edge in service.state["transitions"]:
                edge["status"] = "analyzed"
                edge["candidateCount"] = 7
            service.queued_jobs.clear()
            while not service.jobs.empty():
                service.jobs.get_nowait()
                service.jobs.task_done()
            reordered = (track_ids[0], track_ids[1], track_ids[3], track_ids[2], track_ids[4])

            state = service.reorder(reordered)

            retained = next(
                edge
                for edge in state["transitions"]
                if (edge["sourceTrackId"], edge["targetTrackId"]) == (track_ids[0], track_ids[1])
            )
            self.assertEqual(retained["status"], "analyzed")
            expected_new = {
                ("pair", track_ids[1], track_ids[3]),
                ("pair", track_ids[3], track_ids[2]),
                ("pair", track_ids[2], track_ids[4]),
            }
            self.assertEqual(service.queued_jobs, expected_new)
            self.assertEqual(state["queueRevision"], 2)

    def test_post_analysis_meta_order_must_reduce_blocked_edges(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            track_ids = write_analyzed_library(
                root,
                bpms=(100, 140, 105, 145, 110),
            )
            improved = (track_ids[0], track_ids[2], track_ids[4], track_ids[1], track_ids[3])
            service = SetBuilderService(
                root,
                metadata_reader=metadata, prepare_transitions=True,
                curator=lambda tracks, prompt, count: (track_ids, "initial"),
                post_curator=lambda tracks, prompt, count: (improved, "measured BPM reorder"),
                start_worker=False,
            )
            service.build("rising house", 5)
            for edge in service.state["transitions"]:
                edge["status"] = "blocked"
            service.state["postAnalysisPlanning"] = "pending"

            service._work_post_plan()

            self.assertEqual(service.state["orderedTrackIds"], list(improved))
            self.assertEqual(service.state["queueRevision"], 2)
            self.assertEqual(service.state["postAnalysisPlanning"], "completed")
            self.assertEqual(service.state["reason"], "measured BPM reorder")
            self.assertIsNone(service.state["error"])

    def test_mock_async_pipeline_reaches_ready_within_five_seconds(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / "local-assets/samples"
            library.mkdir(parents=True)
            for index in range(4):
                (library / f"mock-{index}.wav").write_bytes(f"audio-{index}".encode())

            def fake_analyzer(track) -> None:
                time.sleep(0.02)
                index = int(track.title.rsplit("-", 1)[1])
                bpm = (80.0, 100.0, 130.0, 182.0)[index]
                bar_seconds = 240.0 / bpm
                bars = [
                    {
                        "beat_index": bar * 4,
                        "start_seconds": bar * bar_seconds,
                        "end_seconds": (bar + 1) * bar_seconds,
                        "rms_normalized": 0.55,
                        "low_ratio_normalized": 0.45,
                        "positive_flux_normalized": 0.5,
                    }
                    for bar in range(20)
                ]
                payload = {
                    "audio_sha256": track.id,
                    "analysis_cache_key": f"mock-analysis-{index}",
                    "source_filename": track.path.name,
                    "duration_seconds": 20 * bar_seconds,
                    "rhythm": {"bpm": bpm},
                    "bars": bars,
                    "candidates": {
                        "exit": [{"rank": 1, "beat_index": 16, "time_seconds": 4 * bar_seconds, "score": 0.8}],
                        "entry": [{"rank": 1, "beat_index": 8, "time_seconds": 2 * bar_seconds, "score": 0.75, "boundary_change": 0.4}],
                    },
                }
                output = analysis_root(root) / track.id / "track-analysis-v1.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(payload), encoding="utf-8")
                write_music_result(root, track.id)

            def original_order(tracks, prompt, count):
                return tuple(track.id for track in tracks), "mock Meta order"

            service = SetBuilderService(
                root,
                metadata_reader=metadata, prepare_transitions=True,
                curator=original_order,
                post_curator=original_order,
                analyzer=fake_analyzer,
                start_worker=True,
            )
            service.build("four fast mock tracks", 4)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and service.snapshot()["status"] != "ready":
                time.sleep(0.01)

            state = service.snapshot()
            self.assertEqual(state["status"], "ready")
            self.assertTrue(state["preparationReady"])
            self.assertTrue(state["activationReady"])
            self.assertEqual({track["analysisStatus"] for track in state["queue"]}, {"ready"})
            self.assertEqual(len(state["transitions"]), 3)
            self.assertTrue(all(edge["status"] == "analyzed" for edge in state["transitions"]))
            self.assertTrue(all(edge["candidateCount"] > 0 for edge in state["transitions"]))
            # Shared-tempo candidates now bridge these synthetic BPM gaps.
            self.assertTrue(all(edge["reason"] is None for edge in state["transitions"]))


if __name__ == "__main__":
    unittest.main()

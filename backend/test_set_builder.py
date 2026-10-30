from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from set_builder import SetBuilderService, parse_curator_response, sha256_file


def metadata(path: Path) -> dict:
    return {
        "title": path.stem,
        "artist": "Fixture Artist",
        "genre": "house",
        "durationSeconds": 180.0,
    }


def write_analyzed_library(
    root: Path, count: int = 5, bpms: tuple[float, ...] | None = None
) -> tuple[str, ...]:
    library = root / "local-assets/samples"
    analysis_root = root / "artifacts/time-map"
    library.mkdir(parents=True)
    track_ids = []
    for index in range(count):
        source = library / f"house-{index}.wav"
        source.write_bytes(f"fixture-{index}".encode())
        track_id = sha256_file(source)
        track_ids.append(track_id)
        output = analysis_root / track_id / "track-analysis-v1.json"
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
                metadata_reader=metadata,
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
                metadata_reader=metadata,
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
                metadata_reader=metadata,
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


if __name__ == "__main__":
    unittest.main()

"""Backend switching, cache isolation and consumer contract regressions."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from analysis_config import analysis_root, selected_backend
from track_analyzer import analyze, file_hash, validate_analysis, run_for_product
from set_builder import SetBuilderService
from live_bridge import LiveStateStore
from test_set_builder import metadata, write_analyzed_library


def result_for(source, backend):
    beats = [i * .5 for i in range(81)]
    bars = [{"beat_index": i, "start_seconds": beats[i], "end_seconds": beats[i + 4]}
            for i in range(0, 80, 4)]
    return {"schema_version": "dj-track-analysis-v1", "audio_sha256": file_hash(source),
            "source_filename": source.name, "analysis_cache_key": backend, "duration_seconds": 41,
            "rhythm": {"bpm": 120, "beat_count": len(beats), "beats_seconds": beats,
                       "beat_positions": [(i % 4) + 1 for i in range(len(beats))],
                       "downbeats_seconds": beats[::4]}, "bars": bars,
            "candidates": {role: [{"beat_index": 16, "time_seconds": 8.0}]
                           for role in ("entry", "exit")}, "provenance": {"backend": backend}}


class AnalysisBackendTest(unittest.TestCase):
    def test_default_and_invalid_selection(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(selected_backend(), "essentia_dsp")
        with patch.dict(os.environ, {"DJ_ANALYSIS_BACKEND": "typo"}):
            with self.assertRaises(ValueError):
                selected_backend()

    def test_adapters_share_contract_and_preserve_other_backend_output(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "song.wav"
            source.write_bytes(b"synthetic identity")
            def adapter(name):
                return lambda source, work, limit, workspace: result_for(source, name)
            with patch.dict("track_analyzer.BACKEND_RUNNERS", {
                "all_in_one": adapter("all_in_one"), "essentia_dsp": adapter("essentia_dsp")
            }):
                paths = []
                for backend in ("all_in_one", "essentia_dsp"):
                    dest = analysis_root(root, backend) / file_hash(source) / "track-analysis-v1.json"
                    analyze(source, dest, backend, root)
                    paths.append(dest)
                self.assertNotEqual(*paths)
                for path, backend in zip(paths, ("all_in_one", "essentia_dsp")):
                    payload = json.loads(path.read_text())
                    validate_analysis(payload)
                    self.assertEqual(payload["provenance"]["backend"], backend)
                self.assertEqual(set(json.loads(paths[0].read_text())), set(json.loads(paths[1].read_text())))

    def test_invalid_cue_and_grid_are_rejected(self):
        with TemporaryDirectory() as raw:
            source = Path(raw) / "song.wav"; source.write_bytes(b"fixture")
            payload = result_for(source, "essentia_dsp")
            payload["candidates"]["entry"][0]["time_seconds"] = 8.1
            with self.assertRaisesRegex(ValueError, "cue"):
                validate_analysis(payload)
            payload = result_for(source, "essentia_dsp")
            payload["rhythm"]["beats_seconds"][3] = float("nan")
            with self.assertRaisesRegex(ValueError, "beats"):
                validate_analysis(payload)

    def test_default_runner_never_requests_all_in_one(self):
        with TemporaryDirectory() as raw:
            root = Path(raw); source = root / "song.wav"; source.write_bytes(b"fixture")
            with patch("track_analyzer.subprocess.run") as run:
                result = run_for_product(source, root / "out/track-analysis-v1.json", "essentia_dsp", root)
            self.assertEqual(result["neuralNet"], "not_used")
            command = run.call_args.args[0]
            self.assertIn("essentia_dsp", command)
            self.assertNotIn("all_in_one", command)
            self.assertTrue(command[1].endswith("backend/track_analyzer.py"))

    def test_switch_preserves_old_cache_but_invalidates_preparation(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            with patch.dict(os.environ, {"DJ_ANALYSIS_BACKEND": "all_in_one"}):
                ids = write_analyzed_library(root, count=2)
                original = analysis_root(root) / ids[0] / "track-analysis-v1.json"
                old_bytes = original.read_bytes()
                state = root / "artifacts/live-performance/set-builder-state-v1.json"
                state.parent.mkdir(parents=True)
                state.write_text(json.dumps({"orderedTrackIds": ids, "queueRevision": 4,
                    "analysisBackend": "all_in_one", "activationReady": True,
                    "status": "ready", "transitionsRequested": True}))
            with patch.dict(os.environ, {"DJ_ANALYSIS_BACKEND": "essentia_dsp"}):
                service = SetBuilderService(root, metadata_reader=metadata, start_worker=False)
                self.assertTrue(all(t.analysis_status == "pending" for t in service.catalog.values()))
                self.assertEqual(service.state["orderedTrackIds"], list(ids))
                self.assertEqual(service.state["queueRevision"], 5)
                self.assertFalse(service.state["activationReady"])
                self.assertEqual(service.state["buildPhase"], "library_ready")
                self.assertFalse(service.queued_jobs)
                self.assertEqual(original.read_bytes(), old_bytes)
            with patch.dict(os.environ, {"DJ_ANALYSIS_BACKEND": "all_in_one"}):
                service = SetBuilderService(root, metadata_reader=metadata, start_worker=False)
                self.assertTrue(all(t.analysis_status == "ready" for t in service.catalog.values()))

    def test_bridge_does_not_restore_foreign_backend_plan(self):
        with TemporaryDirectory() as raw, patch.dict(os.environ, {"DJ_ANALYSIS_BACKEND": "essentia_dsp"}):
            root = Path(raw); runtime = root / "artifacts/live-performance"; runtime.mkdir(parents=True)
            (runtime / "queue-state-v1.json").write_text(json.dumps({"schema_version": 1,
                "revision": 8, "ordered_track_ids": ["old-model-track"], "updated_at": "old"}))
            (runtime / "dashboard-meta-selection-v1.json").write_text('{"old": true}')
            store = LiveStateStore(root)
            self.assertEqual(store.queue["ordered_track_ids"], [])
            self.assertEqual(store.queue["revision"], 9)
            self.assertEqual(store.selection, {})
            self.assertIsNone(store.envelope)


if __name__ == "__main__":
    unittest.main()

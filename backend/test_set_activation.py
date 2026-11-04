from __future__ import annotations

import hashlib
import json
from pathlib import Path
from analysis_config import analysis_root
from tempfile import TemporaryDirectory
import unittest

from clean_break import compile_clean_break_plan, select_clean_break_with_meta
from set_activation import (
    load_prepared_activation,
    prepare_activation,
    promote_prepared_activation,
)
from transition_pipeline import AnalysisCatalog


class SetActivationTest(unittest.TestCase):
    def test_clean_break_meta_selection_is_bounded_and_strict(self) -> None:
        candidates = [
            {
                "transition_mode": "clean_break",
                "a_exit": {"beat_index": index * 4},
                "b_entry": {"beat_index": index},
                "heuristic_score": 1 - index / 100,
            }
            for index in range(12)
        ]
        seen = {}

        def transport(system: str, prompt: str) -> str:
            seen["system"] = system
            seen["prompt"] = json.loads(prompt)
            return json.dumps(
                {
                    "selected_candidate_id": "clean-3",
                    "confidence": 0.81,
                    "reason": "The strongest measured phrase handoff.",
                }
            )

        selected, confidence, reason = select_clean_break_with_meta(
            candidates,
            queue_revision=4,
            source_id="a" * 64,
            target_id="b" * 64,
            transport=transport,
        )

        self.assertIs(selected, candidates[2])
        self.assertEqual(confidence, 0.81)
        self.assertIn("phrase", reason)
        self.assertEqual(len(seen["prompt"]["candidates"]), 10)

    def test_prepares_hash_verified_playlist_without_replacing_active_queue(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            library = root / "local-assets/samples-v3"
            fixture_analysis_root = analysis_root(root)
            library.mkdir(parents=True)
            ids = []
            for index in range(3):
                source = library / f"track-{index}.wav"
                source.write_bytes(f"audio-{index}".encode())
                track_id = hashlib.sha256(source.read_bytes()).hexdigest()
                ids.append(track_id)
                output = fixture_analysis_root / track_id / "track-analysis-v1.json"
                output.parent.mkdir(parents=True)
                output.write_text(
                    json.dumps(
                        {
                            "audio_sha256": track_id,
                            "analysis_cache_key": f"key-{index}",
                            "source_filename": source.name,
                        }
                    ),
                    encoding="utf-8",
                )
            builder = {
                "status": "ready",
                "preparationReady": True,
                "queueRevision": 2,
                "orderedTrackIds": ids,
                "trackStatuses": {track_id: "ready" for track_id in ids},
                "transitions": [
                    {"status": "analyzed", "candidateCount": 5},
                    {"status": "analyzed", "candidateCount": 5},
                ],
            }

            prepared = prepare_activation(root, builder, active_queue_revision=7)

            self.assertEqual(prepared.queue["revision"], 8)
            self.assertEqual(
                prepared.playlist_path.read_text(encoding="utf-8").splitlines()[1:],
                [str((library / f"track-{index}.wav").resolve()) for index in range(3)],
            )
            self.assertFalse((root / "artifacts/live-performance/queue-state-v1.json").exists())
            loaded = load_prepared_activation(prepared.activation_path)
            self.assertEqual(loaded["queue"]["ordered_track_ids"], ids)

            promoted = promote_prepared_activation(root, loaded)
            self.assertEqual(promoted["ordered_track_ids"], ids)
            active = json.loads(
                (root / "artifacts/live-performance/queue-state-v1.json").read_text()
            )
            self.assertEqual(active["revision"], 8)
            self.assertEqual(
                json.loads(
                    (root / "artifacts/live-performance/tonight-set-list-v1.json").read_text()
                )["ordered_tracks"][0]["track_id"],
                ids[0],
            )

    def test_compiles_clean_break_without_sync_or_crossfade_fields(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            tracks = {}
            for index, track_id in enumerate(("a" * 64, "b" * 64)):
                output = root / track_id / "track-analysis-v1.json"
                output.parent.mkdir(parents=True)
                output.write_text(
                    json.dumps(
                        {
                            "audio_sha256": track_id,
                            "analysis_cache_key": f"key-{index}",
                            "source_filename": f"track-{index}.wav",
                        }
                    ),
                    encoding="utf-8",
                )
            catalog = AnalysisCatalog.from_root(root)
            plan = compile_clean_break_plan(
                source_id="a" * 64,
                target_id="b" * 64,
                pair={
                    "transition_mode": "clean_break",
                    "a_exit": {"beat_index": 80},
                    "b_entry": {"beat_index": 12},
                },
                catalog=catalog,
                queue_revision=4,
                source_deck=1,
                target_deck=2,
                model="muse-test",
            )

            self.assertEqual(plan["transition_mode"], "clean_break")
            self.assertEqual(plan["mix"]["technique"], "fade_out_cut")
            self.assertEqual(plan["mix"]["fade_out_seconds"], .5)
            self.assertFalse(plan["allow_clean_cut"])
            self.assertEqual(plan["mix"]["tempo_source"], "native_file_bpm_no_sync")
            self.assertNotIn("crossfader_curve", plan["mix"])
            self.assertNotIn("transition_end_beat_index", plan["deck_a"])


if __name__ == "__main__":
    unittest.main()

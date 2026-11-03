import json
from pathlib import Path
import tempfile
import unittest

from live_replanner import build_live_replan_selection, enumerate_future_candidates
from plan_lifecycle import DeckPerformance, PerformanceSnapshot
from queue_service import AnalysisCatalog, QueueSnapshot, TrackRef


def pair(exit_beat: int, entry_beat: int, score: float = 0.8) -> dict:
    return {
        "rank": 1,
        "a_exit": {"beat_index": exit_beat},
        "b_entry": {"beat_index": entry_beat},
        "transition_bar_options": [2],
        "transition_metrics_by_bars": {
            "2": {"strategy_scores": {"maintain_energy": score}}
        },
    }


class LiveReplannerTest(unittest.TestCase):
    def test_rejects_old_exit_and_keeps_only_future_candidate(self) -> None:
        pairing = {"pairs": [pair(120, 40), pair(160, 48)]}

        candidates = enumerate_future_candidates(
            pairing,
            current_source_beat=130,
            source_beat_count=220,
            intent="maintain_energy",
            planning_lead_beats=8,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_exit_beat_index, 160)

    def test_fails_when_no_future_candidate_remains(self) -> None:
        with self.assertRaisesRegex(ValueError, "planning lead"):
            enumerate_future_candidates(
                {"pairs": [pair(120, 40)]},
                current_source_beat=130,
                source_beat_count=220,
                intent="maintain_energy",
                planning_lead_beats=0,
            )

    def test_real_style_late_fallback_builds_reverse_deck_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_id = "b" * 64
            target_id = "c" * 64
            source_path = root / source_id / "track-analysis-v1.json"
            target_path = root / target_id / "track-analysis-v1.json"
            source_path.parent.mkdir(parents=True)
            target_path.parent.mkdir(parents=True)
            bars = [
                {
                    "beat_index": beat,
                    "start_seconds": beat / 2,
                    "end_seconds": beat / 2 + 2,
                    "rms_normalized": 0.7,
                    "low_ratio_normalized": 0.5,
                    "positive_flux_normalized": 0.6,
                }
                for beat in range(0, 200, 4)
            ]

            def candidate(beat: int, role: str, rank: int) -> dict:
                return {
                    "beat_index": beat,
                    "time_seconds": beat / 2,
                    "rank": rank,
                    "role": role,
                    "score": 0.8,
                    "exit_score": 0.7,
                    "context_bars": 4,
                    "boundary_change": 0.4,
                    "entry_type": "blend",
                    "blend_score": 0.8,
                    "drop_score": 0.2,
                    "build_score": 0.3,
                    "selection_source": "test",
                }

            common = {
                "duration_seconds": 100.0,
                "bars": bars,
                "rhythm": {"bpm": 120.0, "beat_count": 200},
            }
            source_payload = {
                **common,
                "audio_sha256": source_id,
                "analysis_cache_key": "2" * 64,
                "source_filename": "source.wav",
                "candidates": {
                    "exit": [candidate(120, "exit", 1)],
                    "entry": [candidate(160, "entry", 1)],
                },
            }
            target_payload = {
                **common,
                "audio_sha256": target_id,
                "analysis_cache_key": "3" * 64,
                "source_filename": "target.wav",
                "candidates": {
                    "exit": [candidate(120, "exit", 1)],
                    "entry": [candidate(40, "entry", 1)],
                },
            }
            source_path.write_text(json.dumps(source_payload), encoding="utf-8")
            target_path.write_text(json.dumps(target_payload), encoding="utf-8")
            catalog = AnalysisCatalog(
                {
                    source_id: TrackRef(
                        source_id, "2" * 64, "source.wav", source_path
                    ),
                    target_id: TrackRef(
                        target_id, "3" * 64, "target.wav", target_path
                    ),
                }
            )
            queue = QueueSnapshot(
                revision=3,
                ordered_track_ids=("a" * 64, source_id, target_id),
                updated_at="2026-10-29T00:00:00+00:00",
            )
            performance = PerformanceSnapshot(
                revision=12,
                decks={
                    1: DeckPerformance(1, "a" * 64, "1" * 64, 100, False),
                    2: DeckPerformance(2, source_id, "2" * 64, 140, True),
                },
            )
            previous = {
                "queue_revision": 3,
                "following_feasible_draft": {
                    "source_track_id": source_id,
                    "target_track_id": target_id,
                    "source_exit_beat_index": 120,
                },
            }

            result = build_live_replan_selection(
                previous, queue, performance, catalog
            )

            self.assertTrue(result["replan_context"]["old_exit_expired"])
            self.assertGreaterEqual(
                result["replan_context"]["planning_latency_ms"], 0
            )
            self.assertEqual(
                result["replan_context"]["selected_candidate"][
                    "source_exit_beat_index"
                ],
                160,
            )
            plan = result["next_executable_plan"]
            self.assertEqual(plan["source_start_mode"], "continue_playing")
            self.assertEqual(plan["completion_mode"], "continue_target")
            self.assertEqual(plan["deck_a"]["deck"], 2)
            self.assertEqual(plan["deck_b"]["deck"], 1)


if __name__ == "__main__":
    unittest.main()

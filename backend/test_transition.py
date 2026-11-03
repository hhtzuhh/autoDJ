import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

import transition


class TransitionPlanTests(unittest.TestCase):
    def test_execution_journal_retains_set_identity_on_every_action(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'transition-events.jsonl'
            log = transition.RunLog(path, set_run_id='set-000012', plan_id='p')
            log.write('run_started')
            log.write('in_track_action_completed', function='bass_pulse')
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r['set_run_id'] for r in rows], ['set-000012'] * 2)
            self.assertEqual([r['plan_id'] for r in rows], ['p'] * 2)

    def test_failed_live_preparation_preserves_playing_source(self) -> None:
        calls = []
        probe_api = SimpleNamespace(
            send_sync_value=lambda *args: None,
            send_value=lambda output, group, control, value: calls.append(
                (group, control, value)
            ),
        )
        preserved = transition.preserve_live_playback_after_failure(
            probe_api, object(), 1, 2, source_stopped=False
        )

        self.assertEqual(preserved, 1)
        self.assertIn(("1", "play", 1.0), calls)
        self.assertIn(("2", "play", 0.0), calls)
        self.assertNotIn(("1", "play", 0.0), calls)

    def test_validate_track_accepts_unified_analysis_contract(self) -> None:
        digest = "a" * 64
        analysis = {
            "audio_sha256": digest,
            "analysis_cache_key": "unified-cache",
            "rhythm": {"beats_seconds": [0.2, 0.7, 1.2]},
        }
        plan_track = {
            "audio_sha256": digest,
            "analysis_cache_key": "unified-cache",
            "cue_beat_index": 2,
        }

        transition.validate_track(plan_track, analysis, ["cue_beat_index"])

    def test_status_validation_requires_native_then_matched_bpms(self) -> None:
        native_a = {"sync_mode": 0, "bpm": 126.0, "file_bpm": 126.0}
        native_b = {"sync_mode": 0, "bpm": 130.0, "file_bpm": 130.0}
        transition.validate_native_status(native_a, native_b)

        prepared_a = {"sync_mode": 2, "bpm": 126.0, "file_bpm": 126.0}
        prepared_b = {"sync_mode": 1, "bpm": 126.0, "file_bpm": 130.0}
        transition.validate_beatmatch_status(prepared_a, prepared_b)

        with self.assertRaises(RuntimeError):
            transition.validate_beatmatch_status(prepared_a, native_b)

    def test_resolve_plan_uses_beat_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifacts = root / "artifacts"
            plan_path = root / "plan.json"
            hashes = ("a" * 64, "b" * 64)
            beats_a = [index * 0.5 for index in range(50)]
            beats_b = [1.0 + index * 0.4 for index in range(50)]
            analyses = (
                {
                    "cache_key": "cache-a",
                    "source": {"sha256": hashes[0]},
                    "audio": {"sample_rate": 44_100},
                    "rhythm": {"beats_seconds": beats_a},
                },
                {
                    "cache_key": "cache-b",
                    "source": {"sha256": hashes[1]},
                    "audio": {"sample_rate": 48_000},
                    "rhythm": {"beats_seconds": beats_b},
                },
            )
            for digest, analysis in zip(hashes, analyses, strict=True):
                directory = artifacts / digest
                directory.mkdir(parents=True)
                (directory / "analysis.json").write_text(json.dumps(analysis))
            plan = {
                "schema_version": 1,
                "plan_id": "test",
                "deck_a": {
                    "audio_sha256": hashes[0],
                    "analysis_cache_key": "cache-a",
                    "deck": 1,
                    "play_start_beat_index": 4,
                    "transition_start_beat_index": 8,
                    "transition_end_beat_index": 40,
                },
                "deck_b": {
                    "audio_sha256": hashes[1],
                    "analysis_cache_key": "cache-b",
                    "deck": 2,
                    "cue_beat_index": 3,
                },
                "mix": {
                    "beats": 32,
                    "bars": 8,
                    "beats_per_bar": 4,
                    "crossfader_curve": "linear",
                    "steps": 5,
                    "tail_beats": 8,
                },
            }
            plan_path.write_text(json.dumps(plan))

            resolved = transition.resolve_plan(plan_path, artifacts)

            self.assertEqual(resolved["deck_a_start_seconds"], 2.0)
            self.assertEqual(resolved["deck_b_cue_seconds"], 2.2)
            self.assertEqual(resolved["lead_seconds"], 2.0)
            self.assertEqual(resolved["fade_seconds"], 16.0)
            self.assertEqual(resolved["tail_seconds"], 4.0)
            self.assertEqual(resolved["sample_rate_a"], 44_100)
            self.assertEqual(resolved["sample_rate_b"], 48_000)
            self.assertEqual(resolved["source_start_mode"], "seek_and_start")

            plan["source_start_mode"] = "continue_playing"
            plan["deck_a"]["deck"] = 2
            plan["deck_b"]["deck"] = 1
            plan_path.write_text(json.dumps(plan))
            reversed_resolved = transition.resolve_plan(plan_path, artifacts)

            self.assertEqual(reversed_resolved["source_deck"], 2)
            self.assertEqual(reversed_resolved["target_deck"], 1)
            self.assertEqual(
                reversed_resolved["source_start_mode"], "continue_playing"
            )

    def test_resolve_clean_break_preserves_native_tempos(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifacts = root / "analysis"
            hashes = ("a" * 64, "b" * 64)
            for index, digest in enumerate(hashes):
                output = artifacts / digest / "track-analysis-v1.json"
                output.parent.mkdir(parents=True)
                output.write_text(
                    json.dumps(
                        {
                            "audio_sha256": digest,
                            "analysis_cache_key": f"key-{index}",
                            "audio": {"sample_rate": 44_100},
                            "rhythm": {
                                "bpm": (80.0, 140.0)[index],
                                "beats_seconds": [beat * (0.75, 3 / 7)[index] for beat in range(100)],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            plan = {
                "schema_version": 1,
                "plan_id": "clean-test",
                "transition_mode": "clean_break",
                "source_start_mode": "continue_playing",
                "completion_mode": "continue_target",
                "deck_a": {
                    "audio_sha256": hashes[0],
                    "analysis_cache_key": "key-0",
                    "analysis_filename": "track-analysis-v1.json",
                    "deck": 1,
                    "play_start_beat_index": 72,
                    "transition_start_beat_index": 80,
                },
                "deck_b": {
                    "audio_sha256": hashes[1],
                    "analysis_cache_key": "key-1",
                    "analysis_filename": "track-analysis-v1.json",
                    "deck": 2,
                    "cue_beat_index": 12,
                },
                "mix": {
                    "handoff": "target_start_then_crossfader_cut_then_source_stop",
                    "tempo_source": "native_file_bpm_no_sync",
                    "tail_beats": 0,
                },
            }
            path = root / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")

            resolved = transition.resolve_plan(path, artifacts)

            self.assertEqual(resolved["transition_mode"], "clean_break")
            self.assertEqual(resolved["fade_seconds"], 0.0)
            self.assertEqual(resolved["target_bpm"], 140.0)

    def test_rejects_same_source_and_target_deck(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifacts = root / "artifacts"
            plan_path = root / "plan.json"
            digest = "a" * 64
            directory = artifacts / digest
            directory.mkdir(parents=True)
            (directory / "analysis.json").write_text(
                json.dumps(
                    {
                        "cache_key": "cache",
                        "source": {"sha256": digest},
                        "audio": {"sample_rate": 44_100},
                        "rhythm": {
                            "beats_seconds": [index * 0.5 for index in range(20)]
                        },
                    }
                )
            )
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "deck_a": {
                            "audio_sha256": digest,
                            "analysis_cache_key": "cache",
                            "deck": 1,
                        },
                        "deck_b": {
                            "audio_sha256": digest,
                            "analysis_cache_key": "cache",
                            "deck": 1,
                        },
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "distinct Mixxx decks"):
                transition.resolve_plan(plan_path, artifacts)

    def test_rejects_unknown_completion_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifacts = root / "artifacts"
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "completion_mode": "leave_everything_ambiguous",
                        "deck_a": {"deck": 1},
                        "deck_b": {"deck": 2},
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "completion mode"):
                transition.resolve_plan(plan_path, artifacts)

    def test_crossfader_direction_follows_source_deck(self) -> None:
        self.assertEqual(transition.crossfader_value(1, 0.0), 0.0)
        self.assertEqual(transition.crossfader_value(1, 1.0), 1.0)
        self.assertEqual(transition.crossfader_value(2, 0.0), 1.0)
        self.assertEqual(transition.crossfader_value(2, 1.0), 0.0)

    def test_continued_deadline_uses_observed_live_position(self) -> None:
        resolved = {"deck_a_transition_start_seconds": 80.0}

        deadline = transition.continued_transition_deadline(
            resolved,
            source_position_seconds=70.0,
            source_observed_monotonic=100.0,
            now=105.0,
        )

        self.assertEqual(deadline, 110.0)
        with self.assertRaisesRegex(RuntimeError, "already passed"):
            transition.continued_transition_deadline(
                resolved,
                source_position_seconds=80.0,
                source_observed_monotonic=100.0,
                now=100.0,
            )

    def test_rejects_bass_swap_outside_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifacts = root / "artifacts"
            plan_path = root / "plan.json"
            hashes = ("a" * 64, "b" * 64)
            for digest, cache_key in zip(hashes, ("cache-a", "cache-b"), strict=True):
                directory = artifacts / digest
                directory.mkdir(parents=True)
                (directory / "analysis.json").write_text(
                    json.dumps(
                        {
                            "cache_key": cache_key,
                            "source": {"sha256": digest},
                            "audio": {"sample_rate": 44_100},
                            "rhythm": {"beats_seconds": [index * 0.5 for index in range(20)]},
                        }
                    )
                )
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "deck_a": {
                            "audio_sha256": hashes[0],
                            "analysis_cache_key": "cache-a",
                            "deck": 1,
                            "play_start_beat_index": 1,
                            "transition_start_beat_index": 2,
                            "transition_end_beat_index": 10,
                        },
                        "deck_b": {
                            "audio_sha256": hashes[1],
                            "analysis_cache_key": "cache-b",
                            "deck": 2,
                            "cue_beat_index": 1,
                        },
                        "mix": {
                            "beats": 8,
                            "bars": 2,
                            "beats_per_bar": 4,
                            "crossfader_curve": "linear",
                            "steps": 5,
                            "tail_beats": 4,
                            "bass_swap": {"start_beat": 4, "end_beat": 9},
                        },
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "bass swap range"):
                transition.resolve_plan(plan_path, artifacts)

    def test_analysis_filename_must_be_local_json_basename(self) -> None:
        with self.assertRaisesRegex(ValueError, "local JSON basename"):
            transition.analysis_path(
                Path("artifacts"),
                {"audio_sha256": "a" * 64, "analysis_filename": "../other.json"},
            )

    def test_takeover_signal_is_plan_specific_and_preserves_playback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "takeover.json"
            path.write_text(
                json.dumps(
                    {"plan_id": "plan-a", "preserve_playback": True}
                ),
                encoding="utf-8",
            )

            self.assertTrue(transition.takeover_requested("plan-a", path))
            self.assertFalse(transition.takeover_requested("plan-b", path))
            path.write_text(
                json.dumps(
                    {"plan_id": "plan-a", "preserve_playback": False}
                ),
                encoding="utf-8",
            )
            self.assertFalse(transition.takeover_requested("plan-a", path))

    def test_wait_until_raises_distinct_takeover_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            takeover = root / "takeover.json"
            cancel = root / "cancel"
            takeover.write_text(
                json.dumps(
                    {"plan_id": "plan-a", "preserve_playback": True}
                ),
                encoding="utf-8",
            )

            with self.assertRaises(transition.TransitionTakenOver):
                transition.wait_until(
                    time.monotonic() + 1,
                    plan_id="plan-a",
                    cancel_path=cancel,
                    takeover_path=takeover,
                )

    def test_safe_seek_rebases_transition_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rebase = root / "rebase.json"
            cancel = root / "cancel"
            takeover = root / "takeover.json"
            observed_at = time.monotonic()
            rebase.write_text(
                json.dumps(
                    {
                        "plan_id": "plan-a",
                        "source_position_seconds": 9.98,
                        "source_observed_monotonic": observed_at,
                    }
                ),
                encoding="utf-8",
            )

            started = time.monotonic()
            deadline = transition.wait_until_rebasable_transition(
                started + 5,
                plan_id="plan-a",
                transition_position_seconds=10.0,
                rebase_path=rebase,
                cancel_path=cancel,
                takeover_path=takeover,
            )

            self.assertLess(time.monotonic() - started, 0.2)
            self.assertAlmostEqual(deadline, observed_at + 0.02, places=3)


if __name__ == "__main__":
    unittest.main()

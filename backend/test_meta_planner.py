from __future__ import annotations

from collections import deque
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from live_bridge import LiveStateStore, find_track_deck
from meta_planner import (
    MetaDecision,
    build_append_candidates,
    build_rolling_candidates,
    build_three_transition_candidates,
    parse_decision,
    prompt_for,
    select_future_fallback,
    select_with_meta,
    validate_decision,
)


def pair(rank: int, exit_beat: int, entry_beat: int, score: float) -> dict:
    metrics = {
        "strategy_scores": {
            "maintain_energy": score,
            "clean_break": score - 0.1,
            "impact_at_handoff": score - 0.05,
        },
        "low_frequency_separation": 0.8,
    }
    return {
        "rank": rank,
        "a_exit": {"beat_index": exit_beat},
        "b_entry": {"beat_index": entry_beat},
        "transition_bar_options": [2, 4],
        "transition_metrics_by_bars": {"2": metrics, "4": metrics},
    }


class MetaPlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.immediate = {"pairs": [pair(1, 80, 40, 0.9)]}
        self.following = {
            "pairs": [
                pair(1, 70, 20, 0.95),
                pair(2, 220, 24, 0.8),
            ]
        }
        self.candidates = build_rolling_candidates(
            self.immediate,
            self.following,
        )

    def decision(self, candidate_id: str | None = None) -> MetaDecision:
        return MetaDecision(
            selected_candidate_id=candidate_id
            or self.candidates[0]["candidate_id"],
            intent="maintain_energy",
            use_bass_swap=False,
            confidence=0.84,
            reason="Best measured continuity across both legs.",
        )

    def test_candidates_are_temporally_feasible_and_bounded(self) -> None:
        self.assertGreater(len(self.candidates), 0)
        self.assertLessEqual(len(self.candidates), 10)
        self.assertTrue(
            all(
                row["b_exit_beat_index"] >= row["minimum_exit_beat_index"]
                for row in self.candidates
            )
        )

    def test_one_plan_contains_three_transitions(self) -> None:
        third = {"pairs": [pair(1, 240, 32, 0.86)]}
        candidates = build_three_transition_candidates(
            (self.immediate, self.following, third),
            minimum_dwell_beats=16,
        )

        self.assertGreater(len(candidates), 0)
        self.assertTrue(all(len(row["transitions"]) == 3 for row in candidates))
        selected = candidates[0]["transitions"]
        self.assertEqual(selected[0]["source_exit_beat_index"], 80)
        self.assertEqual(selected[2]["source_exit_beat_index"], 240)

    def test_default_three_transition_dwell_is_32_beats(self) -> None:
        immediate = {"pairs": [pair(1, 80, 40, 0.9)]}
        following = {"pairs": [pair(1, 80, 24, 0.85)]}
        third = {"pairs": [pair(1, 72, 20, 0.8)]}

        candidates = build_three_transition_candidates(
            (immediate, following, third)
        )

        self.assertGreater(len(candidates), 0)
        self.assertEqual(candidates[0]["transitions"][1]["source_exit_beat_index"], 80)

    def test_comparable_scores_prefer_earlier_target_entry(self) -> None:
        immediate = {
            "pairs": [
                pair(1, 80, 48, 0.86),
                pair(2, 80, 320, 0.90),
            ]
        }
        following = {"pairs": [pair(1, 400, 24, 0.85)]}
        third = {"pairs": [pair(1, 96, 20, 0.8)]}

        candidates = build_three_transition_candidates(
            (immediate, following, third)
        )

        self.assertGreater(len(candidates), 0)
        self.assertEqual(
            candidates[0]["transitions"][0]["target_entry_beat_index"],
            48,
        )

    def test_buffered_edge_cannot_force_the_next_track_backward(self) -> None:
        pairings = {
            ("b", "c"): {"pairs": [pair(1, 288, 72, 0.9)]},
            ("c", "d"): {"pairs": [pair(1, 200, 80, 0.85)]},
            ("d", "e"): {"pairs": [pair(1, 200, 40, 0.8)]},
        }
        store = object.__new__(LiveStateStore)
        store.transition_pipeline = SimpleNamespace(
            cache=SimpleNamespace(
                payload=lambda source, target: pairings[(source, target)]
            )
        )
        queue_ids = ["a", "b", "c", "d", "e"]

        self.assertFalse(
            store._edge_preserves_future_window(
                {
                    "target_entry_beat_index": 348,
                    "transition_bars": 0,
                },
                0,
                queue_ids,
            )
        )
        self.assertTrue(
            store._edge_preserves_future_window(
                {
                    "target_entry_beat_index": 200,
                    "transition_bars": 0,
                },
                0,
                queue_ids,
            )
        )

    def test_append_candidates_only_choose_new_tail_edge(self) -> None:
        pairing = {
            "pairs": [
                pair(1, 100, 20, 0.95),
                pair(2, 220, 24, 0.8),
            ]
        }
        candidates = build_append_candidates(
            pairing,
            prior_target_entry_beat=40,
            prior_transition_bars=4,
            minimum_dwell_beats=128,
        )

        self.assertGreater(len(candidates), 0)
        self.assertTrue(
            all(row["transition"]["source_exit_beat_index"] == 220 for row in candidates)
        )

    def test_fallback_revalidates_runway_against_latest_live_beat(self) -> None:
        candidates = [
            {"candidate_id": "early", "a_exit_beat_index": 120},
            {"candidate_id": "future", "a_exit_beat_index": 180},
        ]
        self.assertEqual(
            select_future_fallback(candidates, live_beat=130)["candidate_id"],
            "future",
        )
        self.assertIsNone(select_future_fallback(candidates, live_beat=170))
        self.assertTrue(
            all(row["b_exit_beat_index"] == 220 for row in self.candidates)
        )

    def test_prompt_contains_only_local_candidates(self) -> None:
        request = json.loads(
            prompt_for(
                self.candidates,
                queue_revision=7,
                intent="maintain_energy",
                track_ids=("a", "b", "c"),
            )
        )
        self.assertEqual(request["queue_revision"], 7)
        self.assertEqual(len(request["candidate_data"]), len(self.candidates))
        self.assertNotIn("audio", request)

    def test_strict_parser_and_local_validation_reject_invention(self) -> None:
        payload = self.decision().to_dict()
        parsed = parse_decision(json.dumps(payload))
        self.assertEqual(parsed, self.decision())
        payload["beat_index"] = 999
        with self.assertRaisesRegex(ValueError, "strict schema"):
            parse_decision(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "unknown"):
            validate_decision(
                self.decision("invented"),
                self.candidates,
                intent="maintain_energy",
            )

    def test_selector_makes_exactly_one_model_call(self) -> None:
        calls = []

        def fake_transport(system: str, user: str) -> str:
            calls.append((system, user))
            return json.dumps(self.decision().to_dict())

        selection = select_with_meta(
            self.candidates,
            queue_revision=7,
            intent="maintain_energy",
            track_ids=("a", "b", "c"),
            model="muse-test",
            transport=fake_transport,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(selection.model, "muse-test")
        self.assertEqual(
            selection.candidate["candidate_id"],
            self.decision().selected_candidate_id,
        )

    def test_validated_candidate_compiles_to_armable_plan_shape(self) -> None:
        store = object.__new__(LiveStateStore)
        store.queue = {"revision": 7}
        store.analysis_catalog = SimpleNamespace(
            tracks={
                "a": SimpleNamespace(analysis_cache_key="a-key"),
                "b": SimpleNamespace(analysis_cache_key="b-key"),
            }
        )

        selection = store._compile_meta_selection(
            ("a", "b", "c"),
            self.candidates[0],
            self.decision().to_dict(),
            "muse-test",
            2,
            1,
        )

        plan = selection["next_executable_plan"]
        self.assertEqual(selection["selection"], "meta_model_api_single_call")
        self.assertEqual(plan["deck_a"]["deck"], 2)
        self.assertEqual(plan["deck_b"]["deck"], 1)
        self.assertEqual(
            plan["deck_a"]["transition_start_beat_index"],
            self.candidates[0]["a_exit_beat_index"],
        )
        self.assertEqual(plan["source"], "meta_model_api_muse_test")
        self.assertEqual(plan["mix"]["tail_beats"], 8)

    def test_observer_deck_number_comes_from_dictionary_key(self) -> None:
        performance = SimpleNamespace(
            decks={
                1: SimpleNamespace(track_id="a", beat_index=12),
                2: SimpleNamespace(track_id="b", beat_index=20),
            }
        )

        match = find_track_deck(performance, "b")

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match[0], 2)
        self.assertEqual(match[1].beat_index, 20)

    def test_following_draft_is_visible_as_ready_but_not_armed(self) -> None:
        store = object.__new__(LiveStateStore)
        store.transition_overrides = {}
        store.selection = {
            "following_feasible_draft": {
                "status": "feasible_not_armed",
                "source_track_id": "b",
                "target_track_id": "c",
                "source_exit_beat_index": 288,
                "target_entry_beat_index": 72,
                "transition_bars": 8,
            }
        }

        store._restore_following_override()

        edge = store.transition_overrides[("b", "c")]
        self.assertEqual(edge["status"], "ready")
        self.assertEqual(edge["sourceExitBeat"], 288)
        self.assertEqual(edge["targetEntryBeat"], 72)

    def test_exact_set_list_must_match_queue_revision_and_order(self) -> None:
        with TemporaryDirectory() as raw:
            path = Path(raw) / "set-list.json"
            path.write_text(
                json.dumps(
                    {
                        "queue_revision": 3,
                        "ordered_tracks": [
                            {"track_id": "a"},
                            {"track_id": "b"},
                            {"track_id": "c"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = object.__new__(LiveStateStore)
            store.set_list_manifest_path = path
            store.queue = {
                "revision": 3,
                "ordered_track_ids": ["a", "b", "c"],
            }

            store._verify_set_list_edge("b", "c")
            with self.assertRaisesRegex(RuntimeError, "edge_mismatch"):
                store._verify_set_list_edge("a", "c")

    def test_sampler_registry_is_exact_bounded_and_rejects_duplicates(self) -> None:
        registry = LiveStateStore._sampler_slot_registry(["a", "b", "c"])
        self.assertEqual(registry, {"a": 1, "b": 2, "c": 3})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            LiveStateStore._sampler_slot_registry(["a", "a"])
        with self.assertRaisesRegex(ValueError, "at most 10"):
            LiveStateStore._sampler_slot_registry(
                [f"track-{index}" for index in range(11)]
            )

    def test_sampler_registry_restores_only_for_exact_promoted_queue(self) -> None:
        with TemporaryDirectory() as raw:
            workspace = Path(raw)
            runtime = workspace / "artifacts/live-performance"
            runtime.mkdir(parents=True)
            queue = {
                "revision": 7,
                "ordered_track_ids": ["a", "b", "c"],
            }
            (runtime / "prepared-activation-v1.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "playlist_ready",
                        "queue": queue,
                    }
                ),
                encoding="utf-8",
            )
            manifest = runtime / "tonight-set-list-v1.json"
            manifest.write_text(
                json.dumps(
                    {
                        "queue_revision": 7,
                        "ordered_tracks": [
                            {"track_id": "a"},
                            {"track_id": "b"},
                            {"track_id": "c"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = object.__new__(LiveStateStore)
            store.workspace = workspace
            store.set_list_manifest_path = manifest
            store.queue = queue

            self.assertEqual(
                store._restore_sampler_slot_registry(),
                {"a": 1, "b": 2, "c": 3},
            )
            store.queue = {**queue, "revision": 8}
            self.assertEqual(store._restore_sampler_slot_registry(), {})

    def test_loaded_following_draft_promotes_without_another_model_call(self) -> None:
        store = object.__new__(LiveStateStore)
        store.queue = {"revision": 3}
        store.analysis_catalog = SimpleNamespace(
            tracks={
                "b": SimpleNamespace(analysis_cache_key="b-key"),
                "c": SimpleNamespace(analysis_cache_key="c-key"),
            }
        )
        store.raw_performance = SimpleNamespace(
            decks={
                1: SimpleNamespace(track_id="c", beat_index=0),
                2: SimpleNamespace(track_id="b", beat_index=250),
            }
        )
        store.selection = {
            "intent": "maintain_energy",
            "model": "muse-test",
            "model_decision": {
                "selected_candidate_id": "abc-1",
                "intent": "maintain_energy",
                "use_bass_swap": False,
                "confidence": 0.8,
                "reason": "Selected both legs.",
            },
            "following_feasible_draft": {
                "status": "feasible_not_armed",
                "source_track_id": "b",
                "target_track_id": "c",
                "source_exit_beat_index": 288,
                "target_entry_beat_index": 72,
                "transition_bars": 8,
            },
        }
        store.transition_overrides = {}
        store.envelope = None
        store.supervisor = None
        written = []
        store._write_dashboard_selection = written.append

        status, payload = store._promote_following_draft("b", "c")

        self.assertEqual(status, 200)
        self.assertFalse(payload["model_called"])
        self.assertEqual(len(written), 1)
        plan = store.selection["next_executable_plan"]
        self.assertEqual(plan["deck_a"]["deck"], 2)
        self.assertEqual(plan["deck_b"]["deck"], 1)
        self.assertEqual(plan["deck_a"]["transition_start_beat_index"], 288)

    def test_single_transition_buffer_shifts_without_replanning_ready_edges(self) -> None:
        def deck(track_id: str, beat: int):
            return SimpleNamespace(track_id=track_id, beat_index=beat)

        store = object.__new__(LiveStateStore)
        store.raw_performance = SimpleNamespace(
            decks={1: deck("b", 20), 2: deck("c", 0)}
        )
        edges = [
            {
                "mode": "beat_sync_crossfade",
                "source_track_id": source,
                "target_track_id": target,
                "source_exit_beat_index": exit_beat,
                "target_entry_beat_index": 24,
                "transition_bars": 4,
                "pair_rank": 1,
                "score": 0.8,
            }
            for source, target, exit_beat in (
                ("a", "b", 160),
                ("b", "c", 220),
                ("c", "d", 240),
            )
        ]
        store.selection = {
            "model": "muse-test",
            "transition_buffer": edges,
            "next_executable_plan": {},
        }
        store.transition_overrides = {}
        store.envelope = SimpleNamespace()
        store.supervisor = SimpleNamespace()
        store._plan_from_buffer_edge = lambda *args, **kwargs: {
            "plan_id": "promoted-b-c"
        }
        store._write_dashboard_selection = lambda selection: None

        status, payload = store._promote_transition_buffer("b", "c", edges)

        self.assertEqual(status, 200)
        self.assertFalse(payload["model_called"])
        self.assertEqual(
            [edge["source_track_id"] for edge in store.selection["transition_buffer"]],
            ["b", "c"],
        )
        self.assertEqual(store.selection["next_executable_plan"]["plan_id"], "promoted-b-c")

    def test_active_edge_schedules_one_joint_future_window(self) -> None:
        store = object.__new__(LiveStateStore)
        store.action_lock = threading.RLock()
        store.future_retry_at = 0.0
        store.future_retry_context = None
        store.future_plan_in_progress = False
        store.future_transition_plan = None
        store.queue = {"revision": 1, "ordered_track_ids": ["a", "b", "c", "d", "e"]}
        store.selection = {
            "next_executable_plan": {
                "plan_id": "active-a-b",
                "deck_a": {"audio_sha256": "a"},
                "deck_b": {"audio_sha256": "b"},
            }
        }

        with patch("live_bridge.threading.Thread") as thread:
            self.assertTrue(store._schedule_future_transition_plan())
            self.assertFalse(store._schedule_future_transition_plan())

        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_joint_future_window_promotes_without_model_wait(self) -> None:
        store = object.__new__(LiveStateStore)
        edges = [
            {
                "mode": "beat_sync_crossfade",
                "source_track_id": source,
                "target_track_id": target,
                "source_exit_beat_index": exit_beat,
                "target_entry_beat_index": 24,
                "transition_bars": 4,
            }
            for source, target, exit_beat in (
                ("b", "c", 220),
                ("c", "d", 240),
                ("d", "e", 260),
            )
        ]
        store.queue = {"revision": 9}
        store.future_transition_plan = {
            "queue_revision": 9,
            "transition_buffer": edges,
            "next_executable_plan": {
                "plan_id": "future-b-c",
                "deck_a": {"deck": 2},
                "deck_b": {"deck": 1},
            },
        }
        store.selection = {}
        store.transition_overrides = {}
        store.envelope = SimpleNamespace()
        store.supervisor = SimpleNamespace()
        store._write_dashboard_selection = lambda selection: None

        result = store._promote_future_transition_plan("b", "c", 2, 1, 20)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[0], 200)
        self.assertFalse(result[1]["model_called"])
        self.assertEqual(store.selection["transition_buffer"], edges)
        self.assertIsNone(store.future_transition_plan)

    def test_auto_arm_mode_is_independent_from_planning_buffer(self) -> None:
        store = object.__new__(LiveStateStore)
        store.action_lock = threading.RLock()
        store.events = deque()
        store.auto_arm_enabled = False
        store.envelope = None
        store.selection = {"next_executable_plan": {"plan_id": "ready-plan", "deck_a": {"audio_sha256": "a"}}}
        store.supervisor = None
        store._executor_running = lambda: False
        arm_calls = []
        store.request_arm = lambda: (
            arm_calls.append(True) or 202,
            {"message": "armed"},
        )

        status, payload = store.request_auto_arm(True)

        self.assertEqual(status, 200)
        self.assertTrue(payload["auto_arm_enabled"])
        self.assertTrue(payload["plan_armed"])
        self.assertEqual(arm_calls, [True])

        status, payload = store.request_auto_arm(False)
        self.assertEqual(status, 200)
        self.assertFalse(payload["auto_arm_enabled"])
        self.assertEqual(arm_calls, [True])

    def test_auto_arm_retries_until_target_transport_is_observed(self) -> None:
        store = object.__new__(LiveStateStore)
        store.auto_arm_enabled = True
        store.auto_chain_cancel = threading.Event()
        responses = iter(
            [
                (409, {"error": "target_transport_unknown"}),
                (409, {"error": "source_deck_not_playing"}),
                (202, {"plan_id": "ready-plan"}),
            ]
        )
        store.request_arm = lambda: next(responses)

        with patch("live_bridge.time.sleep") as sleep:
            status, payload = store._arm_ready_plan_with_retry()

        self.assertEqual(status, 202)
        self.assertEqual(payload["plan_id"], "ready-plan")
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.1)


if __name__ == "__main__":
    unittest.main()

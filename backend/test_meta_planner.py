from __future__ import annotations

import json
from types import SimpleNamespace
import unittest

from live_bridge import LiveStateStore
from meta_planner import (
    MetaDecision,
    build_rolling_candidates,
    parse_decision,
    prompt_for,
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
                pair(2, 100, 24, 0.8),
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
        self.assertTrue(
            all(row["b_exit_beat_index"] == 100 for row in self.candidates)
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


if __name__ == "__main__":
    unittest.main()

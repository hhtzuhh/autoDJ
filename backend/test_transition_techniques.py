"""Technique selection -> compilation -> executor, using synthetic audio grids/MIDI."""
from collections import deque
from contextlib import nullcontext
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from live_bridge import LiveStateStore
from meta_planner import MetaDecision, MetaSelection, build_transition_window_candidates, select_with_meta
from test_meta_planner import pair
from transition_techniques import TECHNIQUES, apply_technique, selected_techniques, validate_selection_policy

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "experiments/time-map"))
import transition


def cut_pair():
    return {"rank": 1, "a_exit": {"beat_index": 80}, "b_entry": {"beat_index": 20},
            "transition_mode": "clean_break", "heuristic_score": .7}


class TransitionTechniquesTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = [letter * 64 for letter in "abcd"]
        self.store = object.__new__(LiveStateStore)
        self.store.queue = {"revision": 2, "ordered_track_ids": self.ids}
        self.store.analysis_catalog = SimpleNamespace(tracks={
            track: SimpleNamespace(analysis_cache_key="key-" + track) for track in self.ids})
        self.store.transition_pipeline = SimpleNamespace(cache=SimpleNamespace(payload=lambda *args: {"clean_break_pairs": [cut_pair()]}))
        for track in self.ids:
            folder = self.root / track
            folder.mkdir()
            (folder / "track-analysis-v1.json").write_text(json.dumps({
                "audio_sha256": track, "analysis_cache_key": "key-" + track,
                "audio": {"sample_rate": 44100},
                "rhythm": {"bpm": 120, "beats_seconds": [n * .5 for n in range(500)]}}))

    def compiled(self, techniques):
        pairings = tuple({"clean_break_pairs": [cut_pair()]} if TECHNIQUES[name]["mode"] == "clean_break"
                         else {"pairs": [pair(1, 80, 20, .8)]} for name in techniques)
        candidate = build_transition_window_candidates(pairings)[0]
        decision = MetaDecision(candidate["candidate_id"], "maintain_energy", False, .8, "synthetic", tuple(techniques))
        return self.store._compile_three_transition_candidate(tuple(self.ids[:len(techniques) + 1]),
                    MetaSelection(decision, candidate, "synthetic", 1), "synthetic", 1, 2)

    def test_model_receives_catalog_evidence_and_chooses_each_edge(self):
        candidates = build_transition_window_candidates(tuple({"pairs": [pair(1, 80, 20, .8)]} for _ in range(3)))
        def transport(system, user):
            request = json.loads(user)
            self.assertEqual(set(request["technique_catalog"]), set(TECHNIQUES))
            self.assertIn("musical_evidence", request["candidate_data"][0]["transitions"][0])
            self.assertFalse(request["track_context"]["a"]["verified"])
            self.assertIn("EVERY edge", system)
            return json.dumps({"selected_candidate_id": candidates[0]["candidate_id"], "intent": "maintain_energy",
                "transition_techniques": ["crossfade", "bass_dip", "bass_swap"], "confidence": .8, "reason": "Measured alternatives."})
        selected = select_with_meta(candidates, queue_revision=2, intent="maintain_energy", track_ids=tuple("abcd"),
                      model="synthetic", transport=transport, track_context={"a": {"verified": False}})
        self.assertEqual(selected.decision.transition_techniques, ("crossfade", "bass_dip", "bass_swap"))

    def test_missing_wrong_count_unknown_or_incompatible_technique_rejected(self):
        candidates = build_transition_window_candidates(({"pairs": [pair(1, 80, 20, .8)]},))
        for choices in ([], ["clean_cut"], ["clean_cut", "clean_cut"], ["late_cut"], ["fade_out_cut"], ["echo_out"], None):
            payload = {"selected_candidate_id": candidates[0]["candidate_id"], "intent": "maintain_energy",
                       "confidence": .8, "reason": "synthetic"}
            payload.update({"use_bass_swap": False} if choices is None else {"transition_techniques": choices})
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                select_with_meta(candidates, queue_revision=2, intent="maintain_energy", track_ids=tuple("ab"),
                                 model="synthetic", transport=lambda *args: json.dumps(payload))

    def test_mode_choices_survive_candidate_cap(self):
        mixed = {"pairs": [pair(1, 80, 20, .99)], "clean_break_pairs": [cut_pair()]}
        candidates = build_transition_window_candidates((mixed, mixed, mixed))
        self.assertEqual(len({tuple(e["mode"] for e in row["transitions"]) for row in candidates}), 1)
        self.assertLessEqual(len(candidates), 10)

    def test_compiler_preserves_all_three_independent_techniques_and_promotes(self):
        compiled = self.compiled(["crossfade", "bass_swap", "bass_dip"])
        self.assertEqual([edge["technique"] for edge in compiled["transition_buffer"]], ["crossfade", "bass_swap", "bass_dip"])
        self.store.selection = compiled
        self.store.raw_performance = SimpleNamespace(decks={
            2: SimpleNamespace(track_id=self.ids[1], beat_index=30),
            1: SimpleNamespace(track_id=self.ids[2], beat_index=0)})
        self.store.envelope = self.store.supervisor = None
        self.store.events = deque()
        self.store.transition_overrides = {}
        self.store.future_plan_error = None
        self.store._write_dashboard_selection = lambda payload: None
        self.store._maintain_ready_buffer = lambda: None
        self.store._planning_activity = lambda: {}
        status, _ = self.store._promote_transition_buffer(self.ids[1], self.ids[2], compiled["transition_buffer"])
        self.assertEqual(status, 200)
        self.assertEqual(self.store.selection["next_executable_plan"]["mix"]["technique"], "bass_swap")
        self.assertIn("bass_swap", self.store.selection["next_executable_plan"]["mix"])
        self.assertEqual(self.store.selection["following_feasible_draft"]["technique"], "bass_dip")

    def test_control_shapes_are_distinct_and_bounded(self):
        for name in ("crossfade", "bass_swap", "bass_dip"):
            mix = self.compiled([name])["next_executable_plan"]["mix"]
            points = transition.mix_progress_points(mix)
            self.assertEqual((points[0], points[-1]), (0, 1))
            self.assertTrue(all(0 <= value <= 1 for p in points for value in transition.mix_controls(mix, p).values()))
            middle = transition.mix_controls(mix, .5)
            if name == "bass_dip":
                self.assertEqual((middle["source_bass"], middle["target_bass"]), (0, 0))
            elif name == "bass_swap":
                self.assertEqual((middle["source_bass"], middle["target_bass"]), (.25, .25))
            else:
                self.assertEqual(middle, {"crossfader": .5})
        invalid = self.compiled(["bass_dip"])["next_executable_plan"]["mix"]
        invalid["bass_swap"] = {"start_beat": 2, "end_beat": 6}
        with self.assertRaises(ValueError): transition.validate_mix_technique(invalid)

    def run_executor(self, name, source_deck=1, interrupt=None, fail_sync=False):
        plan = self.compiled([name])["next_executable_plan"]
        plan["deck_a"]["deck"], plan["deck_b"]["deck"] = source_deck, 3 - source_deck
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan))
        resolved = transition.resolve_plan(path, self.root)
        calls, modes = [], {1: 0, 2: 0}
        def send_sync(output, deck, control, value):
            modes[deck] = (2 if control == "leader" else 1) if value else 0
        fake = SimpleNamespace(
            mido=SimpleNamespace(open_output=lambda port: nullcontext(object())), port_name=lambda: "synthetic",
            precise_seek=lambda *args: calls.append(("seek", *args)),
            send_sync_value=send_sync,
            send_value=lambda output, target, control, value: calls.append((target, control, value)),
            deck_status=lambda deck, **kwargs: {"sync_mode": 0 if fail_sync and modes[deck] else modes[deck], "bpm": 120, "file_bpm": 120},
            TransitionCancelled=type("ProbeCancelled", (Exception,), {}))
        waits = []
        def wait(deadline, **kwargs):
            waits.append(deadline)
            if interrupt and len(waits) == 5:
                calls.append(("interrupted",))
                raise interrupt()
        with patch.dict(sys.modules, {"probe": fake}), patch.object(transition, "CANCEL_FILE", self.root / "cancel"), \
             patch.object(transition, "TAKEOVER_FILE", self.root / "takeover"), \
             patch.object(transition, "wait_until", side_effect=wait), \
             patch.object(transition, "wait_until_rebasable_transition", return_value=100), \
             patch.object(transition, "continued_transition_deadline", return_value=100), \
             patch.object(transition.time, "sleep"), patch("sys.stdout", new_callable=io.StringIO):
            transition.execute(resolved, self.root, False, source_position_seconds=0, source_observed_monotonic=0)
        return calls, waits

    def test_every_technique_executes_controls_in_both_deck_directions(self):
        for name in TECHNIQUES:
            for source in (1, 2):
                with self.subTest(technique=name, source=source):
                    calls, waits = self.run_executor(name, source)
                    target = str(3 - source)
                    self.assertIn((target, "play", 1.0), calls)
                    self.assertIn((str(source), "play", 0.0), calls)
                    self.assertNotIn((target, "play", 0.0), calls)
                    self.assertEqual(calls[-1], ("master", "crossfader", 1.0 if source == 1 else 0.0))

    def test_takeover_during_new_techniques_sends_no_cleanup_controls(self):
        for name in ("crossfade", "bass_swap", "bass_dip"):
            calls, _ = self.run_executor(name, interrupt=transition.TransitionTakenOver)
            self.assertEqual(calls[-1], ("interrupted",))

    def test_cut_only_candidates_are_unavailable(self):
        self.assertEqual(build_transition_window_candidates(({'clean_break_pairs':[cut_pair()]},)), [])
        for name in ('clean_cut', 'fade_out_cut', 'late_cut'):
            with self.assertRaises(ValueError):
                apply_technique({'transition_mode':'clean_break','mix':{}}, name)

    def test_sync_failure_cannot_fall_back_to_a_direct_cut(self):
        with self.assertRaisesRegex(RuntimeError, 'clean-cut fallback is disabled'):
            self.run_executor("bass_dip", fail_sync=True)
        events = [json.loads(line) for path in (self.root / 'runs').glob('*/transition-events.jsonl') for line in path.read_text().splitlines()]
        self.assertIn('runtime_clean_cut_blocked', [e['event'] for e in events])
        self.assertNotIn('runtime_clean_break_completed', [e['event'] for e in events])
        self.assertIn('failed_live_transition_cleanup', [e['event'] for e in events])

    def test_cached_technique_options_cannot_reenable_clean_cut(self):
        self.assertNotIn('clean_cut', TECHNIQUES)
        edge = {'mode':'clean_break', 'technique_options':['clean_cut','fade_out_cut']}
        with self.assertRaises(ValueError):
            selected_techniques(SimpleNamespace(transition_techniques=['clean_cut']), [edge])
        with self.assertRaises(ValueError):
            selected_techniques(SimpleNamespace(), [edge])

    def test_saved_direct_cuts_block_start_and_arm_policy_without_rewriting(self):
        for plan in ({'transition_mode':'clean_break','mix':{}},
                     {'transition_mode':'clean_break','mix':{'technique':'clean_cut'}},
                     {'transition_mode':'clean_break','mix':{'technique':'fade_out_cut','fade_out_seconds':.5}},
                     {'mix':{'technique':'late_cut','crossfader_curve':'late_cut'}},
                     {'performance_actions':[{'function':'cut_to_next','fade_out_seconds':0}]}):
            with self.subTest(plan=plan), self.assertRaisesRegex(ValueError, 'Cut transitions are disabled'):
                validate_selection_policy({'next_executable_plan':plan})
        with self.assertRaises(ValueError):
            validate_selection_policy({'transition_buffer':[{'mode':'clean_break','technique':'clean_cut'}]})
        validate_selection_policy(self.compiled(['bass_swap']))

    def test_start_and_arm_reject_old_direct_cut_before_midi_or_executor(self):
        s = self.store
        s.selection = {'next_executable_plan':{'transition_mode':'clean_break','mix':{'technique':'clean_cut'}}}
        s.action_lock = threading.RLock()
        s._executor_running = lambda: False
        s.activation_in_progress = False
        s.prepared_performance = {'selection':s.selection}
        s._activation_snapshot = Mock(side_effect=AssertionError('must reject before activation'))
        status, result = s.request_start_set()
        self.assertEqual(status, 409)
        self.assertIn('Cut transitions are disabled', result['message'])
        s.prepared_performance = None
        s.envelope = None
        s.raw_performance = SimpleNamespace()
        s._recovery_reason = lambda: None
        status, result = s.request_arm()
        self.assertEqual((status,result['error']), (409,'cut_transitions_disabled'))


if __name__ == "__main__": unittest.main()

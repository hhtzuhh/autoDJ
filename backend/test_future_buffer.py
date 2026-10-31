"""Synthetic rolling-buffer regressions: no Meta network or Mixxx writes."""
from collections import deque
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch

from live_bridge import LiveStateStore
from meta_planner import MetaConfig, MetaDecision, MetaSelection
from test_meta_planner import pair


class FutureBufferTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        store = self.store = object.__new__(LiveStateStore)
        store.workspace = Path(self.temp.name)
        (store.workspace / 'artifacts/live-performance').mkdir(parents=True)
        store.action_lock = threading.RLock()
        store.events = deque()
        store.meta_calls = deque(maxlen=8)
        store.meta_calls_lock = threading.Lock()
        store.meta_call_attempts = {}
        store.future_retry_context = None
        store.envelope = None
        store.supervisor = None
        store.continuous_set_active = True
        store.future_plan_in_progress = False
        store.future_transition_plan = None
        store.future_plan_error = None
        store.future_retry_at = 0.0
        store.transition_overrides = {}
        store.queue = {'revision': 1, 'ordered_track_ids': list('abcdefghij')}
        store.selection = {'next_executable_plan': {
            'plan_id': 'active-a-b',
            'deck_a': {'audio_sha256': 'a', 'deck': 1},
            'deck_b': {'audio_sha256': 'b', 'deck': 2, 'cue_beat_index': 24},
            'mix': {'bars': 4},
        }}
        store.analysis_catalog = SimpleNamespace(tracks={
            key: SimpleNamespace(analysis_cache_key=key + '-key')
            for key in store.queue['ordered_track_ids']
        })
        store.transition_pipeline = SimpleNamespace(cache=SimpleNamespace(
            payload=lambda *args: {'pairs': [pair(1, 220, 24, 0.9)]}
        ))
        store._write_dashboard_selection = lambda selection: None
        self.config = patch('live_bridge.MetaConfig.from_environment', return_value=MetaConfig('synthetic'))
        self.config.start()
        self.addCleanup(self.config.stop)
        dotenv = patch('live_bridge.load_dotenv')
        dotenv.start()
        self.addCleanup(dotenv.stop)

    @staticmethod
    def choose(candidates, **kwargs):
        return MetaSelection(
            MetaDecision(candidates[0]['candidate_id'], 'maintain_energy', False, .8, 'synthetic'),
            candidates[0], 'synthetic', len(candidates),
        )

    def run_worker(self, callback=None):
        store = self.store
        context = store._future_context()
        store.future_plan_in_progress = True
        with patch('live_bridge.select_with_meta', side_effect=callback or self.choose):
            store._future_transition_plan_worker(*context)

    def test_repeated_promotions_refill_three_edges_then_short_queue_tail(self):
        store = self.store
        with patch('live_bridge.threading.Thread') as thread:
            for index in range(1, 9):
                active_before = deepcopy(store.selection['next_executable_plan'])
                self.run_worker()
                self.assertEqual(store.selection['next_executable_plan'], active_before)
                future = store.future_transition_plan
                expected = min(3, 9 - index)
                self.assertEqual(len(future['transition_buffer']), expected)
                self.assertIsNone(store.future_plan_error)
                for edge in future['transition_buffer']:
                    override = store.transition_overrides[(edge['source_track_id'], edge['target_track_id'])]
                    self.assertEqual((override['status'], override['selectedBy']), ('ready', 'meta'))
                plan = future['next_executable_plan']
                result = store._promote_future_transition_plan(
                    chr(97 + index), chr(98 + index),
                    plan['deck_a']['deck'], plan['deck_b']['deck'], 20,
                )
                self.assertEqual(result[0], 200)
                self.assertFalse(result[1]['model_called'])
            self.assertEqual(thread.call_count, 7)  # Every promotion except the final edge refills.

    def test_timeout_preserves_ready_buffer_and_rate_limits_retry(self):
        store = self.store
        self.run_worker()
        preserved = deepcopy(store.future_transition_plan)
        overrides = deepcopy(store.transition_overrides)
        store.selection['next_executable_plan']['plan_id'] += '-armed'
        def timeout(*args, **kwargs):
            raise TimeoutError('synthetic timeout')
        self.run_worker(timeout)
        self.assertEqual(store.future_transition_plan, preserved)
        self.assertEqual(store.transition_overrides, overrides)
        self.assertIn('TimeoutError', store.future_plan_error)
        with patch('live_bridge.threading.Thread') as thread:
            self.assertFalse(store._maintain_ready_buffer())
            store.future_retry_at = 0.0
            self.assertTrue(store._maintain_ready_buffer())
            self.assertFalse(store._maintain_ready_buffer())
            thread.assert_called_once()

    def test_active_or_queue_change_rejects_stale_result_and_schedules_successor(self):
        for change in ('active', 'queue'):
            with self.subTest(change=change):
                store = self.store
                store.future_transition_plan = None
                def select(candidates, **kwargs):
                    if change == 'active':
                        store.selection['next_executable_plan']['plan_id'] += '-new'
                    else:
                        store.queue['revision'] += 1
                    return self.choose(candidates, **kwargs)
                with patch('live_bridge.threading.Thread') as thread:
                    self.run_worker(select)
                    self.assertIsNone(store.future_transition_plan)
                    thread.assert_called_once()
                    self.assertEqual(thread.call_args.kwargs['args'], store._future_context())
                    self.assertTrue(store.future_plan_in_progress)

    def test_plan_changes_before_worker_starts_still_schedule_successor(self):
        store = self.store
        old = store._future_context()
        store.selection['next_executable_plan']['plan_id'] += '-new'
        with patch('live_bridge.threading.Thread') as thread, patch('live_bridge.select_with_meta') as select:
            store._future_transition_plan_worker(*old)
            select.assert_not_called()
            thread.assert_called_once()

    def test_takeover_during_inference_cannot_publish_or_restart_planning(self):
        store = self.store
        def takeover(candidates, **kwargs):
            store.envelope = {'status': 'cancelled'}
            return self.choose(candidates, **kwargs)
        with patch('live_bridge.threading.Thread') as thread:
            self.run_worker(takeover)
            self.assertIsNone(store.future_transition_plan)
            self.assertFalse(store.future_plan_in_progress)
            thread.assert_not_called()

    def test_running_thread_hands_off_after_plan_change_without_observer_tick(self):
        store = self.store
        started = threading.Event()
        release = threading.Event()
        threads = []
        original_thread = threading.Thread

        def launch(**kwargs):
            thread = original_thread(**kwargs)
            threads.append(thread)
            return thread

        calls = []
        def select(candidates, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                started.set()
                if not release.wait(2):
                    raise TimeoutError("test did not release inference")
            return self.choose(candidates, **kwargs)

        with patch('live_bridge.threading.Thread', side_effect=launch), patch(
            'live_bridge.select_with_meta', side_effect=select,
        ):
            try:
                self.assertTrue(store._schedule_future_transition_plan())
                self.assertTrue(started.wait(2))
                with store.action_lock:
                    store.selection['next_executable_plan']['plan_id'] += '-armed'
                    self.assertFalse(store._schedule_future_transition_plan())
            finally:
                release.set()
                # The first worker starts its successor before exiting.
                for thread in threads:
                    thread.join(2)
            self.assertEqual(len(threads), 2)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(calls), 2)
            self.assertFalse(store.future_plan_in_progress)
            self.assertEqual(store.future_transition_plan['based_on_active_plan_id'],
                             store.selection['next_executable_plan']['plan_id'])

    def test_live_constraints_are_applied_before_top_ten_cap(self):
        from meta_planner import build_transition_window_candidates
        store = self.store
        first = {'pairs': [pair(1, 60, 24, .99), pair(2, 200, 24, .1)]}
        following = {'pairs': [pair(i, 200 + i * 4, 24, .9) for i in range(1, 5)]}
        pairings = (first, following, following)
        top = build_transition_window_candidates(pairings)
        self.assertTrue(all(row['transitions'][0]['source_exit_beat_index'] == 60 for row in top))
        rows = store._constrained_window_candidates(pairings, 150, 7, list('abcdefghij'))
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), 10)
        self.assertTrue(all(row['transitions'][0]['source_exit_beat_index'] == 200 for row in rows))
        store.transition_pipeline.cache.payload = lambda a, b: first if a == 'b' else following
        self.assertTrue(store._edge_preserves_future_window(
            {'target_entry_beat_index': 100, 'transition_bars': 4}, 0, list('abcdefghij'),
        ))

    def test_observer_refill_does_not_wait_on_foreground_inference_lock(self):
        store = self.store
        entered = threading.Event()
        release = threading.Event()
        def hold_lock():
            with store.action_lock:
                entered.set()
                release.wait(2)
        thread = threading.Thread(target=hold_lock)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(store._maintain_ready_buffer())
            self.assertTrue(thread.is_alive())
        finally:
            release.set()
            thread.join(2)

    def test_auto_promotion_defers_call_until_arm_commits_execution_id(self):
        store = self.store
        store.auto_arm_enabled = True
        store.supervisor = None
        with patch('live_bridge.threading.Thread') as thread:
            self.assertFalse(store._maintain_ready_buffer())
            thread.assert_not_called()
            # request_arm sets both the final ID and supervisor before scheduling.
            store.selection['next_executable_plan']['plan_id'] += '-armed'
            store.supervisor = object()
            self.assertTrue(store._schedule_future_transition_plan())
            thread.assert_called_once()
            self.assertEqual(thread.call_args.kwargs['args'], store._future_context())

    def test_meta_activity_exposes_pending_success_timeout_and_discard(self):
        store = self.store
        def observe(candidates, **kwargs):
            calls = store._planning_activity()['metaCalls']
            self.assertEqual(calls[0]['status'], 'calling')
            self.assertEqual(calls[0]['trackIds'], list('bcde'))
            self.assertEqual(calls[0]['timeoutSeconds'], 30)
            return self.choose(candidates, **kwargs)
        self.run_worker(observe)
        self.assertEqual(store._planning_activity()['metaCalls'][0]['status'], 'succeeded')
        store.selection['next_executable_plan']['plan_id'] += '-new'
        def timeout(*args, **kwargs):
            raise TimeoutError('synthetic deadline')
        self.run_worker(timeout)
        activity = store._planning_activity()
        self.assertEqual(activity['metaCalls'][0]['status'], 'timeout')
        self.assertEqual(activity['metaCalls'][0]['attempt'], 2)
        self.assertEqual(activity['futurePlanStatus'], 'degraded')
        self.assertGreater(activity['futureRetryInSeconds'], 0)
        def stale(candidates, **kwargs):
            store.queue['revision'] += 1
            return self.choose(candidates, **kwargs)
        with patch('live_bridge.threading.Thread'):
            self.run_worker(stale)
        self.assertEqual(store._planning_activity()['metaCalls'][0]['status'], 'discarded')

    def test_snapshot_call_activity_is_fresh_while_observer_state_is_old(self):
        store = self.store
        store.lock = threading.Lock()
        store.state = {'plan': {'metaCalls': []}}
        store.error = None
        store.set_builder = SimpleNamespace(snapshot=lambda: {})
        store._activation_snapshot = lambda: None
        request = store._begin_meta_call(MetaConfig('synthetic'), tuple('abcd'), 'current_plan')
        snapshot, _ = store.snapshot()
        self.assertEqual(snapshot['plan']['metaCalls'][0]['id'], request)
        self.assertEqual(snapshot['plan']['metaCalls'][0]['status'], 'calling')
        store._finish_meta_call(request, ValueError('invalid selection'))
        snapshot, _ = store.snapshot()
        self.assertEqual(snapshot['plan']['metaCalls'][0]['status'], 'failed')
        self.assertIn('invalid selection', snapshot['plan']['metaCalls'][0]['detail'])

    def test_candidate_selection_reserves_timeout_and_retry_when_possible(self):
        store = self.store
        store.catalog = {key: {'rhythm': {'beats_seconds': [i * .5 for i in range(500)]}} for key in 'abcd'}
        pairings = (
            {'pairs': [pair(1, 200, 24, .9)]},
            {'pairs': [pair(1, 100, 24, .99), pair(2, 300, 24, .1)]},
            {'pairs': [pair(1, 350, 24, .9)]},
        )
        with patch.dict('os.environ', {'META_TIMEOUT_SECONDS': '30'}):
            rows = store._constrained_window_candidates(pairings, 0, 0, list('abcd'))
        self.assertTrue(rows)
        self.assertTrue(all(row['transitions'][1]['source_exit_beat_index'] == 300 for row in rows))
        self.assertTrue(all(row['refill_timing']['status'] == 'sufficient' for row in rows))
        self.assertEqual(rows[0]['refill_timing']['budgetSeconds'], 68)
        # Missing measurements must never masquerade as enough inference runway.
        store.catalog.pop('b')
        timing = store._window_refill_timing(rows[0]['transitions'], list('abcd'))
        self.assertEqual(timing['status'], 'unknown')

    def test_timing_alternative_survives_musical_score_cap(self):
        store = self.store
        store.catalog = {key: {'rhythm': {'beats_seconds': [i * .5 for i in range(600)]}} for key in 'abcd'}
        # Eight higher-scoring short windows previously hid the late exit.
        options = [pair(i + 1, 100 + i * 4, 24, .99 - i * .01) for i in range(8)]
        options.append(pair(9, 400, 24, .1))
        pairings = ({'pairs': [pair(1, 200, 24, .9)]}, {'pairs': options},
                    {'pairs': [pair(1, 400, 24, .9)]})
        with patch.dict('os.environ', {'META_TIMEOUT_SECONDS': '30'}):
            rows = store._constrained_window_candidates(pairings, 0, 0, list('abcd'))
        self.assertTrue(rows)
        self.assertTrue(all(row['refill_timing']['status'] == 'sufficient' for row in rows))
        self.assertTrue(all(row['transitions'][1]['source_exit_beat_index'] == 400 for row in rows))

    def test_origin_and_timing_survive_promotion_and_failed_refill(self):
        store = self.store
        with patch('live_bridge.threading.Thread'):
            self.run_worker()
            future = store.future_transition_plan
            origin = deepcopy(future['meta_origin'])
            plan = future['next_executable_plan']
            store._promote_future_transition_plan('b', 'c', plan['deck_a']['deck'], plan['deck_b']['deck'], 20)
            self.assertEqual(store.selection['meta_origin'], origin)
            self.assertEqual(store.transition_overrides[('c', 'd')]['metaOrigin'], origin)
            self.run_worker(lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError('slow')))
            self.assertEqual(store.selection['meta_origin'], origin)
            self.assertEqual(store.transition_overrides[('c', 'd')]['metaOrigin'], origin)
            details = store._active_edge_details({
                'sourceExitBeat': plan['deck_a']['transition_start_beat_index'],
                'targetEntryBeat': plan['deck_b']['cue_beat_index'],
                'bars': plan['mix']['bars'], 'metaOrigin': store.selection['meta_origin'],
            })
            self.assertEqual(details['sourceExitBeat'], 220)
            self.assertEqual(details['targetEntryBeat'], 24)
            self.assertEqual(details['metaOrigin']['requestId'], origin['requestId'])

    def recovery_store(self, playing=True):
        store = self.store
        store.current_track_id = 'c'
        store.raw_performance = SimpleNamespace(revision=12, decks={
            1: SimpleNamespace(track_id='c', play=playing, beat_index=150, position_seconds=75),
            2: SimpleNamespace(track_id='b', play=False, beat_index=260, position_seconds=130),
        })
        store.envelope = {'plan_id': 'active-a-b', 'status': 'cancelled', 'invalidation_reason': 'human_sync_change'}
        store.auto_arm_enabled = False
        store.auto_chain_thread = None
        store.auto_chain_cancel = threading.Event()
        store._executor_running = lambda: False
        store._verify_set_list_edge = lambda *args: None
        return store

    def test_recovery_uses_playing_deck_and_never_restarts_stopped_source(self):
        for playing in (True, False):
            store = self.recovery_store(playing)
            before = deepcopy(store.raw_performance)
            prepared = []
            store._prepare_recovery_target = lambda deck, track: prepared.append((deck, track))
            def replan():
                self.assertEqual(store.current_track_id, 'c')
                self.assertEqual(store.selection['transition_buffer'], [])
                self.assertEqual(store.selection['following_feasible_draft'], None)
                return 200, {'status': 'ready'}
            store.request_meta_plan = replan
            with patch.object(store, 'request_arm', return_value=(202, {})) as arm:
                status, result = store.request_recover()
            self.assertEqual(prepared, [(1, 'd')])
            self.assertEqual(status, 202 if playing else 200)
            self.assertEqual(arm.call_count, 1 if playing else 0)
            self.assertEqual(store.raw_performance, before)
            self.assertFalse(store.auto_arm_enabled)

    def test_recovery_rejects_two_playing_decks_without_loading(self):
        store = self.recovery_store()
        store.raw_performance.decks[2].play = True
        with patch.object(store, '_prepare_recovery_target') as prepare:
            status, result = store.request_recover()
        self.assertEqual(status, 409)
        self.assertEqual(result['error'], 'two_decks_playing_choose_source_first')
        prepare.assert_not_called()

    def test_recovery_does_not_arm_after_source_stops_during_load(self):
        store = self.recovery_store()
        store._prepare_recovery_target = lambda *args: setattr(store.raw_performance.decks[1], 'play', False)
        with patch.object(store, 'request_arm') as arm:
            status, result = store.request_recover()
        self.assertEqual(status, 409)
        self.assertEqual(result['error'], 'live_source_changed_during_recovery')
        arm.assert_not_called()

    def test_cancelled_plan_requires_recovery_before_arm_or_auto_mode(self):
        store = self.recovery_store()
        for action in (store.request_arm, lambda: store.request_auto_arm(True)):
            status, payload = action()
            self.assertEqual(status, 409)
            self.assertEqual(payload['error'], 'recover_from_live_position_required')
        self.assertFalse(store.auto_arm_enabled)
        store.activation_in_progress = False
        store.auto_arm_enabled = True
        with patch('live_bridge.threading.Thread') as thread:
            store._maybe_start_continuity_guard(store.raw_performance)
        thread.assert_not_called()

    def test_forward_replan_supports_final_single_transition(self):
        store = self.recovery_store()
        store.current_track_id = 'i'
        store.raw_performance.decks[1].track_id = 'i'
        store.raw_performance.decks[2].track_id = 'j'
        store._seed_standby_overrides = lambda: None
        with patch('live_bridge.threading.Thread'), patch('live_bridge.select_with_meta', side_effect=self.choose):
            status, result = store.request_meta_plan()
        self.assertEqual(status, 200)
        self.assertEqual(result['transition_count'], 1)
        plan = store.selection['next_executable_plan']
        self.assertEqual(plan['deck_a']['audio_sha256'], 'i')
        self.assertGreater(plan['deck_a']['transition_start_beat_index'], 150 + 16)
        self.assertEqual(plan['deck_b']['audio_sha256'], 'j')

    def test_stale_worker_handoff_does_not_start_before_auto_arm(self):
        store = self.store
        store.auto_arm_enabled = True
        store.supervisor = object()
        def promote_before_response(candidates, **kwargs):
            store.selection['next_executable_plan']['plan_id'] += '-promoted'
            store.supervisor = None
            return self.choose(candidates, **kwargs)
        with patch('live_bridge.threading.Thread') as thread:
            self.run_worker(promote_before_response)
            thread.assert_not_called()
            self.assertFalse(store.future_plan_in_progress)
            store.supervisor = object()
            store.selection['next_executable_plan']['plan_id'] += '-armed'
            self.assertTrue(store._schedule_future_transition_plan())
            thread.assert_called_once()

    def test_no_local_candidates_is_visible_without_claiming_api_call(self):
        store = self.store
        store.transition_pipeline.cache.payload = lambda *args: {'pairs': []}
        with patch('live_bridge.select_with_meta') as select:
            store.future_plan_in_progress = True
            store._future_transition_plan_worker(*store._future_context())
            select.assert_not_called()
        activity = store._planning_activity()
        self.assertEqual(activity['metaCalls'], [])
        self.assertIn('no_feasible_future', activity['futurePlanError'])
        self.assertEqual(activity['futurePlanStatus'], 'degraded')

    def test_arm_execution_id_change_keeps_same_planning_decision(self):
        store = self.store
        before = store._future_context()
        def arm(candidates, **kwargs):
            plan = store.selection['next_executable_plan']
            store.selection['planning_plan_id'] = plan['plan_id']
            plan['plan_id'] += '-dashboard-execution'
            self.assertEqual(store._future_context(), before)
            return self.choose(candidates, **kwargs)
        with patch('live_bridge.threading.Thread') as thread:
            self.run_worker(arm)
            thread.assert_not_called()
        self.assertEqual(store._planning_activity()['metaCalls'][0]['status'], 'succeeded')
        self.assertIsNotNone(store.future_transition_plan)
        self.assertFalse(store._schedule_future_transition_plan())

    def test_replanning_same_cues_still_rejects_prior_decision_response(self):
        store = self.store
        def replan(candidates, **kwargs):
            store._set_selection(deepcopy(store.selection))
            return self.choose(candidates, **kwargs)
        with patch('live_bridge.threading.Thread'):
            self.run_worker(replan)
        self.assertIsNone(store.future_transition_plan)
        self.assertEqual(store._planning_activity()['metaCalls'][0]['status'], 'discarded')

    def test_restart_invalidates_unowned_execution_without_transport_commands(self):
        store = self.store
        store.envelope = {'plan_id': 'active-a-b', 'status': 'executing'}
        store._invalidate_restored_envelope()
        self.assertEqual(store.envelope['status'], 'invalidated')
        self.assertEqual(store.envelope['invalidation_reason'], 'bridge_restart_needs_human')
        self.assertFalse(store._maintain_ready_buffer())
        self.assertEqual(store.request_arm()[1]['error'], 'bridge_restart_needs_human')
        self.assertIsNone(store.supervisor)


if __name__ == '__main__':
    unittest.main()

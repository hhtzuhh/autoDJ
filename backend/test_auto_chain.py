"""Delayed Mixxx feedback at executor exit; synthetic clock/MIDI only."""
from collections import deque
from contextlib import nullcontext
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from live_bridge import LiveStateStore


class FakeCancel:
    def __init__(self):
        self.flag = False
        self.now = 0.0
        self.on_wait = lambda: None

    def is_set(self): return self.flag
    def set(self): self.flag = True
    def wait(self, seconds):
        self.now += seconds
        self.on_wait()
        return self.flag


def state(outgoing=True, incoming=False, revision=27):
    return SimpleNamespace(revision=revision, decks={
        1: SimpleNamespace(track_id='a', play=outgoing, beat_index=168),
        2: SimpleNamespace(track_id='b', play=incoming, beat_index=96)})


class AutoChainTest(unittest.TestCase):
    def setUp(self):
        s = self.store = object.__new__(LiveStateStore)
        s.action_lock = threading.RLock()
        s.lock = threading.Lock()
        s.events = deque()
        s.workspace = Path('/synthetic')
        s.auto_chain_cancel = FakeCancel()
        s.auto_chain_states = {}
        s.auto_chain_thread = None
        s.auto_arm_enabled = True
        s.raw_performance = state()
        s.queue = {'revision': 1, 'ordered_track_ids': list('abcd')}
        s.selection = {'next_executable_plan': {'plan_id': 'a-b',
            'deck_a': {'deck': 1, 'audio_sha256': 'a'}, 'deck_b': {'deck': 2, 'audio_sha256': 'b'}}}
        s.supervisor = SimpleNamespace(executor=None,
            manager=SimpleNamespace(envelope=SimpleNamespace(plan_id='a-b', status='completed')))
        s.sampler_slots = {'c': 3, 'd': 4}
        s._crowd_commit = Mock(return_value='d')
        s._verify_set_list_edge = Mock()
        s._wait_for_loaded_track_after_revision = Mock(return_value=True)
        s.request_meta_plan = Mock(return_value=(200, {'accepted': True}))
        s._arm_ready_plan_with_retry = Mock(return_value=(202, {'plan_id': 'b-d'}))
        s._crowd_execution_status = Mock()
        s._crowd_rotation_failed = Mock()
        self.send = Mock()
        self.patches = [patch('live_bridge.time.monotonic', side_effect=lambda: s.auto_chain_cancel.now),
            patch.dict('sys.modules', {'mido': SimpleNamespace(open_output=lambda _: nullcontext('synthetic-midi')),
                'probe': SimpleNamespace(port_name=lambda: 'synthetic', send_load_sampler_value=self.send)})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def run_worker(self):
        self.store._auto_chain_worker('b', 'c', 1, completed_plan_id='a-b', queue_revision=1, outgoing_id='a')

    def test_delayed_start_then_stop_applies_crowd_window_once_after_confirmation(self):
        s = self.store
        samples = iter([state(True, True, 28), state(False, True, 30)])
        def tick():
            self.send.assert_not_called()
            s._crowd_commit.assert_not_called()
            s.raw_performance = next(samples)
        s.auto_chain_cancel.on_wait = tick
        self.run_worker()
        s._crowd_commit.assert_called_once_with('b', 'c', 1)
        self.send.assert_called_once_with('synthetic-midi', 1, 4)
        s.request_meta_plan.assert_called_once()
        s._arm_ready_plan_with_retry.assert_called_once()
        self.assertEqual(s.auto_chain_states['a-b'], 'completed')
        s._crowd_rotation_failed.assert_not_called()

    def test_confirmed_state_needs_no_sleep_and_unknown_stop_is_not_confirmation(self):
        s = self.store
        s.raw_performance = state(None, True)
        s.auto_chain_cancel.on_wait = lambda: setattr(s, 'raw_performance', state(False, True, 30))
        self.run_worker()
        self.assertEqual(s.auto_chain_cancel.now, .05)
        self.assertEqual(s.auto_chain_states['a-b'], 'completed')

    def test_timeout_never_commits_or_loads_and_has_specific_reason(self):
        s = self.store
        self.run_worker()
        s._crowd_commit.assert_not_called()
        self.send.assert_not_called()
        self.assertEqual(s.auto_chain_states['a-b'], 'failed')
        self.assertIn('rotation_readback_timeout:incoming_play_not_confirmed', s._crowd_rotation_failed.call_args.args[0])
        self.assertAlmostEqual(s.auto_chain_cancel.now, 3.0)

    def test_takeover_during_wait_never_commits_loads_or_rearms(self):
        s = self.store
        s.auto_chain_cancel.on_wait = s.auto_chain_cancel.set
        self.run_worker()
        self.assertEqual(s.auto_chain_states['a-b'], 'cancelled')
        s._crowd_commit.assert_not_called()
        self.send.assert_not_called()
        s._arm_ready_plan_with_retry.assert_not_called()
        with patch('live_bridge.threading.Thread') as thread:
            s._maybe_start_auto_chain()
            thread.assert_not_called()
        self.assertTrue(s.auto_chain_cancel.is_set())

    def test_identity_or_queue_change_during_wait_cannot_load(self):
        for change in ('source', 'outgoing', 'queue', 'plan'):
            with self.subTest(change=change):
                self.setUp()
                s = self.store
                def tick():
                    if change == 'source': s.raw_performance.decks[2].track_id = 'x'
                    elif change == 'outgoing': s.raw_performance.decks[1].track_id = 'x'
                    elif change == 'queue': s.queue['revision'] += 1
                    else: s.supervisor.manager.envelope.plan_id = 'replacement'
                s.auto_chain_cancel.on_wait = tick
                self.run_worker()
                self.send.assert_not_called()
                s._crowd_commit.assert_not_called()
                self.assertEqual(s.auto_chain_states['a-b'], 'failed')

    def test_observer_ticks_reserve_one_pending_attempt_without_calling_it_completed(self):
        s = self.store
        with patch('live_bridge.threading.Thread') as thread:
            s._maybe_start_auto_chain()
            s._maybe_start_auto_chain()
            thread.assert_called_once()
            self.assertEqual(s.auto_chain_states['a-b'], 'waiting_for_readback')
            self.assertEqual(thread.call_args.kwargs['kwargs']['outgoing_id'], 'a')

    def test_immediate_confirmation_does_not_wait(self):
        s = self.store
        s.raw_performance = state(False, True, 30)
        self.run_worker()
        self.assertEqual(s.auto_chain_cancel.now, 0)
        self.send.assert_called_once()


class InitialArmTest(unittest.TestCase):
    def setUp(self):
        self.store = s = object.__new__(LiveStateStore)
        s.action_lock = threading.RLock()
        s.auto_arm_enabled = True
        s.auto_chain_cancel = threading.Event()
        s.queue = {'revision': 1}
        s.selection = {'next_executable_plan': {'plan_id': 'reviewed',
            'deck_a': {'deck': 1, 'audio_sha256': 'a', 'play_start_beat_index': 160}}}
        s.raw_performance = state(False, False)
        s.raw_performance.decks[1].beat_index = 0
        s.envelope = None
        # The old fader favors B while Mixxx has not acknowledged Start on A.
        s.current_track_id = 'b'
        s._append_system_event = Mock()
        def arm():
            reason = s._recovery_reason()
            return (409, {'error': reason}) if reason else (202, {'plan_id': 'armed'})
        s.request_arm = Mock(side_effect=arm)
        self.now = 0.0

    def run_initial(self, on_tick=lambda: None):
        def tick(seconds):
            self.now += seconds
            on_tick()
        with patch('live_bridge.time.monotonic', side_effect=lambda: self.now), \
                patch('live_bridge.time.sleep', side_effect=tick):
            self.store._arm_started_set('reviewed', 1)

    def test_delayed_start_with_old_fader_does_not_silently_abandon_plan(self):
        s = self.store
        self.assertEqual(s._recovery_reason(), 'live_source_changed')
        def acknowledge():
            s.request_arm.assert_not_called()
            s.current_track_id = 'a'
            s.raw_performance = state(True, False, 24)
            s.raw_performance.decks[1].beat_index = 0
        self.run_initial(acknowledge)
        s.request_arm.assert_called_once()
        self.assertEqual(s._append_system_event.call_args.args[0], 'Initial transition accepted for arming')

    def test_missing_play_ack_times_out_and_reports_failure_without_arm(self):
        self.run_initial()
        self.store.request_arm.assert_not_called()
        self.assertIn('source_deck_not_playing', self.store._append_system_event.call_args.args[1])
        self.assertGreaterEqual(self.now, 4)

    def test_takeover_or_replacement_during_ack_wait_cannot_arm(self):
        for change, expected in [('takeover', 'auto_arm_cancelled'),
                                 ('plan', 'initial_plan_changed_before_arm'),
                                 ('queue', 'initial_plan_changed_before_arm'),
                                 ('identity', 'initial_source_identity_changed')]:
            with self.subTest(change=change):
                self.setUp()
                s = self.store
                def replace():
                    if change == 'takeover': s.auto_chain_cancel.set()
                    elif change == 'plan': s.selection['next_executable_plan']['plan_id'] = 'replacement'
                    elif change == 'queue': s.queue['revision'] = 2
                    else: s.raw_performance.decks[1].track_id = 'x'
                self.run_initial(replace)
                s.request_arm.assert_not_called()
                self.assertIn(expected, s._append_system_event.call_args.args[1])

    def test_actual_preflight_rejection_and_exception_are_reported(self):
        for result in [(409, {'error': 'latest_arm_beat_passed'}), RuntimeError('synthetic MIDI failure')]:
            with self.subTest(result=result):
                self.setUp()
                s = self.store
                s.raw_performance.decks[1].play = True
                s.request_arm = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
                self.run_initial()
                self.assertEqual(s._append_system_event.call_args.args[0], 'Initial transition not armed')


if __name__ == '__main__':
    unittest.main()

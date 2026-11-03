"""Synthetic feedback regressions; no camera, Meta request or MIDI writes."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_future_buffer
from crowd_adaptive import bounded_orders, signal_for
from meta_planner import MetaConfig, MetaDecision, MetaSelection, prompt_for
from test_meta_planner import pair
from transition_pipeline import PairResult


class FakeCache:
    def __init__(self, *args): pass
    def payload(self, *args): return {'pairs': [pair(1, 220, 24, .9)]}
    def ensure(self, a, b): return PairResult(a, b, a+b, 1, True, ('beat_sync_crossfade',))


def observation(session, *, cue='none', goal='build', age=0):
    metric = lambda value: {'value': value, 'evidence_ids': ['e1']}
    return dict(id=str(uuid4()), captureTime=datetime.fromtimestamp(time.time()-age, timezone.utc).isoformat(),
        status='completed', mode='webcam', profile='webcam-v1', webcamSession=session, cue={'value': cue, 'evidence_ids': ['e1']},
        observation={'summary_en': 'Synthetic visible movement.', 'quality': {'visibility': 'good', 'camera_motion': 'stable'},
            'regions': [{'id': 'audience', 'visibility': 'good',
                'movement_spread': metric('most' if goal == 'build' else 'few'),
                'movement_amplitude': metric('moderate' if goal == 'build' else 'subtle'),
                'movement_continuity': metric('sustained' if goal == 'build' else 'intermittent')}],
            'evidence': [{'id': 'e1', 'start_seconds': 0, 'end_seconds': 3, 'observation': 'Synthetic arms overhead.'}]})


class CrowdAdaptiveTest(unittest.TestCase):
    def setUp(self):
        test_future_buffer.FutureBufferTest.setUp(self)
        s = self.store
        s.auto_arm_enabled = True
        s.auto_chain_cancel = threading.Event()
        s._recovery_reason = lambda: None
        s._crowd_init()
        s._crowd_record = lambda *args: None
        s.supervisor = SimpleNamespace(executor=None, manager=SimpleNamespace(envelope=SimpleNamespace(status='executing')))
        s.sampler_slots = {t: i + 1 for i, t in enumerate(s.queue['ordered_track_ids'])}
        s.raw_performance = SimpleNamespace(decks={1: SimpleNamespace(play=False),
            2: SimpleNamespace(play=True, track_id='b', beat_index=60)})
        s.set_list_manifest_path = s.workspace / 'artifacts/live-performance/tonight-set-list-v1.json'
        s.set_list_manifest_path.write_text(json.dumps({'queue_revision': 1, 'ordered_tracks': [
            {'track_id': t, 'source_path': '/synthetic/' + t} for t in s.queue['ordered_track_ids']]}))
        self.queue_path = s.workspace / 'artifacts/live-performance/queue-state-v1.json'
        self.queue_path.write_text(json.dumps(s.queue))
        self.session = str(uuid4())
        self.assertEqual(s.request_crowd_config({'enabled': True, 'session': self.session})[0], 200)

    def stage(self, callback=None, *, cache_type=FakeCache, techniques=None):
        s = self.store
        with patch('crowd_adaptive.threading.Thread'):
            s._crowd_ingest(observation(self.session, cue='build'))
        gen, context = s.crowd_generation, s._future_context()
        evidence = deepcopy(s.crowd['observation'])
        def choose(candidates, **kwargs):
            if callback: callback()
            chosen = next(r for r in candidates if r['ordered_track_ids'][2] == 'd')
            if chosen.get('in_track_planning'):
                actions = [[{'function':'cut_to_next','start_beat':0,'fade_out_seconds':.5 if t == 'fade_out_cut' else 0}]
                           for t in techniques]
                return MetaSelection(MetaDecision(chosen['candidate_id'], kwargs['intent'], True, .8,
                    'Synthetic stronger handoff.', transition_actions=actions, in_track_actions=[]), chosen, 'synthetic', len(candidates))
            return MetaSelection(MetaDecision(chosen['candidate_id'], kwargs['intent'], True, .8, 'Synthetic stronger handoff.', techniques), chosen, 'synthetic', len(candidates))
        with patch('crowd_adaptive.PairAnalysisCache', cache_type), patch('crowd_adaptive.select_with_meta', side_effect=choose), patch('crowd_adaptive.MetaConfig.from_environment', return_value=MetaConfig('synthetic')):
            s._crowd_plan_worker(gen, context, evidence)

    def test_native_tempo_future_techniques_stage_without_changing_active_plan(self):
        class CleanCache(FakeCache):
            def payload(self, *args):
                return {'clean_break_pairs': [{'rank': 1, 'a_exit': {'beat_index': 220},
                    'b_entry': {'beat_index': 24}, 'heuristic_score': .8, 'transition_mode': 'clean_break'}]}
        s=self.store
        s.selection['next_executable_plan']['mix']={}
        s.transition_pipeline.cache=CleanCache()
        active=deepcopy(s.selection['next_executable_plan'])
        self.stage(cache_type=CleanCache, techniques=('fade_out_cut','clean_cut','fade_out_cut'))
        self.assertEqual(s.crowd['status'],'staged',s.crowd['reason'])
        self.assertEqual(s.crowd['decision']['technique'],'fade_out_cut')
        self.assertEqual(s.crowd['decision']['bars'],0)
        self.assertEqual(s.crowd_pending['compiled']['next_executable_plan']['mix']['fade_out_seconds'],.5)
        self.assertEqual(s.selection['next_executable_plan'],active)

    def test_bounded_orders_keep_locked_prefix_and_every_track(self):
        ids = list('abcdefghij')
        for order in bounded_orders(ids, 3):
            self.assertEqual(order[:4], ids[:4])
            self.assertEqual(order[7:], ids[7:])
            self.assertEqual(sorted(order), ids)
        self.assertEqual(bounded_orders(ids, 9), [])

    def test_natural_signal_requires_two_distinct_fresh_observations(self):
        s = self.store
        first = observation(self.session, age=10)
        with patch('crowd_adaptive.threading.Thread') as thread:
            self.assertEqual(s._crowd_ingest(first)[0], 200)
            thread.assert_not_called()
            self.assertEqual(s._crowd_ingest(first)[0], 409)
            self.assertEqual(s._crowd_ingest(observation(self.session))[0], 202)
            thread.assert_called_once()

    def test_unknown_conflict_camera_motion_and_no_audience_hold(self):
        for mutate in (lambda j: j['cue'].update(value='conflict'),
                       lambda j: j['cue'].update(value='unknown'),
                       lambda j: j['observation']['quality'].update(camera_motion='moving'),
                       lambda j: j['observation'].update(regions=[])):
            job = observation(self.session, cue='build'); mutate(job)
            self.assertIsNone(signal_for(job)[0])
        job = observation(self.session, cue='ease')
        self.assertEqual(signal_for(job)[:2], ('ease', 'gesture'))

    def test_stale_cross_session_replay_and_uploaded_clips_cannot_control(self):
        for mutate in (lambda j: j.update(mode='uploaded_clip'), lambda j: j.update(webcamSession=str(uuid4())),
                       lambda j: j.update(captureTime='invalid'), lambda j: j.update(profile='crowd-v1')):
            job = observation(self.session, cue='build'); mutate(job)
            self.assertEqual(self.store._crowd_ingest(job)[0], 409)
        self.assertEqual(self.store._crowd_ingest(observation(self.session, age=121))[0], 409)

    def test_takeover_during_model_call_cannot_publish(self):
        self.stage(lambda: self.store._crowd_stop('Human takeover.'))
        self.assertIsNone(self.store.crowd_pending)
        self.assertFalse(self.store.crowd['enabled'])
        self.assertEqual(self.store.queue['revision'], 1)

    def test_staging_cannot_change_locked_plan_or_queue(self):
        active, queue = deepcopy(self.store.selection), deepcopy(self.store.queue)
        self.stage()
        self.assertEqual(self.store.crowd['status'], 'staged')
        self.assertEqual(self.store.selection, active)
        self.assertEqual(self.store.queue, queue)
        self.assertEqual(json.loads(self.queue_path.read_text()), queue)
        self.assertIn('bass_swap', self.store.crowd_pending['compiled']['next_executable_plan']['mix'])
        self.assertEqual(self.store._crowd_commit('b', 'c', 1), 'c')  # executing, not completed
        self.assertEqual(self.store.queue, queue)

    def test_completed_boundary_commits_future_order_and_preserves_sampler_slots(self):
        self.stage()
        s = self.store
        slots, active = deepcopy(s.sampler_slots), deepcopy(s.selection['next_executable_plan'])
        s.supervisor.manager.envelope.status = 'completed'
        self.assertEqual(s._crowd_commit('b', 'c', 1), 'd')
        self.assertEqual(s.queue['revision'], 2)
        self.assertEqual(s.queue['ordered_track_ids'][:5], list('abdce'))
        self.assertEqual(s.sampler_slots, slots)
        self.assertEqual(s.selection['next_executable_plan'], active)
        self.assertEqual(s.future_transition_plan['queue_revision'], 2)
        self.assertEqual(json.loads(self.queue_path.read_text()), s.queue)
        manifest = json.loads(s.set_list_manifest_path.read_text())
        self.assertEqual(manifest['queue_revision'], 2)
        self.assertEqual([r['track_id'] for r in manifest['ordered_tracks']], s.queue['ordered_track_ids'])
        s._verify_set_list_edge('b', 'd')
        with patch.object(s, '_maintain_ready_buffer'):
            promoted = s._promote_future_transition_plan('b', 'd', 2, 1, 60)
        self.assertEqual(promoted[0], 200)
        self.assertIn('bass_swap', s.selection['next_executable_plan']['mix'])
        self.assertEqual(s.crowd['lastApplied'].get('executionStatus'), None)

    def test_expired_lease_or_passed_cue_rejects_commit(self):
        for reason in ('lease', 'cue', 'context', 'age'):
            self.setUp()
            self.stage(); s = self.store
            s.supervisor.manager.envelope.status = 'completed'
            if reason == 'lease': s.crowd_lease = time.monotonic() - 1
            if reason == 'cue': s.raw_performance.decks[2].beat_index = 219
            if reason == 'context': s.planning_epoch = 99
            if reason == 'age': s.crowd_pending['evidence']['capturedAt'] = observation(self.session, age=121)['captureTime']
            self.assertEqual(s._crowd_commit('b', 'c', 1), 'c')
            self.assertEqual(s.queue['revision'], 1)

    def test_disk_commit_error_rolls_back_both_files(self):
        self.stage(); s = self.store
        s.supervisor.manager.envelope.status = 'completed'
        original_queue, original_manifest = self.queue_path.read_bytes(), s.set_list_manifest_path.read_bytes()
        with patch.object(s, '_crowd_record', side_effect=OSError('Synthetic disk full')):
            self.assertEqual(s._crowd_commit('b', 'c', 1), 'c')
        self.assertEqual(self.queue_path.read_bytes(), original_queue)
        self.assertEqual(s.set_list_manifest_path.read_bytes(), original_manifest)
        self.assertEqual(s.queue['revision'], 1)

    def test_unknown_during_inference_invalidates_pending_worker(self):
        s = self.store
        with patch('crowd_adaptive.threading.Thread'):
            s._crowd_ingest(observation(self.session, cue='build', age=3))
            generation = s.crowd_generation
            s._crowd_ingest(observation(self.session, cue='unknown'))
        self.assertGreater(s.crowd_generation, generation)

    def test_prompt_contains_evidence_and_per_candidate_track_order(self):
        self.stage()
        p = self.store.crowd_pending
        prompt = json.loads(prompt_for([p['compiled']['selected_candidate']], queue_revision=1,
            intent='impact_at_handoff', track_ids=tuple('bcde'), crowd_context=p['evidence']))
        self.assertEqual(prompt['crowd_context']['jobId'], p['evidence']['jobId'])
        self.assertEqual(prompt['candidate_data'][0]['window_track_ids'], list('bdce'))


    def test_only_owner_can_disable_or_renew_an_active_session(self):
        s = self.store
        other = str(uuid4())
        self.assertEqual(s.request_crowd_config({'enabled': False, 'session': other})[0], 409)
        self.assertEqual(s.request_crowd_config({'enabled': True, 'session': other})[0], 409)
        self.assertEqual(s.request_crowd_heartbeat({'session': other})[0], 409)
        self.assertTrue(s.crowd['enabled'])
        self.assertEqual(s.request_crowd_config({'enabled': False, 'session': self.session})[0], 200)
        self.assertFalse(s.crowd['enabled'])

    def test_existing_takeover_disables_crowd_even_without_an_owned_executor(self):
        s = self.store
        s.supervisor = None
        s.auto_chain_thread = None
        s.request_takeover()
        self.assertFalse(s.crowd['enabled'])
        self.assertFalse(s.auto_arm_enabled)
        self.assertTrue(s.auto_chain_cancel.is_set())


    def test_single_natural_reversal_does_not_cancel_held_gesture(self):
        self.stage()
        s = self.store
        pending = s.crowd_pending
        with patch('crowd_adaptive.threading.Thread') as thread:
            s._crowd_ingest(observation(self.session, goal='ease'))
            self.assertIs(s.crowd_pending, pending)
            thread.assert_not_called()
            s._crowd_ingest(observation(self.session, goal='ease'))
            self.assertIsNone(s.crowd_pending)
            thread.assert_called_once()

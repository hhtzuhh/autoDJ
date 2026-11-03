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

    def test_observation_and_disable_responses_retain_memory_for_ui(self):
        s = self.store
        # These responses replace the frontend state, just like heartbeat/status.
        # Include hold/unknown and queued evidence, even without a Meta decision.
        with patch('crowd_adaptive.threading.Thread'):
            for cue in ('unknown', 'build'):
                job = observation(self.session, cue=cue)
                status, response = s._crowd_ingest(job)
                self.assertIn(status, (200, 202))
                self.assertEqual(response['memory'], s.crowd_snapshot()['memory'])
                self.assertTrue(any(row['data'].get('jobId') == job['id']
                    for row in response['memory']['recent']))
        s._memory().record('execution', {'status': 'completed'}, ['b', 'd'])
        _, heartbeat = s.request_crowd_heartbeat({'session': self.session})
        _, stopped = s.request_crowd_config({'enabled': False, 'session': self.session})
        self.assertFalse(stopped['enabled'])
        self.assertEqual(stopped['memory'], heartbeat['memory'])
        self.assertEqual(stopped['memory']['recent'][-1]['data']['status'], 'completed')

    def test_late_observation_from_previous_set_cannot_seed_next_set(self):
        s = self.store
        s._begin_set_run()
        previous = s._run_id()
        job = observation(self.session, cue='reorder')
        job['setRunId'] = previous
        s._begin_set_run()
        with patch('crowd_adaptive.threading.Thread'):
            status, result = s._crowd_ingest(job)
        self.assertEqual(status, 200)
        self.assertIn('ignoredObservation', result)
        self.assertIsNone(s.crowd_requested)
        self.assertEqual(s._memory().context(['a'])['recentObservations'], [])
        self.assertEqual(s._memory().events[-1]['setRunId'], previous)
        job = observation(self.session, cue='unknown')
        job['setRunId'] = s._run_id()
        status, _ = s._crowd_ingest(job)
        self.assertEqual(status, 200)
        self.assertEqual(len(s._memory().context(['a'])['recentObservations']), 1)

    def stage(self, callback=None, *, cache_type=FakeCache, techniques=None, in_track_actions=None, cue='reorder', inspect_candidates=None):
        s = self.store
        with patch('crowd_adaptive.threading.Thread'):
            s._crowd_ingest(observation(self.session, cue=cue))
        gen, context = s.crowd_generation, s._future_context()
        evidence = deepcopy(s.crowd['observation'])
        def choose(candidates, **kwargs):
            if inspect_candidates: inspect_candidates(candidates, kwargs)
            if callback: callback()
            chosen = next(r for r in candidates if r['ordered_track_ids'][2] == ('d' if cue == 'reorder' else 'c'))
            if chosen.get('in_track_planning'):
                actions = [[{'function':'crossfade','start_beat':0,'duration_beats':e['transition_bars']*4,'curve':'linear','target_bpm':120}]
                           for e in chosen['transitions']]
                return MetaSelection(MetaDecision(chosen['candidate_id'], kwargs['intent'], True, .8,
                    'Synthetic stronger handoff.', transition_actions=actions, in_track_actions=in_track_actions or []), chosen, 'synthetic', len(candidates))
            return MetaSelection(MetaDecision(chosen['candidate_id'], kwargs['intent'], True, .8, 'Synthetic stronger handoff.', techniques), chosen, 'synthetic', len(candidates))
        with patch('crowd_adaptive.PairAnalysisCache', cache_type), patch('crowd_adaptive.select_with_meta', side_effect=choose), patch('crowd_adaptive.MetaConfig.from_environment', return_value=MetaConfig('synthetic')):
            s._crowd_plan_worker(gen, context, evidence)

    def test_cut_only_future_does_not_change_active_plan(self):
        class CleanCache(FakeCache):
            def payload(self, *args):
                return {'clean_break_pairs': [{'rank': 1, 'a_exit': {'beat_index': 220},
                    'b_entry': {'beat_index': 24}, 'heuristic_score': .8, 'transition_mode': 'clean_break'}]}
        s = self.store
        active = deepcopy(s.selection)
        self.stage(cache_type=CleanCache)
        self.assertNotEqual(s.crowd['status'], 'staged')
        self.assertIsNone(s.crowd_pending)
        self.assertEqual(s.selection, active)

    def test_bounded_orders_keep_locked_prefix_and_every_track(self):
        ids = list('abcdefghij')
        for order in bounded_orders(ids, 3):
            self.assertEqual(order[:4], ids[:4])
            # A distant remaining song can move next; all others stay stable.
            self.assertEqual(order[5:], [t for t in ids[4:] if t != order[4]])
            self.assertEqual(sorted(order), ids)
        self.assertIn(ids[:4] + ['j'] + ids[4:9], bounded_orders(ids, 3))
        self.assertEqual(bounded_orders(ids, 9), [])

    def test_number_one_bypasses_cooldown_but_requires_release_to_repeat(self):
        s = self.store
        s.crowd_last_apply = time.monotonic()
        s.crowd_retry_at = time.monotonic() + 60
        with patch.object(s, '_schedule_future_transition_plan') as schedule:
            status, response = s._crowd_ingest(observation(self.session, cue='reorder'))
            self.assertEqual(status, 202)
            self.assertEqual(response['observation']['goal'], 'reorder')
            self.assertEqual(s.crowd_retry_at, 0)
            schedule.assert_called_once()
            generation = s.crowd_generation
            s._crowd_ingest(observation(self.session, cue='reorder'))
            self.assertEqual(s.crowd_generation, generation)
            schedule.assert_called_once()
            # A clear released pose re-arms the gesture, not a conflict/unknown.
            s._crowd_ingest(observation(self.session, cue='none'))
            self.assertEqual(s.crowd_requested['goal'], 'reorder')
            self.assertEqual(s._crowd_ingest(observation(self.session, cue='reorder'))[0], 202)
            self.assertEqual(schedule.call_count, 2)

    def test_number_one_excludes_unchanged_order_from_model_candidates(self):
        s = self.store
        original = deepcopy(s.queue['ordered_track_ids'])
        def inspect(candidates, kwargs):
            self.assertTrue(candidates)
            for candidate in candidates:
                self.assertNotEqual(candidate['ordered_track_ids'], original)
                self.assertEqual(candidate['ordered_track_ids'][:2], original[:2])
            self.assertIn('explicitly held the number 1', kwargs['crowd_context']['policy'])
        self.stage(cue='reorder', inspect_candidates=inspect)
        self.assertEqual(s.crowd['status'], 'staged', s.crowd['reason'])
        self.assertTrue(s.crowd['decision']['queueChanged'])
        self.assertEqual(s.queue['ordered_track_ids'], original)

    def test_arms_up_and_crossed_arms_each_require_release_before_repeat(self):
        s = self.store
        with patch.object(s, '_schedule_future_transition_plan') as schedule:
            for cue in ('build', 'ease'):
                self.assertEqual(s._crowd_ingest(observation(self.session, cue=cue))[0], 202)
                count = schedule.call_count
                s._crowd_ingest(observation(self.session, cue=cue))
                self.assertEqual(schedule.call_count, count)
                s._crowd_ingest(observation(self.session, cue='unknown'))
                s._crowd_ingest(observation(self.session, cue=cue))
                self.assertEqual(schedule.call_count, count)
                s._crowd_ingest(observation(self.session, cue='none'))
                self.assertEqual(s._crowd_ingest(observation(self.session, cue=cue))[0], 202)
                self.assertEqual(schedule.call_count, count + 1)

    def test_in_track_request_without_action_change_keeps_existing_plan(self):
        s = self.store
        original = deepcopy(s.selection)
        def inspect(candidates, kwargs):
            self.assertTrue(all(row['ordered_track_ids'] == s.queue['ordered_track_ids'] for row in candidates))
            self.assertEqual(kwargs['crowd_context']['requested_change'], 'in_track_actions')
            self.assertIn('[] remains valid', kwargs['crowd_context']['policy'])
        with patch.object(s, '_crowd_record') as record:
            self.stage(cue='build', inspect_candidates=inspect)
        self.assertIsNone(s.crowd_pending)
        self.assertEqual(s.crowd['decision']['historyStatus'], 'unchanged')
        self.assertEqual(s.selection, original)
        self.assertEqual(record.call_args.args[0], 'unchanged')
        change = s.crowd['decision']['changeSummary']
        self.assertEqual(change['beforeWindow'], change['afterWindow'])

    def test_stopping_staged_request_records_terminal_reason(self):
        s = self.store
        self.stage()
        with patch.object(s, '_crowd_record') as record:
            s._crowd_stop('Operator stopped camera.')
        self.assertIsNone(s.crowd_pending)
        self.assertEqual(record.call_args.args[0], 'discarded')
        self.assertEqual(s.crowd['decision']['outcomeReason'], 'Operator stopped camera.')

    def test_number_one_needs_two_unlocked_songs_and_clear_visibility(self):
        s = self.store
        job = observation(self.session, cue='reorder')
        job['observation']['quality']['visibility'] = 'insufficient'
        self.assertEqual(signal_for(job)[1], 'uncertain')
        s.queue['ordered_track_ids'] = list('abc')
        with patch.object(s, '_schedule_future_transition_plan') as schedule:
            status, response = s._crowd_ingest(observation(self.session, cue='reorder'))
        self.assertEqual(status, 200)
        self.assertIn('fewer than two unlocked songs', response['reason'])
        schedule.assert_not_called()

    def test_number_one_never_falls_back_to_unchanged_order(self):
        s = self.store
        original = deepcopy(s.queue)
        with patch('crowd_adaptive.bounded_orders', return_value=[original['ordered_track_ids']]), \
                patch('crowd_adaptive.select_with_meta') as model:
            with patch('crowd_adaptive.threading.Thread'):
                s._crowd_ingest(observation(self.session, cue='reorder'))
            s._crowd_plan_worker(s.crowd_generation, s._future_context(), s.crowd['observation'])
        model.assert_not_called()
        self.assertIsNone(s.crowd_pending)
        self.assertEqual(s.queue, original)
        self.assertIn('No different remaining order', s.crowd['reason'])

    def test_webcam_comparison_freezes_prior_future_plan_and_excludes_locked_edge(self):
        s = self.store
        old_edge = dict(source_track_id='b', target_track_id='c', technique='crossfade',
            source_exit_beat_index=180, target_entry_beat_index=24, bars=4,
            routine={'actions': [{'function': 'crossfade', 'start_beat': 0, 'duration_beats': 16}],
                     'in_track_actions': []})
        s.selection['transition_buffer'] = [dict(source_track_id='a', target_track_id='b'), old_edge]
        self.stage()
        change = s.crowd['decision']['changeSummary']
        self.assertEqual(change['beforeWindow'][0]['sourceTrack'], 'b')
        self.assertEqual(change['beforeWindow'][0]['targetTrack'], 'c')
        self.assertEqual(change['afterWindow'][0]['targetTrack'], 'd')
        self.assertEqual(change['observationSummary'], 'Synthetic visible movement.')
        old_edge['routine']['actions'][0]['duration_beats'] = 64
        self.assertEqual(change['beforeWindow'][0]['transitionActions'][0]['duration_beats'], 16)

    def test_webcam_comparison_prefers_the_current_ready_future_window(self):
        s = self.store
        context = s._future_context()
        s.future_transition_plan = {'based_on_active_plan_id': context[0], 'queue_revision': context[1],
            'based_on_planning_epoch': context[2], 'transition_buffer': [
                dict(source_track_id='b', target_track_id='c', technique='bass_swap')]}
        self.stage()
        self.assertEqual(s.crowd['decision']['changeSummary']['beforeWindow'][0]['technique'], 'bass_swap')

    def test_natural_movement_records_without_planning_even_after_repetition(self):
        s = self.store
        first = observation(self.session, age=10)
        with patch('crowd_adaptive.threading.Thread') as thread:
            self.assertEqual(s._crowd_ingest(first)[0], 200)
            thread.assert_not_called()
            self.assertEqual(s._crowd_ingest(first)[0], 409)
            self.assertEqual(s._crowd_ingest(observation(self.session))[0], 200)
            thread.assert_not_called()
            self.assertEqual(len(s._memory().context(['a'])['recentEpisodes']), 2)

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
            self.assertIs(s.crowd_pending, pending)
            thread.assert_not_called()

    def test_crowd_waits_behind_normal_window_and_scheduler_prioritizes_it_afterward(self):
        s = self.store
        s.future_plan_in_progress = True
        with patch('crowd_adaptive.threading.Thread') as thread:
            s._crowd_ingest(observation(self.session, cue='build'))
            thread.assert_not_called()
            self.assertIsNotNone(s.crowd_requested)
            s.future_plan_in_progress = False
            self.assertTrue(s._schedule_future_transition_plan())
            self.assertTrue(s.crowd_busy)
            self.assertFalse(s.future_plan_in_progress)
            thread.assert_called_once()
            s._schedule_future_transition_plan()
            thread.assert_called_once()  # no ordinary window alongside crowd inference

    def test_pending_window_keeps_existing_buffer_without_competing_call(self):
        self.stage()
        with patch('live_bridge.threading.Thread') as thread:
            self.assertFalse(self.store._schedule_future_transition_plan())
            thread.assert_not_called()

    def test_failure_before_commit_is_visible_and_not_reported_as_applied(self):
        self.stage()
        s = self.store
        original = deepcopy(s.queue)
        del s._crowd_record
        s._crowd_rotation_failed('rotation_readback_timeout:outgoing_stop_not_confirmed')
        self.assertEqual(s.queue, original)
        self.assertIsNone(s.crowd_pending)
        self.assertIsNone(s.crowd['lastApplied'])
        self.assertEqual(s.crowd['status'], 'application_failed')
        self.assertEqual(s.crowd['decision']['applicationStatus'], 'failed')
        self.assertIn('outgoing_stop_not_confirmed', s.crowd['decision']['applicationError'])
        self.assertEqual(s._memory().snapshot()['recent'][-1]['data']['status'], 'application_failed')

    def test_unknown_clears_queued_request_but_is_retained_in_memory(self):
        s = self.store
        s.future_plan_in_progress = True
        s._crowd_ingest(observation(self.session, cue='build', age=2))
        s._crowd_ingest(observation(self.session, cue='unknown'))
        self.assertIsNone(s.crowd_requested)
        rows = s._memory().context(['a'])['recentObservations']
        self.assertEqual(len(rows), 2)
        self.assertEqual(s._memory().snapshot()['episodes'][0]['crowd']['signalSource'], 'uncertain')

    def test_adaptive_decision_receives_memory_and_reaches_persistent_applied_record(self):
        s = self.store
        del s._crowd_record  # exercise the real audit + memory writer
        self.stage()
        self.assertTrue(s.crowd['decision']['memoryContext']['recentObservations'])
        self.assertEqual(s._memory().snapshot()['recent'][-1]['data']['status'], 'staged')
        s.supervisor.manager.envelope.status = 'completed'
        s._crowd_commit('b', 'c', 1)
        self.assertEqual(s._memory().snapshot()['recent'][-1]['data']['status'], 'queue_committed')
        s._crowd_execution_status('completed', s.crowd['lastApplied']['planId'])
        self.assertEqual(s._memory().snapshot()['recent'][-1]['data']['status'], 'completed')

    def test_reordered_candidate_in_track_windows_use_its_own_track_ids(self):
        candidate = {'candidate_id': 'test', 'action_planning': True, 'in_track_planning': True,
            'window_track_ids': list('bdce'), 'in_track_source_ids': list('bdc'),
            'transitions': [{'mode': 'clean_break', 'source_exit_beat_index': 220,
                             'target_entry_beat_index': 24, 'transition_bars': 0} for _ in range(3)]}
        prompt = json.loads(prompt_for([candidate], queue_revision=1, intent='maintain_energy', track_ids=tuple('bcde')))
        self.assertEqual([r['track_id'] for r in prompt['candidate_data'][0]['in_track_windows']], list('bdc'))

    def test_full_tail_options_fit_request_budget_and_include_distant_songs(self):
        for count in (7, 10, 50):
            ids = [str(i) for i in range(count)]
            orders = bounded_orders(ids, 1)
            self.assertLessEqual(len(orders), 10)
            self.assertEqual(orders[0], ids)
            self.assertEqual(orders[-1][2], ids[-1])
            for row in orders:
                self.assertEqual(row[:2], ids[:2])
                self.assertEqual(sorted(row), sorted(ids))

    def test_reordered_window_retains_nonempty_in_track_action_through_promotion(self):
        class BlendCache(FakeCache):
            def payload(self, *args):
                from performance_actions import tempo_bounds
                return {**super().payload(*args), 'tempo':{'shared_tempo_bounds':tempo_bounds(120,120)}}
        s = self.store
        s.transition_pipeline.cache = BlendCache()
        actions = [{'function': 'bass_pulse', 'track_id': 'b', 'start_beat': 80, 'duration_beats': 8, 'depth': .2}]
        self.stage(cache_type=BlendCache, in_track_actions=actions, cue='build')
        self.assertEqual(s.crowd['status'], 'staged', s.crowd['reason'])
        self.assertEqual(s.crowd['decision']['inTrackActions'], actions)
        s.supervisor.manager.envelope.status = 'completed'
        self.assertEqual(s._crowd_commit('b', 'c', 1), 'c')
        with patch.object(s, '_maintain_ready_buffer'):
            self.assertEqual(s._promote_future_transition_plan('b', 'c', 2, 1, 60)[0], 200)
        self.assertEqual(s.selection['next_executable_plan']['in_track_actions'], actions)

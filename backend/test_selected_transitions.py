import json
from pathlib import Path
from analysis_config import analysis_root
from copy import deepcopy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, MagicMock, patch

from set_builder import SetBuilderService
from set_activation import prepare_activation
from live_bridge import LiveStateStore
from test_set_builder import metadata, write_analyzed_library


def wait(service):
    deadline=time.monotonic()+3
    while service.jobs.unfinished_tasks and time.monotonic()<deadline:time.sleep(.005)
    assert service.jobs.unfinished_tasks==0


class SelectedTransitionsTest(unittest.TestCase):
    def service(self, root, pair_builder=None):
        ids=write_analyzed_library(root,count=3,bpms=(90,140,100))
        service=SetBuilderService(root,metadata_reader=metadata,
            curator=Mock(return_value=(ids,'Synthetic selected order')),
            post_curator=Mock(side_effect=AssertionError('Do not change selected order')),
            pair_builder=pair_builder or Mock(return_value=(5,'clean_break_required')))
        service.build('three',3)
        return service,ids

    def test_selected_order_prepares_exports_and_survives_restart(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);service,ids=self.service(root)
            self.assertEqual(service.snapshot()['status'],'selected')
            with self.assertRaises(ValueError):prepare_activation(root,service.snapshot(),active_queue_revision=0)
            service.prepare_selected_transitions();wait(service)
            state=service.snapshot()
            self.assertEqual(state['status'],'ready');self.assertTrue(state['activationReady'])
            self.assertEqual(state['orderedTrackIds'],list(ids))
            self.assertEqual(service.pair_builder.call_count,2)
            service.post_curator.assert_not_called();self.assertEqual(service.curator.call_count,1)
            exported=prepare_activation(root,state,active_queue_revision=0)
            self.assertEqual(json.loads(exported.activation_path.read_text())['builder_revision'],state['queueRevision'])
            self.assertFalse((root/'artifacts/live-performance/queue-state-v1.json').exists())
            restored=SetBuilderService(root,metadata_reader=metadata,start_worker=False)
            self.assertEqual(restored.snapshot()['status'],'ready')
            self.assertEqual(restored.queued_jobs,set())
            restored.curator=lambda *args:(ids,'Another selection')
            restored.build('again',3)
            self.assertEqual(restored.snapshot()['status'],'selected')
            self.assertEqual(restored.snapshot()['transitions'],[])
            self.assertFalse(restored.snapshot()['activationReady'])

    def test_blocked_candidates_do_not_enable_export_and_can_retry(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);pairs=Mock(side_effect=[0,5,5,5]);service,ids=self.service(root,pairs)
            service.prepare_selected_transitions();wait(service)
            self.assertEqual(service.snapshot()['status'],'blocked')
            self.assertFalse(service.snapshot()['activationReady'])
            with self.assertRaises(ValueError):prepare_activation(root,service.snapshot(),active_queue_revision=0)
            service.prepare_selected_transitions();wait(service)
            self.assertEqual(service.snapshot()['status'],'ready')
            self.assertEqual(service.snapshot()['orderedTrackIds'],list(ids))

    def test_missing_selection_missing_analysis_and_busy_are_rejected(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);service=SetBuilderService(root,start_worker=False)
            with self.assertRaises(ValueError):service.prepare_selected_transitions()
        with TemporaryDirectory() as raw:
            root=Path(raw);service,ids=self.service(root)
            service.queued_jobs.add(('synthetic',))
            with self.assertRaises(ValueError):service.prepare_selected_transitions()
            service.queued_jobs.clear()
            (analysis_root(root)/ids[0]/'track-analysis-v1.json').write_text('{}')
            with self.assertRaises(ValueError):service.prepare_selected_transitions()
            self.assertFalse(service.snapshot()['activationReady'])

    def start_store(self, root, *, meta_ok):
        service,ids=self.service(root)
        service.prepare_selected_transitions();wait(service)
        prepare_activation(root,service.snapshot(),active_queue_revision=0)
        store=object.__new__(LiveStateStore);store.workspace=root;store._data_root=root
        store.lock=threading.Lock();store.action_lock=threading.RLock();store.activation_in_progress=False
        store.set_builder=service;store._executor_running=Mock(return_value=False)
        keys=[json.loads((analysis_root(root)/ident/'track-analysis-v1.json').read_text())['analysis_cache_key'] for ident in ids]
        store.raw_performance=SimpleNamespace(revision=1,decks={n:SimpleNamespace(play=False,track_id=ids[n-1],analysis_cache_key=keys[n-1],beat_index=0) for n in (1,2)})
        store.prepared_performance=None;store.performance_plan_state={'status':'idle'}
        store._sampler_slot_registry=Mock(return_value={ident:i+1 for i,ident in enumerate(ids)})
        store._wait_for_loaded_track_after_revision=Mock(return_value=True)
        store.queue={'revision':0,'ordered_track_ids':[]};store.current_track_id=None
        store.selection={};store.supervisor=None;store.envelope=None;store.transition_overrides={};store.sampler_slots={}
        store.future_transition_plan=None;store.continuous_set_active=False;store.auto_arm_enabled=False
        store.auto_chain_cancel=threading.Event();store._arm_started_set=Mock()
        store._install_active_queue=Mock(side_effect=lambda value:setattr(store,'queue',value))
        store._set_selection=Mock(side_effect=lambda value:setattr(store,'selection',value))
        store._write_dashboard_selection=Mock();store._append_system_event=Mock()
        def meta():
            if not meta_ok:return 409,{'error':'synthetic_meta_failure','detail':'source tempo ramp requires at least 16 beats'}
            store.selection={'model':'synthetic', 'planning_window_track_ids':list(ids), 'model_decision':{'reason':'test'},
                'next_executable_plan':{'plan_id':'synthetic-plan',
                    'deck_a':{'deck':1,'audio_sha256':ids[0],'analysis_cache_key':keys[0],'play_start_beat_index':80},
                    'deck_b':{'deck':2,'audio_sha256':ids[1],'analysis_cache_key':keys[1]}}}
            return 200,{'plan_id':'synthetic-plan'}
        store.request_meta_plan=Mock(side_effect=meta)
        return store

    def probe(self):
        return SimpleNamespace(**{name:Mock() for name in ['port_name','send_library_value','send_load_sampler_value','send_stage_sampler_value','send_sync_value','send_value']})

    def test_build_then_start_uses_reviewed_plan_without_another_meta_call(self):
        for meta_ok in [False,True]:
            with self.subTest(meta_ok=meta_ok),TemporaryDirectory() as raw:
                store=self.start_store(Path(raw),meta_ok=meta_ok)
                probe=self.probe()
                with patch.dict('sys.modules',{'mido':SimpleNamespace(open_output=MagicMock()),'probe':probe}),patch('live_bridge.time.sleep'):
                    status,payload=store.request_build_performance_plan()
                    self.assertEqual(status,202 if meta_ok else 409)
                    probe.send_value.assert_not_called()
                    self.assertFalse(store.continuous_set_active)
                    self.assertFalse(store.activation_in_progress)
                    self.assertEqual(store.request_meta_plan.call_count,1)
                    self.assertTrue(all(call.kwargs.get('timeout') == 12.0
                        for call in store._wait_for_loaded_track_after_revision.call_args_list))
                    if meta_ok:
                        self.assertEqual(store._performance_plan_snapshot()['status'],'ready')
                        self.assertEqual(store.request_arm()[0],409)
                        self.assertFalse(store._schedule_future_transition_plan())
                    else:
                        self.assertEqual(payload['error'],'initial_meta_plan_required')
                        self.assertIn('requires at least 16 beats',payload['message'])
                        self.assertIn('requires at least 16 beats',store.performance_plan_state['error'])
                        self.assertEqual(store.queue,{'revision':0,'ordered_track_ids':[]})
                        self.assertEqual(store._performance_plan_snapshot()['status'],'failed')
                    probe.send_load_sampler_value.reset_mock()
                    status,_=store.request_start_set()
                    self.assertEqual(status,202 if meta_ok else 409)
                    self.assertEqual(store.request_meta_plan.call_count,1)
                    probe.send_load_sampler_value.assert_not_called()
                    plays=[c for c in probe.send_value.call_args_list if c.args[1:]==('1','play',1.0)]
                    self.assertEqual(len(plays),1 if meta_ok else 0)
                    self.assertEqual(store.auto_arm_enabled,meta_ok)
                    if meta_ok:
                        self.assertEqual(store._run_id(), 'set-000001')
                        self.assertEqual(store.selection['next_executable_plan']['set_run_id'], 'set-000001')
                        self.assertEqual(store._runs().current['status'], 'started')
                    self.assertEqual(store.request_start_set()[0],409)
                    self.assertEqual(len(store._runs().rows), 1 if meta_ok else 0)

    def test_preparation_wait_accepts_delayed_identity_but_rejects_wrong_or_stale(self):
        for observed_id, revision, accepted in [('expected',2,True),('wrong',2,False),('expected',1,False)]:
            with self.subTest(observed_id=observed_id,revision=revision):
                store=object.__new__(LiveStateStore);store.lock=threading.Lock()
                store.raw_performance=SimpleNamespace(revision=1,decks={1:SimpleNamespace(track_id=None)})
                elapsed=[0.0]
                def advance(seconds):
                    elapsed[0]+=seconds
                    if elapsed[0]>=5:
                        store.raw_performance.revision=revision
                        store.raw_performance.decks[1].track_id=observed_id
                with patch('live_bridge.time.monotonic',side_effect=lambda:elapsed[0]),patch('live_bridge.time.sleep',side_effect=advance):
                    self.assertEqual(store._wait_for_loaded_track_after_revision(1,'expected',1,timeout=12.0),accepted)
                self.assertGreaterEqual(elapsed[0],5)

    def test_wrong_mixxx_selection_reports_expected_and_observed_without_planning(self):
        with TemporaryDirectory() as raw:
            store=self.start_store(Path(raw),meta_ok=True)
            expected_id,observed_id=store.set_builder.snapshot()['orderedTrackIds'][:2]
            store.raw_performance.decks[1].track_id=observed_id
            store._wait_for_loaded_track_after_revision.return_value=False
            probe=self.probe()
            with patch.dict('sys.modules',{'mido':SimpleNamespace(open_output=MagicMock()),'probe':probe}),patch('live_bridge.time.sleep'):
                status,payload=store.request_build_performance_plan()
            self.assertEqual(status,409)
            self.assertEqual(payload['track_id'],expected_id)
            self.assertEqual(payload['observed_track_id'],observed_id)
            self.assertIn('select the first song',payload['message'])
            self.assertEqual(store.performance_plan_state['error'],payload['message'])
            self.assertEqual(store.queue['ordered_track_ids'],[])
            store.request_meta_plan.assert_not_called()
            probe.send_library_value.assert_not_called()
            probe.send_value.assert_not_called()

    def test_start_without_build_neither_calls_meta_nor_controls_mixxx(self):
        with TemporaryDirectory() as raw:
            store=self.start_store(Path(raw),meta_ok=True)
            with patch.dict('sys.modules',{'mido':None,'probe':None}):
                self.assertEqual(store.request_start_set()[0],409)
            store.request_meta_plan.assert_not_called()

    def test_changed_bindings_restart_and_takeover_reject_reviewed_plan(self):
        mutations={
            'queue':lambda s:s.queue.update(revision=999),
            'selection':lambda s:s.selection['next_executable_plan'].update(plan_id='changed'),
            'identity':lambda s:setattr(s.raw_performance.decks[1],'track_id','unknown'),
            'analysis':lambda s:setattr(s.raw_performance.decks[2],'analysis_cache_key','new'),
            'seek':lambda s:setattr(s.raw_performance.decks[1],'beat_index',5),
            'playing':lambda s:setattr(s.raw_performance.decks[1],'play',True),
            'restart':lambda s:setattr(s,'prepared_performance',None),
            'takeover':lambda s:s.request_takeover(),
            'reexport':lambda s:prepare_activation(s.data_root,s.set_builder.snapshot(),active_queue_revision=10),
        }
        for label,mutate in mutations.items():
            with self.subTest(label=label),TemporaryDirectory() as raw:
                store=self.start_store(Path(raw),meta_ok=True);probe=self.probe()
                with patch.dict('sys.modules',{'mido':SimpleNamespace(open_output=MagicMock()),'probe':probe}),patch('live_bridge.time.sleep'):
                    self.assertEqual(store.request_build_performance_plan()[0],202)
                    mutate(store)
                    self.assertNotEqual(store._performance_plan_snapshot()['status'],'ready')
                    self.assertEqual(store.request_start_set()[0],409)
                    probe.send_value.assert_not_called()
                    self.assertEqual(store.request_meta_plan.call_count,1)

    def test_new_preparation_invalidates_old_export_even_with_same_song_order(self):
        with TemporaryDirectory() as raw:
            store=self.start_store(Path(raw),meta_ok=True)
            mounted=store._activation_snapshot()
            self.assertIsNotNone(mounted)
            builder=store.set_builder.snapshot()
            self.assertEqual(mounted['builderRevision'],builder['queueRevision'])
            self.assertEqual(mounted['orderedTrackIds'],builder['orderedTrackIds'])
            store.set_builder.prepare_selected_transitions();wait(store.set_builder)
            self.assertIsNone(store._activation_snapshot())
            status,payload=store.request_build_performance_plan()
            self.assertEqual(status,409);self.assertEqual(payload['error'],'prepared_playlist_is_stale')
            store.request_meta_plan.assert_not_called()

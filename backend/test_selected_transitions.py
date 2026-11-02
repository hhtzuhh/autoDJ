import json
from pathlib import Path
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
            (root/'artifacts/time-map'/ids[0]/'track-analysis-v1.json').write_text('{}')
            with self.assertRaises(ValueError):service.prepare_selected_transitions()
            self.assertFalse(service.snapshot()['activationReady'])

    def start_store(self, root, *, meta_ok):
        service,ids=self.service(root)
        service.prepare_selected_transitions();wait(service)
        prepare_activation(root,service.snapshot(),active_queue_revision=0)
        store=object.__new__(LiveStateStore);store.workspace=root;store._data_root=root
        store.lock=threading.Lock();store.action_lock=threading.RLock();store.activation_in_progress=False
        store.set_builder=service;store._executor_running=Mock(return_value=False)
        store.raw_performance=SimpleNamespace(revision=1,decks={1:SimpleNamespace(play=False),2:SimpleNamespace(play=False)})
        store._sampler_slot_registry=Mock(return_value={ident:i+1 for i,ident in enumerate(ids)})
        store._wait_for_loaded_track_after_revision=Mock(return_value=True)
        store.queue={'revision':0,'ordered_track_ids':[]};store.current_track_id=None
        store.selection={};store.supervisor=None;store.envelope=None;store.transition_overrides={};store.sampler_slots={}
        store.future_transition_plan=None;store.continuous_set_active=False;store.auto_arm_enabled=False
        store._install_active_queue=Mock(side_effect=lambda value:setattr(store,'queue',value))
        store._set_selection=Mock(side_effect=lambda value:setattr(store,'selection',value))
        store._write_dashboard_selection=Mock();store._append_system_event=Mock()
        store.request_meta_plan=Mock(return_value=(200,{'plan_id':'synthetic-plan'}) if meta_ok else (409,{'error':'synthetic_meta_failure'}))
        return store

    def test_start_never_plays_without_initial_meta_plan(self):
        for meta_ok in [False,True]:
            with self.subTest(meta_ok=meta_ok),TemporaryDirectory() as raw:
                store=self.start_store(Path(raw),meta_ok=meta_ok)
                probe=SimpleNamespace(**{name:Mock() for name in ['port_name','send_library_value','send_load_sampler_value','send_stage_sampler_value','send_sync_value','send_value']})
                events=[]
                original=store.request_meta_plan
                store.request_meta_plan=lambda: (events.append('meta') or original())
                probe.send_value.side_effect=lambda *args:events.append('play') if args[1:]==('1','play',1.0) else None
                with patch.dict('sys.modules',{'mido':SimpleNamespace(open_output=MagicMock()),'probe':probe}),patch('live_bridge.time.sleep'):
                    status,payload=store.request_start_set()
                self.assertEqual(status,202 if meta_ok else 409)
                self.assertEqual(events,['meta','play'] if meta_ok else ['meta'])
                self.assertFalse(store.activation_in_progress)
                if not meta_ok:
                    self.assertEqual(payload['error'],'initial_meta_plan_required')
                    self.assertEqual(store.queue,{'revision':0,'ordered_track_ids':[]})

    def test_new_preparation_invalidates_old_export_even_with_same_song_order(self):
        with TemporaryDirectory() as raw:
            store=self.start_store(Path(raw),meta_ok=True)
            self.assertIsNotNone(store._activation_snapshot())
            store.set_builder.prepare_selected_transitions();wait(store.set_builder)
            self.assertIsNone(store._activation_snapshot())
            status,payload=store.request_start_set()
            self.assertEqual(status,409);self.assertEqual(payload['error'],'prepared_playlist_is_stale')
            store.request_meta_plan.assert_not_called()

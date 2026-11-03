from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import threading

from set_builder import SetBuilderService
from test_set_builder import metadata, write_analyzed_library
from test_transition_pipeline import analysis_payload
from live_bridge import LiveStateStore


class PlaylistImportTest(unittest.TestCase):
    def service(self, root):
        ids = write_analyzed_library(root, count=3)
        no_call = Mock(side_effect=AssertionError("Import must not call analysis or Meta"))
        service = SetBuilderService(root, metadata_reader=metadata, curator=no_call,
                                    analyzer=no_call, pair_builder=no_call, start_worker=False)
        return service, ids, no_call

    def test_preview_preserves_state_then_import_keeps_order_and_survives_restart(self):
        with TemporaryDirectory() as raw:
            root = Path(raw); service, ids, no_call = self.service(root)
            paths = [service.catalog[t].path for t in ids]
            content = f"\ufeff#EXTM3U\r\n#EXTINF:120,Synthetic\r\n{paths[2]}\r\n{paths[0].as_uri()}\r\n{paths[1].name}\r\n"
            previous = deepcopy(service.state)
            preview = service.import_playlist('my-set.m3u8', content, preview=True)
            self.assertTrue(preview['canImport']); self.assertEqual(service.state, previous)
            self.assertEqual([t['id'] for t in preview['tracks']], [ids[2], ids[0], ids[1]])
            state = service.import_playlist('my-set.m3u8', content)
            self.assertEqual(state['orderedTrackIds'], [ids[2], ids[0], ids[1]])
            self.assertEqual(state['status'], 'selected'); self.assertIsNone(state['model'])
            self.assertFalse(state['activationReady']); self.assertEqual(state['transitions'], [])
            self.assertEqual(state['selectionSource']['kind'], 'mixxx_playlist')
            self.assertEqual(service.queued_jobs, set()); no_call.assert_not_called()
            self.assertFalse((root/'artifacts/live-performance/queue-state-v1.json').exists())
            self.assertEqual(len(service.saved_sets()), 1)
            restored = SetBuilderService(root, metadata_reader=metadata, start_worker=False)
            self.assertEqual(restored.state['orderedTrackIds'], state['orderedTrackIds'])
            self.assertEqual(restored.queued_jobs, set())

    def test_missing_remote_duplicates_and_wrong_absolute_paths_are_atomic(self):
        with TemporaryDirectory() as raw:
            root = Path(raw); service, ids, no_call = self.service(root)
            first = str(service.catalog[ids[0]].path)
            for entry in ('missing.wav', 'https://example.invalid/song.mp3', first,
                          '/different/place/'+Path(first).name, 'file://remote/song.wav'):
                before = deepcopy(service.state)
                content = first+'\n'+entry
                report = service.import_playlist('set.m3u', content, preview=True)
                self.assertFalse(report['canImport']); self.assertEqual(len(report['issues']), 1)
                with self.assertRaisesRegex(ValueError, 'no partial'):
                    service.import_playlist('set.m3u', content)
                self.assertEqual(service.state, before)
            no_call.assert_not_called()

    def test_pending_analysis_is_visible_and_not_implicitly_scheduled(self):
        with TemporaryDirectory() as raw:
            root = Path(raw); service, ids, no_call = self.service(root)
            (root/'artifacts/time-map'/ids[1]/'track-analysis-v1.json').unlink()
            content = '\n'.join(str(service.catalog[t].path) for t in ids[:2])
            report = service.import_playlist('pending.m3u8', content, preview=True)
            self.assertTrue(report['canImport']); self.assertEqual(report['missingAnalysis'], 1)
            selected = service.import_playlist('pending.m3u8', content)
            self.assertEqual(selected['queue'][1]['analysisStatus'], 'pending')
            self.assertEqual(selected['status'], 'selected')
            self.assertEqual(service.queued_jobs, set())
            with self.assertRaisesRegex(ValueError, 'valid local analyses'):
                service.prepare_selected_transitions()
            restored = SetBuilderService(root, metadata_reader=metadata, start_worker=False)
            self.assertEqual(restored.state['orderedTrackIds'], list(ids[:2]))
            self.assertEqual(restored.queued_jobs, set()); no_call.assert_not_called()

    def test_prepared_import_reuses_pairs_and_rebuilds_changed_analysis_keys(self):
        with TemporaryDirectory() as raw:
            root = Path(raw); service, ids, no_call = self.service(root)
            for index, track_id in enumerate(ids):
                data = analysis_payload(track_id, index)
                data['source_filename'] = service.catalog[track_id].path.name
                (service.analysis_root/track_id/'track-analysis-v1.json').write_text(json.dumps(data))
            service.pair_builder = service._build_pair
            content = '\n'.join(str(service.catalog[t].path) for t in ids)
            selected = service.import_playlist('prepared.m3u8', content, prepared=True)
            self.assertEqual(selected['status'], 'ready')
            self.assertTrue(selected['activationReady'])
            self.assertEqual(len(selected['transitions']), 2)
            with patch('pair_analyzer._transition_metrics', side_effect=AssertionError('Expected cached pairs')):
                self.assertTrue(service.import_playlist('prepared.m3u8', content, prepared=True)['activationReady'])
            path = service.analysis_root/ids[0]/'track-analysis-v1.json'
            data=json.loads(path.read_text()); data['analysis_cache_key']='changed-key'; path.write_text(json.dumps(data))
            from pair_analyzer import _transition_metrics
            with patch('pair_analyzer._transition_metrics', wraps=_transition_metrics) as recompute:
                self.assertTrue(service.import_playlist('prepared.m3u8', content, prepared=True)['activationReady'])
                self.assertGreater(recompute.call_count, 0)
            self.assertEqual(service.queued_jobs, set()); no_call.assert_not_called()

    def test_step_two_import_binds_export_for_step_three_without_meta_or_playback(self):
        with TemporaryDirectory() as raw:
            root=Path(raw); service, ids, no_call=self.service(root)
            service.pair_builder=Mock(return_value=(5,None))
            store=object.__new__(LiveStateStore);store._data_root=root;store.workspace=root
            store.set_builder=service;store.action_lock=threading.RLock();store.activation_in_progress=False
            store._executor_running=Mock(return_value=False)
            store.raw_performance=SimpleNamespace(decks={1:SimpleNamespace(play=False),2:SimpleNamespace(play=False)})
            store.queue={'revision':0,'ordered_track_ids':[]};store._append_system_event=Mock()
            store.request_meta_plan=no_call
            content='\n'.join(str(service.catalog[t].path) for t in ids)
            with patch.dict('sys.modules', {'mido':None,'probe':None}):
                status,result=store.request_import_prepared_playlist({'filename':'prior.m3u8','content':content})
            self.assertEqual(status,200);self.assertEqual(result['transitionCount'],2)
            self.assertEqual(store._activation_snapshot()['status'],'playlist_ready')
            self.assertEqual(store.performance_plan_state['status'],'idle')
            self.assertEqual(store.queue,{'revision':0,'ordered_track_ids':[]})
            self.assertEqual(Path(result['playlist']).read_text().splitlines()[1:],content.splitlines())
            self.assertRegex(Path(result['playlist']).name,r'prepared-tonight-set-\d{8}-\d{6}-[0-9a-f]{5}\.m3u8')
            no_call.assert_not_called()

    def test_prepared_import_blocks_missing_analysis_or_empty_pairs_before_changing_selection(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);service,ids,no_call=self.service(root)
            content='\n'.join(str(service.catalog[t].path) for t in ids)
            before=deepcopy(service.state)
            service.pair_builder=Mock(side_effect=[(5,None),(0,None)])
            with self.assertRaisesRegex(ValueError,'No valid transition'):
                service.import_playlist('prior.m3u8',content,prepared=True)
            self.assertEqual(service.state,before)
            (service.analysis_root/ids[0]/'track-analysis-v1.json').unlink()
            with self.assertRaisesRegex(ValueError,'local song analysis'):
                service.import_playlist('prior.m3u8',content,prepared=True)
            self.assertEqual(service.state,before);no_call.assert_not_called()

    def test_confirm_revalidates_library_and_rejects_invalid_input(self):
        with TemporaryDirectory() as raw:
            root = Path(raw); service, ids, _ = self.service(root)
            paths = [service.catalog[t].path for t in ids[:2]]
            content = '\n'.join(map(str, paths))
            self.assertTrue(service.import_playlist('set.m3u8', content, preview=True)['canImport'])
            paths[1].rename(paths[1].with_suffix('.removed'))
            with self.assertRaises(ValueError): service.import_playlist('set.m3u8', content)
            for name, body in [('set.csv', content), ('set.m3u8', ''), ('set.m3u8', None),
                               ('set.m3u8', 'x'*32769), ('set.m3u8', '\0')]:
                with self.assertRaises(ValueError): service.import_playlist(name, body, preview=True)
            self.assertEqual(service.state['orderedTrackIds'], [])


if __name__ == '__main__': unittest.main()

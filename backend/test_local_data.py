from pathlib import Path
from analysis_config import analysis_root
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from live_bridge import LiveStateStore
from local_data import fresh_outputs, initialize
from set_builder import SetBuilderService
from test_set_builder import metadata, write_analyzed_library


class LocalDataTest(unittest.TestCase):
    def test_v3_library_excludes_legacy_songs_and_legacy_selection(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            initialize(root)
            legacy = root / 'local-assets/samples'
            legacy.mkdir()
            (legacy / 'old.wav').write_bytes(b'old song')
            previous = root / 'local-assets/samples-v2'
            previous.mkdir()
            (previous / 'v2.wav').write_bytes(b'previous library song')
            (root / 'local-assets/samples-v3/new.wav').write_bytes(b'new song')
            old = SetBuilderService(root, library_root=legacy,
                                    metadata_reader=metadata, start_worker=False)
            old.state['orderedTrackIds'] = list(old.catalog)
            old._persist()
            current = SetBuilderService(root, metadata_reader=metadata, start_worker=False)
            self.assertEqual([t.path.name for t in current.catalog.values()], ['new.wav'])
            self.assertEqual(current.state['orderedTrackIds'], [])
            self.assertEqual(current.queued_jobs, set())
            self.assertTrue((legacy / 'old.wav').is_file())

    def test_empty_product_does_not_restore_outer_workspace_state(self):
        with TemporaryDirectory() as raw:
            outer = Path(raw)
            write_analyzed_library(outer, count=2)
            runtime = outer / "artifacts/live-performance"
            runtime.mkdir(parents=True)
            (runtime / "queue-state-v1.json").write_text('not product state')
            product = outer / "submission"
            initialize(product)
            store = LiveStateStore(outer, data_root=product)
            deck = lambda: SimpleNamespace(track_id=None, play=False, beat_index=None,
                                            position_seconds=0, sync_mode=0)
            store.update(SimpleNamespace(decks={1: deck(), 2: deck()}, crossfader=0, revision=0), ())
            state, error = store.snapshot()
            self.assertIsNone(error)
            self.assertEqual(state['queue'], [])
            self.assertIsNone(state['plan']['selectedBy'])
            self.assertEqual(state['setBuilder']['catalogCount'], 0)
            self.assertEqual(state['setBuilder']['queue'], [])
            self.assertEqual(state['flowTelemetry']['completedTransitions'], 0)
            self.assertFalse((product/'artifacts/live-performance/queue-state-v1.json').exists())

    def test_matching_outer_analysis_is_not_a_product_cache_hit(self):
        with TemporaryDirectory() as raw:
            outer=Path(raw); write_analyzed_library(outer,count=2)
            product=outer/'submission'; initialize(product)
            for song in (outer/'local-assets/samples-v3').iterdir():
                (product/'local-assets/samples-v3'/song.name).write_bytes(song.read_bytes())
            service=SetBuilderService(outer,data_root=product,metadata_reader=metadata,start_worker=False)
            self.assertEqual(len(service.catalog),2)
            self.assertTrue(all(t.analysis_status=='pending' for t in service.catalog.values()))
            self.assertEqual(service.analysis_root,analysis_root(product).resolve())

    def test_fresh_archives_outputs_and_preserves_songs_and_folder_docs(self):
        with TemporaryDirectory() as raw, patch('local_data.socket.create_connection', side_effect=ConnectionRefusedError):
            root=Path(raw); initialize(root)
            song=root/'local-assets/samples-v3/song.wav'; song.write_bytes(b'private fixture')
            readme=root/'artifacts/README.md'; readme.write_text('instructions')
            cache=root/'artifacts/time-map'; cache.mkdir(); (cache/'example.json').write_text('{}')
            archive=fresh_outputs(root)
            self.assertTrue((archive/'time-map/example.json').is_file())
            self.assertFalse(cache.exists())
            self.assertTrue(song.is_file()); self.assertTrue(readme.is_file())
            self.assertIsNone(fresh_outputs(root))

    def test_fresh_refuses_when_service_is_listening(self):
        with TemporaryDirectory() as raw, patch('local_data.socket.create_connection'):
            with self.assertRaisesRegex(RuntimeError,'Stop the service'):
                fresh_outputs(Path(raw))

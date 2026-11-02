import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from set_builder import SetBuilderService
from test_set_builder import metadata, write_analyzed_library


class SavedSetsTest(unittest.TestCase):
    def service(self, root):
        ids=write_analyzed_library(root,count=3)
        s=SetBuilderService(root,metadata_reader=metadata, prepare_transitions=True,start_worker=False,
                            curator=lambda *args:(ids,'Synthetic selection'),
                            pair_builder=lambda *args:(5,None))
        s.build('Three synthetic house tracks',3)
        self.drain(s)
        return s

    def drain(self,s):
        while not s.jobs.empty():
            job=s.jobs.get_nowait()
            self.assertEqual(job[0],'pair', 'Loading a saved set must not request Meta or neural analysis')
            s._work_pair(job[1],job[2])
            s.queued_jobs.discard(job);s.jobs.task_done()

    def test_completed_set_is_saved_once_and_load_reuses_analyses(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);s=self.service(root)
            saved=s.saved_sets();self.assertEqual(len(saved),1)
            self.assertTrue(saved[0]['available'])
            s._persist();self.assertEqual(len(s.saved_sets()),1)
            active=root/'artifacts/live-performance/queue-state-v1.json'
            active.write_text('{"unchanged":true}')
            s.load_saved_set(saved[0]['id']);self.drain(s)
            self.assertEqual(s.snapshot()['status'],'ready')
            self.assertTrue(all(t['analysisOrigin']=='cache' and t['analysisRun'] is None for t in s.snapshot()['queue']))
            self.assertEqual(active.read_text(),'{"unchanged":true}')
            self.assertEqual(len(s.saved_sets()),1)

    def test_missing_song_changed_analysis_and_path_traversal_are_rejected(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);s=self.service(root);saved=s.saved_sets()[0]
            with self.assertRaisesRegex(ValueError,'Invalid saved'):
                s.load_saved_set('../set-builder-state-v1')
            ident=s.state['orderedTrackIds'][0]
            analysis=s.analysis_root/ident/'track-analysis-v1.json'
            data=json.loads(analysis.read_text());data['analysis_cache_key']='changed'
            analysis.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'Analysis changed'):
                s.load_saved_set(saved['id'])
            song=s.catalog[ident].path;song.rename(song.with_suffix('.removed'))
            with self.assertRaisesRegex(ValueError,'no longer available'):
                s.load_saved_set(saved['id'])

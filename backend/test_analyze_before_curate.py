import json
from pathlib import Path
from analysis_config import analysis_root
from tempfile import TemporaryDirectory
import time
import unittest

from set_builder import SetBuilderService, curate_with_meta, sha256_file
from test_set_builder import metadata, write_music_result


class AnalyzeBeforeCurateTest(unittest.TestCase):
    def library(self, root, count=5):
        folder=root/'local-assets/samples';folder.mkdir(parents=True)
        for i in range(count):
            song=folder/f'track-{i}.wav';song.write_bytes(f'synthetic-{i}'.encode())
            write_music_result(root,sha256_file(song))

    def analyze(self, root, track):
        output=analysis_root(root)/track.id/'track-analysis-v1.json'
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps({'audio_sha256':track.id,'analysis_cache_key':f'test-{track.id}',
            'source_filename':track.path.name,'duration_seconds':180,'rhythm':{'bpm':128},
            'bars':[{'rms_normalized':v,'low_ratio':0.3} for v in [0.1,0.4,0.8]],
            'structure':{'segments':[{'start_seconds':0,'end_seconds':30,'weak_label':'intro'}]}}))

    def wait(self, service):
        deadline=time.monotonic()+3
        while service.jobs.unfinished_tasks and time.monotonic()<deadline: time.sleep(0.005)
        self.assertEqual(service.jobs.unfinished_tasks,0)

    def test_all_five_are_analyzed_before_meta_chooses_two_and_no_pairs_are_built(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);self.library(root);events=[];pairs=[]
            def analyzer(track):
                self.analyze(root,track);events.append(('analysis',track.id))
            def curator(tracks,prompt,count):
                self.assertEqual(len(events),5)
                self.assertEqual(len(tracks),5)
                self.assertTrue(all(t.analysis_status=='ready' and t.analysis_summary['measuredBpm']==128 for t in tracks))
                events.append(('meta',count))
                return tuple(t.id for t in tracks[:count]),'Synthetic measured selection'
            service=SetBuilderService(root,metadata_reader=metadata,analyzer=analyzer,curator=curator,pair_builder=lambda *args:pairs.append(args))
            initial=service.build('two songs',2)
            self.assertEqual(initial['queue'],[])
            self.assertEqual(initial['buildPhase'],'library_analysis')
            self.wait(service)
            state=service.snapshot()
            self.assertEqual(state['status'],'selected')
            self.assertEqual(len(state['library']),5);self.assertEqual(len(state['queue']),2)
            self.assertEqual(events[-1],('meta',2));self.assertEqual(pairs,[])
            self.assertEqual(state['transitions'],[])
            self.assertFalse(state['activationReady']);self.assertFalse(state['preparationReady'])
            self.assertEqual(len(service.saved_sets()),1)
            service.load_saved_set(service.saved_sets()[0]['id'])
            self.assertEqual(service.snapshot()['status'],'selected')
            self.assertEqual(service.queued_jobs,set())

    def test_failure_blocks_meta_even_when_enough_other_tracks_are_ready(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);self.library(root);calls=[]
            def analyzer(track):
                if track.title=='track-4': raise RuntimeError('synthetic analyzer failure')
                self.analyze(root,track)
            service=SetBuilderService(root,metadata_reader=metadata,analyzer=analyzer,curator=lambda *args:calls.append(args))
            service.build('two songs',2);self.wait(service)
            self.assertEqual(calls,[])
            self.assertEqual(service.snapshot()['status'],'error')
            self.assertEqual(service.snapshot()['queue'],[])

    def test_cached_results_are_reused_and_model_payload_contains_measured_summary(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);self.library(root,count=3)
            service=SetBuilderService(root,metadata_reader=metadata,start_worker=False)
            for track in service.catalog.values(): self.analyze(root,track)
            calls=[]
            def transport(system,user):
                request=json.loads(user);calls.append(request)
                self.assertEqual(len(request['catalog']),3)
                self.assertTrue(all(t['analysisSummary']['relativeEnergyByThird']==[0.1,0.4,0.8] for t in request['catalog']))
                self.assertIn('not comparable',system)
                return json.dumps({'ordered_track_ids':[t['id'] for t in request['catalog'][:2]],'reason':'Synthetic'})
            service.curator=lambda tracks,prompt,count:curate_with_meta(tracks,prompt=prompt,count=count,model='synthetic',transport=transport)
            state=service.build('two songs',2)
            self.assertEqual(state['status'],'selected');self.assertEqual(len(calls),1)
            self.assertEqual(service.queued_jobs,set())
            self.assertTrue(all(t['analysisOrigin']=='cache' and t['analysisRun'] is None for t in state['library']))

    def test_new_build_is_rejected_until_pending_analysis_finishes(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);self.library(root,count=2)
            service=SetBuilderService(root,metadata_reader=metadata,start_worker=False)
            service.build('two',2)
            with self.assertRaisesRegex(ValueError,'current build'):
                service.build('different request',2)

    def test_corrupt_cache_requires_analysis_instead_of_early_selection(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);self.library(root,count=2)
            song=next((root/'local-assets/samples').iterdir());ident=sha256_file(song)
            path=analysis_root(root)/ident/'track-analysis-v1.json';path.parent.mkdir(parents=True);path.write_text('{broken')
            service=SetBuilderService(root,metadata_reader=metadata,start_worker=False)
            state=service.build('two',2)
            self.assertEqual(state['buildPhase'],'library_analysis')
            self.assertEqual(len([j for j in service.queued_jobs if j[0]=='track']),2)

    def test_standalone_analysis_never_calls_meta_and_build_reuses_results(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);self.library(root,count=3);runs=[];calls=[];pairs=[]
            def analyzer(track):
                self.analyze(root,track);runs.append(track.id)
            def curator(tracks,prompt,count):
                calls.append(prompt)
                return tuple(t.id for t in tracks[:count]),'Synthetic selection'
            service=SetBuilderService(root,metadata_reader=metadata,analyzer=analyzer,curator=curator,pair_builder=lambda *args:pairs.append(args))
            service.analyze_library();self.wait(service)
            state=service.snapshot()
            self.assertEqual(state['buildPhase'],'library_ready')
            self.assertEqual(state['status'],'idle')
            self.assertTrue(all(t['analysisStatus']=='ready' for t in state['library']))
            self.assertEqual(state['queue'],[]);self.assertEqual(calls,[]);self.assertEqual(pairs,[])
            self.assertEqual(service.saved_sets(),[])
            state=service.build('choose two',2)
            self.assertEqual(state['status'],'selected')
            self.assertEqual(len(runs),3);self.assertEqual(calls,['choose two'])
            selected=state['orderedTrackIds']
            state=service.analyze_library()
            self.assertEqual(state['orderedTrackIds'],selected)
            self.assertEqual(service.queued_jobs,set())
            self.assertEqual(len(runs),3);self.assertEqual(calls,['choose two']);self.assertEqual(pairs,[])

    def test_standalone_analysis_retries_only_failed_tracks(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);self.library(root,count=3);runs=[];calls=[]
            def analyzer(track):
                runs.append(track.title)
                if track.title=='track-1' and runs.count('track-1')==1:
                    raise RuntimeError('synthetic failure')
                self.analyze(root,track)
            service=SetBuilderService(root,metadata_reader=metadata,analyzer=analyzer,curator=lambda *args:calls.append(args))
            service.analyze_library();self.wait(service)
            self.assertEqual(service.snapshot()['status'],'error')
            self.assertEqual(sum(t['analysisStatus']=='failed' for t in service.snapshot()['library']),1)
            service.analyze_library();self.wait(service)
            self.assertEqual(service.snapshot()['status'],'idle')
            self.assertEqual(len(runs),4);self.assertEqual(calls,[])
            self.assertEqual(sum(t['analysisOrigin']=='cache' for t in service.snapshot()['library']),2)

    def test_standalone_analysis_rejects_overlap_and_empty_library(self):
        with TemporaryDirectory() as raw:
            root=Path(raw)
            service=SetBuilderService(root,metadata_reader=metadata,start_worker=False)
            with self.assertRaisesRegex(ValueError,'No songs'):
                service.analyze_library()
            self.library(root,count=1)
            service.analyze_library()
            self.assertEqual(len(service.snapshot()['library']),1)
            with self.assertRaisesRegex(ValueError,'current build or analysis'):
                service.analyze_library()
            with self.assertRaisesRegex(ValueError,'current build'):
                service.build('two',2)

    def test_subset_analyzes_only_requested_songs_and_retains_other_run_evidence(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);self.library(root,count=4);runs=[];calls=[]
            def analyzer(track):
                runs.append(track.id);self.analyze(root,track)
            def curator(tracks,prompt,count):
                calls.append(len(tracks))
                self.assertTrue(all(t.analysis_status=='ready' for t in tracks))
                return tuple(t.id for t in tracks[:count]),'Synthetic'
            service=SetBuilderService(root,metadata_reader=metadata,analyzer=analyzer,curator=curator)
            ids=list(service.catalog)
            service.analyze_library(ids[:2]);self.wait(service)
            state=service.snapshot()
            self.assertEqual(runs,ids[:2]);self.assertEqual(calls,[])
            self.assertEqual(state['analysisTrackIds'],ids[:2])
            self.assertEqual([t['analysisStatus'] for t in state['library']],['ready','ready','pending','pending'])
            first_run=state['library'][0]['analysisRun']
            service.analyze_library(ids[1:3]);self.wait(service)
            self.assertEqual(runs,ids[:3])
            self.assertEqual(service.snapshot()['library'][0]['analysisRun'],first_run)
            self.assertEqual(service.snapshot()['library'][1]['analysisOrigin'],'cache')
            service.build('two',2);self.wait(service)
            self.assertEqual(runs,ids);self.assertEqual(calls,[4])

    def test_invalid_subset_never_schedules_work_or_mutates_state(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);self.library(root,count=2)
            service=SetBuilderService(root,metadata_reader=metadata,start_worker=False)
            ident=next(iter(service.catalog));before=service.snapshot()
            for invalid in [[], 'all', [ident,ident], ['missing'], [ident,None], [{}]]:
                with self.subTest(ids=invalid), self.assertRaises(ValueError):
                    service.analyze_library(invalid)
                self.assertEqual(service.snapshot(),before)
                self.assertEqual(service.queued_jobs,set())

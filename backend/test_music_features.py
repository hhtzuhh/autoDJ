import base64
import json
from pathlib import Path
from analysis_config import analysis_root
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock

from music_features import CACHE_KEY, MODEL, PROMPT, MusicFeatures
from set_builder import SetBuilderService
from test_set_builder import metadata
from live_bridge import LiveStateStore


def response(content='Synthetic genre: uncertain; steady percussion.', finish='stop'):
    return json.dumps({'id':'synthetic-response','model':MODEL,
        'choices':[{'message':{'content':content},'finish_reason':finish}],
        'usage':{'prompt_tokens':20,'completion_tokens':30,'total_tokens':50}})


def prepare(root, count=3, ready=True, transport=None, curator=None, start_worker=True):
    songs=root/'local-assets/samples';songs.mkdir(parents=True)
    for i in range(count): (songs/f'track-{i}.mp3').write_bytes(f'synthetic audio {i}'.encode())
    service=SetBuilderService(root,metadata_reader=metadata,start_worker=False)
    if ready:
        for track in service.catalog.values():
            dest=analysis_root(root)/track.id/'track-analysis-v1.json'
            dest.parent.mkdir(parents=True)
            dest.write_text(json.dumps({'audio_sha256':track.id,'analysis_cache_key':'synthetic',
                'rhythm':{'bpm':128},'bars':[],'structure':{'segments':[]}}))
    return SetBuilderService(root,metadata_reader=metadata,music_transport=transport,
                             curator=curator,start_worker=start_worker)


def wait(service):
    deadline=time.monotonic()+3
    while service.jobs.unfinished_tasks and time.monotonic()<deadline: time.sleep(.005)
    assert service.jobs.unfinished_tasks==0


class MusicFeaturesTest(unittest.TestCase):
    def test_subset_exact_prompt_raw_response_and_curation_provenance(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);requests=[];curations=[]
            def transport(payload):
                requests.append(payload);return response()
            def curator(tracks,prompt,count):
                curations.append(tracks)
                return tuple(t.id for t in tracks[:count]),'Synthetic'
            service=prepare(root,transport=transport,curator=curator)
            ids=list(service.catalog)
            service.describe_music([ids[0]]);wait(service)
            self.assertEqual(len(requests),1);self.assertEqual(curations,[])
            self.assertEqual(service.snapshot()['queue'],[])
            self.assertFalse(service.snapshot()['activationReady'])
            payload=requests[0]
            self.assertEqual(payload['model'],'muse-spark-1.2');self.assertEqual(payload['max_tokens'],4000)
            content=payload['messages'][0]['content']
            self.assertEqual(content[0],{'type':'text','text':PROMPT})
            self.assertEqual(base64.b64decode(content[1]['input_audio']['data']),service.catalog[ids[0]].path.read_bytes())
            result=service.snapshot()['library'][0]['musicFeatures']
            self.assertEqual(result['status'],'ready');self.assertEqual(result['usage']['total_tokens'],50)
            self.assertEqual(service.music_extractor.raw_response(ids[0]),json.loads(response()))
            self.assertEqual([t['musicFeatures']['status'] for t in service.snapshot()['library']],['ready','pending','pending'])
            service.build('two songs',2);wait(service)
            self.assertEqual(len(curations),1)
            self.assertFalse(curations[0][0].music_features['verified'])
            self.assertEqual(curations[0][0].music_features['description'],result['content'])
            self.assertTrue(all(t.music_features for t in curations[0]))
            self.assertEqual(len(requests),3)

    def test_cache_survives_restart_without_another_api_call(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);transport=Mock(return_value=response())
            service=prepare(root,transport=transport);ident=next(iter(service.catalog))
            service.describe_music([ident]);wait(service)
            restored=SetBuilderService(root,metadata_reader=metadata,music_transport=transport)
            self.assertEqual(restored.snapshot()['library'][0]['musicFeatures']['origin'],'cache')
            restored.describe_music([ident]);wait(restored)
            self.assertEqual(transport.call_count,1)

    def test_truncated_and_empty_answers_fail_then_retry(self):
        for raw_answer in [response('Partial','length'), response(''), '{bad json']:
            with self.subTest(answer=raw_answer), TemporaryDirectory() as raw:
                root=Path(raw);transport=Mock(side_effect=[raw_answer,response()])
                service=prepare(root,transport=transport);ident=next(iter(service.catalog))
                service.describe_music([ident]);wait(service)
                self.assertEqual(service.snapshot()['library'][0]['musicFeatures']['status'],'failed')
                self.assertEqual(service.snapshot()['status'],'error')
                service.describe_music([ident]);wait(service)
                self.assertEqual(service.snapshot()['library'][0]['musicFeatures']['status'],'ready')
                self.assertEqual(transport.call_count,2)

    def test_requires_local_analysis_and_rejects_unknown_empty_duplicate_ids(self):
        with TemporaryDirectory() as raw:
            service=prepare(Path(raw),ready=False,start_worker=False);ident=next(iter(service.catalog))
            for invalid in [None,[],['unknown'],[ident,ident],[ident]]:
                with self.subTest(ids=invalid), self.assertRaises(ValueError):service.describe_music(invalid)
            self.assertEqual(service.queued_jobs,set())

    def test_background_request_reports_running_and_blocks_overlapping_builds(self):
        with TemporaryDirectory() as raw:
            entered=threading.Event();release=threading.Event()
            def transport(payload):
                entered.set();release.wait(2);return response()
            service=prepare(Path(raw),transport=transport);ident=next(iter(service.catalog))
            try:
                service.describe_music([ident]);self.assertTrue(entered.wait(1))
                item=service.snapshot()['library'][0]['musicFeatures']
                self.assertEqual(item['status'],'running');self.assertIsNotNone(item['requestStartedAt'])
                with self.assertRaises(ValueError):service.build('two',2)
                with self.assertRaises(ValueError):service.analyze_library([ident])
                with self.assertRaises(ValueError):service.describe_music([ident])
            finally:release.set();wait(service)

    def test_corrupt_stale_and_interrupted_cache_and_changed_source(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);transport=Mock(return_value=response());service=prepare(root,transport=transport)
            track=next(iter(service.catalog.values()));extractor=service.music_extractor
            folder=extractor.folder(track.id);folder.mkdir(parents=True)
            for text in ['{bad','[]',json.dumps({'status':'ready','audioSha256':track.id,'cacheKey':'old','content':'stale'})]:
                (folder/'result.json').write_text(text)
                self.assertEqual(extractor.load(track.id)['status'],'pending')
            extractor.save(track.id,{'status':'running','audioSha256':track.id,'cacheKey':CACHE_KEY})
            self.assertEqual(extractor.load(track.id)['status'],'failed')
            track.path.write_bytes(b'changed')
            result=extractor.extract(track)
            self.assertEqual(result['status'],'failed');transport.assert_not_called()
            with self.assertRaises(ValueError):extractor.raw_response('../outside')

    def test_bridge_rejects_execution_and_passes_selected_ids(self):
        store=object.__new__(LiveStateStore);store.action_lock=threading.Lock()
        store._executor_running=Mock(return_value=True);store.activation_in_progress=False
        store.set_builder=Mock()
        self.assertEqual(store.request_music_features({'trackIds':['a']})[0],409)
        store.set_builder.describe_music.assert_not_called()
        store._executor_running.return_value=False
        store.set_builder.describe_music.return_value={'status':'analyzing'}
        self.assertEqual(store.request_music_features({'trackIds':['a']})[0],202)
        store.set_builder.describe_music.assert_called_once_with(['a'])

    def test_build_finishes_all_local_analysis_before_music_then_selects(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);events=[]
            def transport(payload):
                self.assertEqual(len([x for x in events if x=='local']),3)
                events.append('music');return response()
            def curator(tracks,prompt,count):
                self.assertEqual(events,['local']*3+['music']*3)
                self.assertTrue(all(t.music_features and not t.music_features['verified'] for t in tracks))
                events.append('select')
                return tuple(t.id for t in tracks[:count]),'Synthetic'
            service=prepare(root,ready=False,transport=transport,curator=curator)
            def analyze(track):
                dest=analysis_root(root)/track.id/'track-analysis-v1.json'
                dest.parent.mkdir(parents=True)
                dest.write_text(json.dumps({'audio_sha256':track.id,'analysis_cache_key':'synthetic',
                    'rhythm':{'bpm':128},'bars':[],'structure':{'segments':[]}}))
                events.append('local')
            service.analyzer=analyze
            service.build('two tracks',2);wait(service)
            self.assertEqual(events,['local']*3+['music']*3+['select'])
            self.assertEqual(service.snapshot()['status'],'selected')
            self.assertEqual(service.snapshot()['transitions'],[])
            self.assertFalse(service.snapshot()['activationReady'])

    def test_build_music_failure_blocks_selection_and_retry_only_fills_failed_song(self):
        with TemporaryDirectory() as raw:
            root=Path(raw);curator=Mock(side_effect=lambda tracks,prompt,count:(tuple(t.id for t in tracks[:count]),'Synthetic'))
            transport=Mock(side_effect=[response(),response('partial','length'),response(),response()])
            service=prepare(root,transport=transport,curator=curator)
            service.build('two',2);wait(service)
            self.assertEqual(service.snapshot()['status'],'error');curator.assert_not_called()
            self.assertEqual(service.snapshot()['queue'],[])
            service.build('two',2);wait(service)
            self.assertEqual(transport.call_count,4);self.assertEqual(curator.call_count,1)
            self.assertEqual(service.snapshot()['status'],'selected')
            service.build('two again',2);wait(service)
            self.assertEqual(transport.call_count,4);self.assertEqual(curator.call_count,2)

    def test_build_local_failure_never_calls_music_or_selection(self):
        with TemporaryDirectory() as raw:
            transport=Mock(return_value=response());curator=Mock()
            service=prepare(Path(raw),ready=False,transport=transport,curator=curator)
            service.analyzer=Mock(side_effect=RuntimeError('Synthetic local failure'))
            service.build('two',2);wait(service)
            transport.assert_not_called();curator.assert_not_called()
            self.assertEqual(service.snapshot()['status'],'error')

    def test_selection_gate_rejects_missing_music_even_if_called_directly(self):
        with TemporaryDirectory() as raw:
            curator=Mock();service=prepare(Path(raw),curator=curator,start_worker=False)
            service.build('two',2)
            self.assertEqual(service.snapshot()['buildPhase'],'music_features')
            service._curate_analyzed_catalog(service.state['buildId'])
            curator.assert_not_called()
            self.assertIn('descriptions are incomplete',service.snapshot()['error'])

    def test_build_rejects_unsupported_music_format_before_jobs(self):
        with TemporaryDirectory() as raw:
            service=prepare(Path(raw),start_worker=False)
            track=next(iter(service.catalog.values()))
            track.path.rename(track.path.with_suffix('.flac'))
            with self.assertRaisesRegex(ValueError,'MP3/WAV'):
                service.build('two',2)
            self.assertEqual(service.queued_jobs,set())

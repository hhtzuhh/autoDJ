from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from flow_telemetry import LiveFlowTelemetry
from set_builder import SetBuilderService
from test_set_builder import metadata, write_analyzed_library


class FlowTelemetryTest(unittest.TestCase):
    def supervisor(self, status, plan='plan-1', running=False):
        return SimpleNamespace(manager=SimpleNamespace(envelope=SimpleNamespace(status=status, plan_id=plan)),
                               executor=SimpleNamespace(poll=lambda: None if running else 0))

    def test_counts_only_observed_completed_owned_plans_once(self):
        flow=LiveFlowTelemetry()
        flow.observe(None)
        self.assertEqual(flow.snapshot(None)['completedTransitions'],0)
        flow.observe(self.supervisor('armed', running=True))
        self.assertIsNone(flow.last_execution)
        flow.observe(self.supervisor('executing', running=True))
        self.assertEqual(flow.snapshot(None)['completedTransitions'],0)
        flow.observe(self.supervisor('completed'))
        flow.observe(self.supervisor('completed'))
        self.assertEqual(flow.snapshot(None)['completedTransitions'],1)
        flow.observe(self.supervisor('completed','plan-2'))
        self.assertEqual(flow.snapshot(None)['completedTransitions'],2)
        self.assertEqual(LiveFlowTelemetry().snapshot(None)['completedTransitions'],0)

    def test_takeover_and_snapshot_are_read_only(self):
        flow=LiveFlowTelemetry(); sup=self.supervisor('cancelled')
        flow.observe(sup)
        snapshot=flow.snapshot(sup)
        self.assertFalse(snapshot['executorRunning'])
        self.assertEqual(snapshot['completedTransitions'],0)
        self.assertEqual(snapshot['lastExecution']['status'],'cancelled')
        snapshot['lastExecution']['status']='completed'
        self.assertEqual(flow.last_execution['status'],'cancelled')

    def test_set_builder_distinguishes_existing_and_reused_analysis(self):
        with TemporaryDirectory() as raw:
            root=Path(raw); ids=write_analyzed_library(root,count=3)
            service=SetBuilderService(root,metadata_reader=metadata,start_worker=False,curator=lambda *args:(ids,'Synthetic selection'))
            self.assertTrue(all(v=='available' for v in service.state['analysisOrigins'].values()))
            service.build('three',3)
            self.assertTrue(all(t['analysisOrigin']=='cache' for t in service.snapshot()['queue']))
            self.assertFalse(any(job[0]=='track' for job in service.queued_jobs))

    def test_analysis_jobs_record_completion_failure_and_custom_engine_honestly(self):
        with TemporaryDirectory() as raw:
            root=Path(raw); ids=write_analyzed_library(root,count=3)
            service=SetBuilderService(root,metadata_reader=metadata,start_worker=False,
                                      curator=lambda *args:(ids,'Synthetic selection'), analyzer=lambda track: None)
            service.build('three',3)
            service._work_track(ids[0])
            track=service.snapshot()['queue'][0]
            self.assertEqual(track['analysisOrigin'],'computed')
            self.assertEqual(track['analysisRun']['engine'],'custom')
            self.assertEqual(track['analysisRun']['neuralNet'],'unverified')
            self.assertEqual(track['analysisRun']['status'],'completed')
            self.assertIsNotNone(track['analysisRun']['finishedAt'])
            self.assertGreaterEqual(track['analysisRun']['durationSeconds'],0)
            with patch.object(service,'analyzer',side_effect=RuntimeError('synthetic failure')):
                service._work_track(ids[1])
            failed=service.snapshot()['queue'][1]
            self.assertEqual(failed['analysisStatus'],'failed')
            self.assertEqual(failed['analysisRun']['status'],'failed')
            self.assertNotEqual(failed['analysisRun']['jobId'],track['analysisRun']['jobId'])

    def test_default_analyzer_reports_internal_neural_cache_separately(self):
        with TemporaryDirectory() as raw:
            root=Path(raw); ids=write_analyzed_library(root,count=2)
            service=SetBuilderService(root,metadata_reader=metadata,start_worker=False)
            track=service.catalog[ids[0]]
            with patch('set_builder.subprocess.run'):
                self.assertEqual(service._run_analyzer(track)['neuralNet'],'computed')
                cached=service.analysis_root/track.id/'allin1-raw/source.json'
                cached.parent.mkdir(); cached.write_text('{}')
                self.assertEqual(service._run_analyzer(track)['neuralNet'],'cache')
            with patch('set_builder.subprocess.run',side_effect=RuntimeError('failed')):
                with self.assertRaises(RuntimeError):
                    service._run_analyzer(track)

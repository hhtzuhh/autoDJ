"""Curation budget/recovery regressions; synthetic library and no network."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from meta_planner import MetaConfig, MetaOutputLimitError
from set_builder import SetBuilderService
from test_set_builder import metadata, write_analyzed_library


class CuratorBudgetTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = write_analyzed_library(self.root, count=3)
        self.service = SetBuilderService(self.root, metadata_reader=metadata, start_worker=False)
        self.tracks = tuple(self.service.catalog.values())
        self.response = json.dumps({'ordered_track_ids': list(self.ids), 'reason': 'Measured continuity.'})
        for patcher in [patch.dict(os.environ, {'META_CURATOR_MAX_TOKENS': '4096', 'META_CURATOR_TIMEOUT_SECONDS': '60'}),
                        patch('meta_planner.load_dotenv'),
                        patch('set_builder.MetaConfig.from_environment', return_value=MetaConfig('private-synthetic-key'))]:
            patcher.start(); self.addCleanup(patcher.stop)

    def test_only_truncation_gets_one_larger_budget_retry(self):
        with patch('set_builder.OpenAICompatibleTransport') as transport:
            transport.return_value.side_effect = [MetaOutputLimitError('truncated'), self.response]
            ordered, _ = self.service._meta_curator(self.tracks, 'three tracks', 3)
        self.assertEqual(ordered, self.ids)
        self.assertEqual([c.args[0].max_tokens for c in transport.call_args_list], [4096, 8192])
        self.assertEqual([c.args[0].timeout_seconds for c in transport.call_args_list], [60, 60])
        self.assertEqual(MetaConfig('x').max_tokens, 2048)
        self.assertEqual([c['status'] for c in self.service.state['curatorCalls']], ['truncated', 'completed'])
        self.assertNotIn('private-synthetic-key', json.dumps(self.service.snapshot()))

    def test_second_truncation_and_other_errors_do_not_loop_or_accept_partial_plan(self):
        for error, expected_calls in [(MetaOutputLimitError('truncated'), 2), (TimeoutError('timeout'), 1),
                                      (RuntimeError('HTTP 429'), 1)]:
            with self.subTest(error=type(error).__name__), patch('set_builder.OpenAICompatibleTransport') as transport:
                transport.return_value.side_effect = error
                with self.assertRaises(type(error)):
                    self.service._meta_curator(self.tracks, 'three', 3)
                self.assertEqual(transport.call_count, expected_calls)
        with patch('set_builder.OpenAICompatibleTransport') as transport:
            transport.return_value.return_value = '{"ordered_track_ids":["unknown"],"reason":"bad"}'
            with self.assertRaises(ValueError):
                self.service._meta_curator(self.tracks, 'three', 3)
            self.assertEqual(transport.call_count, 1)

    def failed_service(self):
        s = self.service
        s.curator = lambda *args: (self.ids, 'initial fixture')
        s.build('three', 3)
        while not s.jobs.empty(): s.jobs.get_nowait(); s.jobs.task_done()
        s.queued_jobs.clear()
        for edge in s.state['transitions']:
            edge.update(status='analyzed', candidateCount=5, reason='clean_break_required')
        s.post_curator = lambda *args: (_ for _ in ()).throw(MetaOutputLimitError('synthetic limit'))
        s._work_post_plan()
        return s

    def test_retry_reuses_analysis_and_never_activates_a_failed_plan(self):
        s = self.failed_service()
        before = s.snapshot()
        self.assertEqual(before['postAnalysisPlanning'], 'failed')
        self.assertFalse(before['activationReady'])
        self.assertFalse(before['preparationReady'])
        active = self.root / 'artifacts/live-performance/queue-state-v1.json'
        active.write_text('active queue sentinel')
        s.retry_post_plan()
        self.assertEqual(s.queued_jobs, {('post_plan',)})
        self.assertEqual(s.state['trackStatuses'], before['trackStatuses'])
        self.assertEqual(s.state['orderedTrackIds'], before['orderedTrackIds'])
        self.assertEqual(s.state['queueRevision'], before['queueRevision'])
        with self.assertRaises(ValueError): s.retry_post_plan()
        s.post_curator = lambda *args: (self.ids, 'Recovered measured ordering.')
        s._work_post_plan()
        self.assertEqual(s.state['status'], 'ready')
        self.assertTrue(s.state['activationReady'])
        self.assertEqual(active.read_text(), 'active queue sentinel')

    def test_late_retry_cannot_overwrite_a_newer_queue(self):
        s = self.failed_service()
        s.retry_post_plan()
        def changed(*args):
            s.state['queueRevision'] += 1
            s.state['postAnalysisPlanning'] = 'pending'
            s.state['reason'] = 'New request'
            return self.ids, 'Old response'
        s.post_curator = changed
        s._work_post_plan()
        self.assertEqual(s.state['reason'], 'New request')
        self.assertEqual(s.state['postAnalysisPlanning'], 'pending')

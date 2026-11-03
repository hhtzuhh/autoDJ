"""Synthetic closed-loop memory checks; no camera, model or MIDI access."""
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from performance_memory import PerformanceMemory


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'memory/events-v1.jsonl'
        self.memory = PerformanceMemory(self.path)

    def test_capture_association_uses_old_song_not_response_arrival(self):
        now = time.time()
        for i in range(7):
            self.memory.sample(SimpleNamespace(decks={1: SimpleNamespace(track_id='a', beat_index=40+i, play=True)}),
                               {'plan_id': 'a-b'}, {'status': 'executing'}, now-20+i)
        self.memory.sample(SimpleNamespace(decks={1: SimpleNamespace(track_id='b', beat_index=60, play=True)}),
                           {'plan_id': 'b-c'}, {'status': 'armed'}, now)
        evidence = {'capturedAt': datetime.fromtimestamp(now-20, timezone.utc).isoformat(), 'goal': None, 'source': 'uncertain'}
        row = self.memory.observe(evidence, {'quality': {}, 'regions': []}, now-20)
        self.assertEqual(row['trackIds'], ['a'])
        self.assertEqual(row['data']['playback']['association'], 'capture_time_telemetry')
        self.assertTrue(self.memory.context(['a'])['recentObservations'][0]['data']['fresh'])
        episode = self.memory.context(['a'])['recentEpisodes'][0]
        self.assertEqual([t['id'] for t in episode['tracks']], ['a'])
        self.assertEqual(episode['moments'][0]['planId'], 'a-b')
        self.assertEqual(episode['moments'][0]['transitionStatus'], 'execution_unknown')
        self.assertEqual(self.memory.snapshot()['episodes'][0]['id'], row['id'])
        restored = PerformanceMemory(self.path).context(['a'])
        self.assertEqual(restored['relatedEpisodes'], [])
        self.assertEqual(restored['recentEpisodes'], [])
        self.assertEqual(self.memory.capture_context(now-120)['association'], 'incomplete_telemetry')

    def test_cross_restart_retrieval_retains_failures_and_does_not_claim_execution(self):
        self.memory.record('decision', {'status': 'selected_not_executed'}, ['a'])
        self.memory.record('execution', {'status': 'cancelled'}, ['a'])
        self.memory.record('execution', {'status': 'completed'}, ['unrelated'])
        restored = PerformanceMemory(self.path)
        context = restored.context(['a'])
        self.assertEqual(context['recentObservations'], [])
        self.assertEqual(context['relatedHistory'], [])
        self.assertNotEqual(restored.session, self.memory.session)
        snapshot = restored.snapshot()
        self.assertEqual(snapshot['sessionEvents'], 0)
        self.assertEqual(snapshot['historicalEvents'], 3)
        self.assertEqual([e['data']['status'] for e in snapshot['recent']],
                         ['selected_not_executed', 'cancelled', 'completed'])

    def test_followup_links_only_execution_that_precedes_capture(self):
        now = time.time()
        actual = self.memory.record('execution', {'status': 'completed'}, ['a'], now-40)
        self.memory.record('decision', {'status': 'selected_not_executed'}, ['b'], now-30)
        self.memory.record('execution', {'status': 'executing'}, ['b'], now-5)
        row = self.memory.observe({'capturedAt': datetime.fromtimestamp(now-20, timezone.utc).isoformat()},
                                  {'quality': {}, 'regions': []}, now-20)
        self.assertEqual(row['data']['afterExecutionEventId'], actual['id'])

    def test_context_contains_only_four_fresh_current_set_observations(self):
        now = time.time()
        self.memory.begin_run('set-000001')
        self.memory.observe({'capturedAt': datetime.fromtimestamp(now-10, timezone.utc).isoformat()}, {'quality': {}, 'regions': []}, now-10)
        self.memory.begin_run('set-000002')
        rows = []
        for age in (121, 60, 50, 40, 30, 20, -10):
            rows.append(self.memory.observe({'capturedAt': datetime.fromtimestamp(now-age, timezone.utc).isoformat()}, {'quality': {}, 'regions': []}, now-age))
        context = self.memory.context(['a'])
        self.assertEqual([row['id'] for row in context['recentObservations']], [row['id'] for row in rows[2:6]])
        self.assertEqual(context['relatedHistory'], [])
        self.assertEqual(self.memory.context(['a'], active=False)['recentEpisodes'], [])
        self.assertEqual(len(self.memory.events), 8)

    def test_decision_history_coalesces_execution_without_losing_previous_decisions(self):
        first = {'planId': 'first', 'observationId': 'o1', 'before': ['a', 'b'], 'after': ['b', 'a']}
        second = {'planId': 'second', 'observationId': 'o2'}
        self.memory.record('decision', {'status': 'staged', 'decision': first}, at=10)
        self.memory.record('decision', {'status': 'queue_committed', 'decision': first}, at=11)
        self.memory.record('decision', {'status': 'staged', 'decision': second}, at=12)
        self.memory.record('execution', {'status': 'completed', 'decision': first}, at=13)
        self.memory.record('decision', {'status': 'discarded', 'observationId': 'o2'}, at=14)
        rows = PerformanceMemory(self.path).crowd_decisions()
        self.assertEqual([r['planId'] for r in rows], ['second', 'first'])
        self.assertEqual([r['historyStatus'] for r in rows], ['discarded', 'completed'])
        self.assertEqual(rows[1]['recordedAt'], 10)
        self.assertEqual(rows[1]['before'], ['a', 'b'])

    def test_disk_failure_keeps_ram_and_malformed_tail_does_not_destroy_history(self):
        with patch.object(Path, 'open', side_effect=OSError('synthetic full disk')):
            self.memory.record('execution', {'status': 'invalidated'}, ['a'])
        self.assertEqual(self.memory.snapshot()['sessionEvents'], 1)
        self.assertIn('RAM', self.memory.snapshot()['warning'])
        self.memory.record('execution', {'status': 'cancelled'}, ['a'])
        with self.path.open('a') as out:
            out.write('{partial')
        restored = PerformanceMemory(self.path)
        self.assertEqual(len(restored.snapshot()['history']), 1)
        restored.record('execution', {'status': 'invalidated'}, ['a'])
        self.assertEqual(len(PerformanceMemory(self.path).snapshot()['history']), 2)


if __name__ == '__main__':
    unittest.main()

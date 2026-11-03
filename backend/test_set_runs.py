"""Offline checks for Start identities, persistence and asynchronous isolation."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from set_runs import SetRuns
from performance_memory import PerformanceMemory


class SetRunTest(unittest.TestCase):
    def test_numbers_survive_restart_failed_start_and_incomplete_directory(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            runs = SetRuns(root)
            plan = {'next_executable_plan': {'plan_id': 'same-plan'}}
            queue = {'ordered_track_ids': ['a', 'b']}
            self.assertEqual(runs.start(plan, queue)['id'], 'set-000001')
            runs.update('start_failed')
            (root / 'set-000002').mkdir()  # interrupted allocation is never reused
            restored = SetRuns(root)
            self.assertEqual(restored.current['status'], 'start_failed')
            self.assertEqual(restored.start(plan, queue)['number'], 3)
            saved = json.loads((root / 'set-000003/reviewed-plan.json').read_text())
            self.assertEqual(saved['next_executable_plan']['set_run_id'], 'set-000003')
            self.assertNotIn('set_run_id', plan)  # reviewed value is not mutated

    def test_new_run_keeps_archive_without_retrieving_earlier_sets(self):
        with TemporaryDirectory() as raw:
            memory = PerformanceMemory(Path(raw) / 'events.jsonl')
            memory.begin_run('set-000001')
            memory.record('execution', {'status': 'completed'}, ['a'])
            memory.begin_run('set-000002')
            # A late execution writer carries the old explicit identity.
            memory.record('execution', {'setRunId': 'set-000001', 'status': 'cancelled'}, ['a'])
            memory.record('execution', {'status': 'executing'}, ['b'])
            self.assertEqual(len(memory.snapshot()['recent']), 1)
            context = memory.context(['a'])
            self.assertEqual(context['relatedHistory'], [])
            self.assertEqual(context['recentDecisionsAndExecution'], [])
            self.assertEqual(len(memory.snapshot()['history']), 3)

    def test_rolling_decision_execution_is_matched_to_run_request_and_first_pair(self):
        with TemporaryDirectory() as raw:
            memory = PerformanceMemory(Path(raw) / 'events.jsonl')
            for run in ('set-000001', 'set-000002'):
                memory.begin_run(run)
                memory.record('decision', {'status': 'selected_not_executed', 'decision': {
                    'planId': 'same', 'origin': 'rolling', 'requestId': 'r', 'sourceTrack': 'a', 'targetTrack': 'b'}})
            memory.record('execution', {'setRunId': 'set-000001', 'requestId': 'r', 'status': 'completed'}, ['a', 'b'])
            memory.record('execution', {'requestId': 'r', 'status': 'completed'}, ['b', 'c'])
            rows = {r['setRunId']: r for r in memory.crowd_decisions()}
            self.assertEqual(rows['set-000001']['executionStatus'], 'completed')
            self.assertNotIn('executionStatus', rows['set-000002'])

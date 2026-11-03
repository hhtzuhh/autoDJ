import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from planning_history import PlanningHistory


class PlanningHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = PlanningHistory(self.root)

    def event(self, kind, status, request='meta-1', at=1, run='set-000001'):
        return {'kind': kind, 'at': at, 'sessionId': 'bridge', 'setRunId': run,
                'data': {'requestId': request, 'status': status}}

    def artifacts(self):
        directory = self.root / 'meta-1'
        directory.mkdir()
        (directory / 'set-run.json').write_text(json.dumps({'scope': 'crowd_adaptive'}))
        (directory / 'request.json').write_text(json.dumps({'model': 'synthetic', 'messages': [
            {'role': 'user', 'content': json.dumps({'crowd_context': {'goal': 'build', 'jobId': 'observation'}})}]}))
        (directory / 'response.json').write_text(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'reason': 'Synthetic build', 'transition_actions': [[{'function': 'crossfade'}]], 'in_track_actions': []})}}]}))

    def test_older_discarded_attempt_recovers_saved_response_without_claiming_execution(self):
        self.artifacts()
        events = [self.event('planning_failed', 'discarded')]
        row = self.history.attempts(events)[0]
        self.assertEqual(row['goal'], 'build')
        self.assertEqual(row['observationId'], 'observation')
        self.assertEqual(row['status'], 'discarded')
        self.assertEqual(row['inTrackCount'], 0)
        self.assertEqual(row['transitionFunctions'], [['crossfade']])
        self.assertTrue(row['responseReceived'])
        self.assertNotIn('executionStatus', row)

    def test_old_initial_plan_uses_saved_scope_instead_of_guessing_a_rolling_trigger(self):
        self.artifacts()
        (self.root / 'meta-1/set-run.json').write_text(json.dumps({'scope': 'current_plan'}))
        source = [{'origin': 'rolling', 'requestId': 'meta-1'}]
        result = self.history.decorate_decisions(source)
        self.assertEqual(result[0]['planningScope'], 'current_plan')
        self.assertNotIn('planningScope', source[0])

    def test_timeout_is_visible_without_inventing_model_response(self):
        row = self.history.attempts([self.event('planning_failed', 'timeout')])[0]
        self.assertFalse(row['responseReceived'])
        self.assertNotIn('inTrackCount', row)

    def test_attempt_updates_coalesce_and_accepted_decisions_are_not_duplicated(self):
        events = [self.event('planning_attempt', 'calling'), self.event('planning_attempt', 'succeeded', at=2)]
        self.assertEqual(len(self.history.attempts(events)), 1)
        events.append(self.event('decision', 'selected_not_executed', at=3))
        self.assertEqual(self.history.attempts(events), [])
        events.append(self.event('planning_failed', 'discarded', request='meta-2', at=4))
        self.assertEqual([r['id'] for r in self.history.attempts(events)], ['meta-2'])

    def test_run_isolation_and_partial_response_file_can_be_retried(self):
        self.artifacts()
        path = self.root / 'meta-1/response.json'
        saved = path.read_text()
        path.write_text('{partial')
        events = [self.event('planning_failed', 'failed'), self.event('planning_attempt', 'calling', run='set-000002')]
        self.assertFalse(self.history.attempts(events)[0]['responseReceived'])
        path.write_text(saved)
        rows = self.history.attempts(events)
        self.assertEqual({r['setRunId'] for r in rows}, {'set-000001', 'set-000002'})
        self.assertTrue(all(r['responseReceived'] for r in rows))

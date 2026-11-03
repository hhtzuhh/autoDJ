from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from performance_episodes import PerformanceEpisodes, plan_context


def iso(at): return datetime.fromtimestamp(at, timezone.utc).isoformat()


class EpisodeTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'time-map/runs'
        self.directory = self.root / 'synthetic'
        self.directory.mkdir(parents=True)
        self.plan = {'plan_id': 'p', 'set_run_id': 'set-000001',
            'deck_a': {'deck': 1, 'audio_sha256': 'a', 'transition_start_beat_index': 200},
            'deck_b': {'deck': 2, 'audio_sha256': 'b', 'cue_beat_index': 64},
            'mix': {'technique': 'crossfade', 'bars': 4}, 'in_track_actions': []}
        self.builder = PerformanceEpisodes(self.root)

    def save(self, events):
        (self.directory / 'resolved-plan.json').write_text(json.dumps({'plan': self.plan}))
        (self.directory / 'transition-events.jsonl').write_text('\n'.join(json.dumps({
            'plan_id': 'p', 'set_run_id': 'set-000001', 'timestamp': iso(at), 'event': name, **extra})
            for at, name, extra in events))

    def observation(self):
        return {'id': 'memory', 'setRunId': 'set-000001', 'kind': 'observation', 'data': {
            'capturedAt': iso(100), 'durationSeconds': 6, 'summary': 'Arms overhead.', 'source': 'gesture', 'goal': 'build',
            'playback': {'association': 'capture_time_telemetry', 'samples': [{'at': at,
                'setRunId': 'set-000001', 'planId': 'p', 'executionStatus': 'executing',
                'decks': [{'deck': 1, 'trackId': 'a', 'beat': 140, 'playing': True},
                          {'deck': 2, 'trackId': 'b', 'beat': 64, 'playing': False}]} for at in (100, 103, 106)]}}}

    def test_waiting_executor_is_not_handoff_and_future_events_do_not_leak_into_clip(self):
        self.save([(90, 'run_started', {}), (110, 'routine_handoff_started', {}), (120, 'run_taken_over', {})])
        row = self.builder.build([self.observation()])[0]
        self.assertEqual([t['id'] for t in row['tracks']], ['a'])
        self.assertEqual(row['moments'][0]['transitionStatus'], 'waiting_for_handoff')
        self.assertEqual(row['moments'][0]['inTrackActions'], [])
        self.assertEqual(row['crowd']['signalGoal'], 'build')

    def test_action_spanning_clip_and_takeover_remains_an_observed_action(self):
        action = {'function': 'bass_pulse', 'track_id': 'a', 'start_beat': 136, 'duration_beats': 16, 'depth': .2}
        self.plan['in_track_actions'] = [action]
        self.save([(90, 'run_started', {}), (99, 'in_track_action_started', {'action': action}),
                   (104, 'run_taken_over', {}), (110, 'in_track_action_completed', {'action': action})])
        moment = self.builder.build([self.observation()])[0]['moments'][0]
        self.assertEqual(moment['inTrackActiveDuringClip'], [action])
        self.assertEqual(moment['inTrackCompletedDuringClip'], [])
        self.assertEqual(moment['transitionStatus'], 'interrupted_during_clip')

    def test_both_playing_tracks_and_a_confirmed_handoff_are_preserved(self):
        self.save([(90, 'run_started', {}), (102, 'routine_handoff_started', {})])
        observation = self.observation()
        observation['data']['playback']['samples'][-1]['decks'][1]['playing'] = True
        row = self.builder.build([observation])[0]
        self.assertEqual([t['id'] for t in row['tracks']], ['a', 'b'])
        self.assertEqual(row['moments'][0]['transitionStatus'], 'handoff_started_during_clip')

    def test_snapshot_plan_without_journal_stays_planned_not_executed(self):
        observation = self.observation()
        for sample in observation['data']['playback']['samples']:
            sample['planContext'] = plan_context(self.plan)
        row = self.builder.build([observation])[0]
        self.assertEqual(row['moments'][0]['transitionStatus'], 'execution_unknown')
        self.assertFalse(row['moments'][0]['executionEvidence'])

    def test_action_confirmation_requires_matching_run_plan_and_executor_event(self):
        action = {'function': 'bass_pulse'}
        self.save([(90, 'run_started', {}), (99, 'in_track_action_started', {'action': action}),
                   (100, 'in_track_action_completed', {'action': action, 'set_run_id': 'set-000002'}),
                   (101, 'in_track_action_completed', {'action': action, 'plan_id': 'unrelated'})])
        self.assertEqual(self.builder.action_execution('p', 'set-000001'), {'started': ['bass_pulse'], 'completed': []})
        self.assertEqual(self.builder.action_execution('p', 'set-000002'), {'started': [], 'completed': []})
        self.plan['plan_id'] = 'p-dashboard-123'
        self.save([(102, 'in_track_action_completed', {'action': action, 'plan_id': 'p-dashboard-123'})])
        self.assertEqual(self.builder.action_execution('p', 'set-000001')['completed'], ['bass_pulse'])
        self.assertEqual(self.builder.action_execution('prefix', 'set-000001')['completed'], [])

    def test_missing_capture_samples_and_other_set_cannot_be_backfilled_from_current_song(self):
        self.save([(90, 'run_started', {})])
        observation = self.observation()
        for sample in observation['data']['playback']['samples']:
            sample['setRunId'] = 'set-000002'
        row = self.builder.build([observation])[0]
        self.assertEqual(row['tracks'], [])
        self.assertEqual(row['moments'], [])
        self.assertEqual(row['playbackState'], 'unknown')

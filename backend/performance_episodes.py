"""Capture-time music / audience episodes for display and planner context.

Executor journals confirm actions; lifecycle 'executing' alone does not.
No response-arrival state or events after the clip are used as capture evidence.
"""
from copy import deepcopy
from datetime import datetime
import json
import re
import threading
import time


def seconds(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def plan_context(plan):
    source, target, mix = (plan.get(k, {}) for k in ('deck_a', 'deck_b', 'mix'))
    return {'planId': plan.get('plan_id'), 'sourceTrack': source.get('audio_sha256'),
        'targetTrack': target.get('audio_sha256'), 'sourceDeck': source.get('deck'),
        'technique': mix.get('technique', plan.get('transition_mode')),
        'sourceExitBeat': source.get('transition_start_beat_index'),
        'targetEntryBeat': target.get('cue_beat_index'), 'bars': mix.get('bars'),
        'commonBpm': plan.get('tempo_routine', {}).get('common_bpm'),
        'transitionActions': deepcopy(plan.get('performance_actions')),
        'inTrackActions': deepcopy(plan.get('in_track_actions'))}


class PerformanceEpisodes:
    def __init__(self, runs_root):
        self.root = runs_root
        self.files = {}
        self.lock = threading.RLock()

    def _read(self, path, journal=False):
        try:
            stamp = (path.stat().st_mtime_ns, path.stat().st_size)
            cached = self.files.get(path)
            if cached and cached[0] == stamp:
                return cached[1]
            if journal:
                value = []
                for line in path.read_text().splitlines():
                    try:
                        row = json.loads(line)
                        row['_at'] = seconds(row['timestamp'])
                        value.append(row)
                    except (ValueError, KeyError, TypeError):
                        continue
            else:
                value = json.loads(path.read_text())
                if path.name == 'resolved-plan.json':
                    plan = value['plan']
                    value = {'context': plan_context(plan), 'setRunId': plan.get('set_run_id')}
                elif path.name == 'track-analysis-v1.json':
                    value = value.get('source_filename', '')
            self.files[path] = (stamp, value)
            return value
        except (OSError, ValueError, KeyError, TypeError):
            return [] if journal else {}

    def _title(self, track):
        if not isinstance(track, str) or not re.fullmatch('[a-f0-9]{64}', track):
            return None
        name = self._read(self.root.parent / track / 'track-analysis-v1.json')
        if not isinstance(name, str) or not name:
            return None
        name = re.sub(r'^\d{4}-\d{2}-\d{2}_-_', '', name.rsplit('.', 1)[0]).replace('_', ' ')
        return re.split(r'\s+-\s+(?:www\.)?FesliyanStudios\.com', name, flags=re.I)[0].strip()

    def build(self, observations):
        with self.lock:
            index = {}
            for path in self.root.glob('*/resolved-plan.json'):
                saved = self._read(path)
                if saved:
                    index[(saved['setRunId'], saved['context']['planId'])] = (saved['context'], path.parent)
            result = []
            for event in observations:
                data = event['data']
                try:
                    start = seconds(data['capturedAt'])
                except (ValueError, KeyError, TypeError):
                    continue
                duration = data.get('durationSeconds', 6)
                end = start + duration
                playback = data.get('playback', {})
                samples = [s for s in playback.get('samples', []) if start <= s['at'] <= end
                           and s.get('setRunId') == event.get('setRunId')]
                tracks, moments = {}, {}
                for sample in samples:
                    for deck in sample['decks']:
                        if not deck.get('playing') or not deck.get('trackId'):
                            continue
                        ident = deck['trackId']
                        row = tracks.setdefault(ident, {'id': ident, 'title': self._title(ident),
                            'deck': deck['deck'], 'firstBeat': deck.get('beat')})
                        row['lastBeat'] = deck.get('beat')
                    plan_id = sample.get('planId')
                    if not plan_id or plan_id in moments:
                        continue
                    saved = index.get((event.get('setRunId'), plan_id))
                    context = sample.get('planContext')
                    if not context or context.get('planId') != plan_id:
                        context = saved[0] if saved else None
                    if not context:
                        continue
                    journal = self._read(saved[1] / 'transition-events.jsonl', journal=True) if saved else []
                    journal = [e for e in journal if e['_at'] <= end and e.get('plan_id', plan_id) == plan_id
                               and e.get('set_run_id') == event.get('setRunId')]
                    moment = deepcopy(context)
                    moment['sourceTitle'] = self._title(moment['sourceTrack'])
                    moment['targetTitle'] = self._title(moment['targetTrack'])
                    moment.update(self._execution(journal, start, end))
                    moment['lifecycleStatus'] = sample.get('executionStatus')
                    moments[plan_id] = moment
                region = next(iter(data.get('regions', [])), {})
                crowd = {'summary': data.get('summary'), 'visibility': data.get('quality', {}).get('visibility'),
                    'cameraMotion': data.get('quality', {}).get('camera_motion'),
                    'signalSource': data.get('source'), 'signalGoal': data.get('goal')}
                for key in ('movement_spread', 'movement_amplitude', 'movement_continuity', 'within_clip_trend'):
                    crowd[key] = region.get(key, {}).get('value', 'unknown')
                result.append({'id': event['id'], 'setRunId': event.get('setRunId'), 'jobId': data.get('jobId'),
                    'capturedAt': data['capturedAt'], 'durationSeconds': duration,
                    'ageSeconds': round(max(0, time.time() - start), 1), 'fresh': 0 <= time.time() - start <= 120,
                    'association': playback.get('association', 'incomplete_telemetry'),
                    'playbackState': 'playing' if tracks else 'stopped' if samples else 'unknown',
                    'tracks': list(tracks.values()), 'moments': list(moments.values()), 'crowd': crowd,
                    'use': 'Compare visible movement during similar songs or confirmed actions in later windows. Gesture requests are operator instructions, not musical preference. Missing action evidence stays unknown; temporal association does not establish enjoyment or causality.'})
            return sorted(result, key=lambda e: e['capturedAt'], reverse=True)

    def action_execution(self, plan_id, run_id):
        """First outgoing song only: completion must come from executor events."""
        started, completed = [], []
        with self.lock:
            for path in self.root.glob('*/resolved-plan.json'):
                saved = self._read(path)
                if not saved or saved['setRunId'] != run_id:
                    continue
                executed_id = saved['context']['planId'] or ''
                if executed_id != plan_id and not executed_id.startswith(plan_id + '-dashboard-'):
                    continue
                for event in self._read(path.parent / 'transition-events.jsonl', journal=True):
                    if event.get('set_run_id') != run_id or event.get('plan_id') != executed_id:
                        continue
                    if event.get('event') == 'in_track_action_started':
                        started.append(event.get('action', {}).get('function', 'unknown'))
                    elif event.get('event') == 'in_track_action_completed':
                        completed.append(event.get('action', {}).get('function', 'unknown'))
        return {'started': started, 'completed': completed}

    @staticmethod
    def _execution(events, start, end):
        transition = 'execution_unknown'
        active, during, completed = {}, {}, []
        key = lambda a: json.dumps(a, sort_keys=True)
        for event in sorted(events, key=lambda e: e['_at']):
            name, at = event['event'], event['_at']
            if name == 'run_started':
                transition = 'waiting_for_handoff'
            elif name in ('routine_handoff_started', 'deck_b_started'):
                transition = 'handoff_started_during_clip' if at >= start else 'handoff_in_progress'
            elif name in ('routine_completed', 'tail_completed', 'clean_break_handoff'):
                transition = 'completed_during_clip' if at >= start else 'completed_before_clip'
            elif name in ('run_taken_over', 'run_cancelled', 'run_taken_over_before_start'):
                transition = 'interrupted_during_clip' if at >= start else 'interrupted_before_clip'
                if at >= start:
                    during.update(active)
                active.clear()
            elif name == 'in_track_action_started':
                action = event.get('action', {})
                active[key(action)] = action
            elif name == 'in_track_action_completed':
                action = event.get('action', {})
                if at >= start:
                    completed.append(action)
                    during[key(action)] = action
                active.pop(key(action), None)
            if at >= start:
                during.update(active)
        during.update(active)
        return {'transitionStatus': transition, 'executionEvidence': bool(events),
                'inTrackActiveDuringClip': list(during.values()), 'inTrackCompletedDuringClip': completed}

"""Local, inspectable episodic memory. No embeddings, identities or preference inference.

Observation timestamps refer to capture, not inference completion. Playback samples
are telemetry, while selected actions remain plans until execution is confirmed.
"""
from collections import deque
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import threading
import time
from uuid import uuid4
from performance_episodes import PerformanceEpisodes, plan_context


class PerformanceMemory:
    def __init__(self, path: Path):
        self.path = path
        self.episodes = PerformanceEpisodes(path.parent.parent.parent / "time-map/runs")
        self.session = str(uuid4())
        self.run_id = None
        self.events = deque(maxlen=512)
        self.playback = deque(maxlen=240)
        self.lock = threading.RLock()
        self.warning = None
        try:
            # Bound startup cost even after many performances; ignore partial lines.
            with path.open('rb') as source:
                source.seek(0, 2)
                size = source.tell()
                source.seek(max(0, size - 2_000_000))
                if size > 2_000_000:
                    source.readline()
                for line in source:
                    try:
                        row = json.loads(line)
                        if (row.get('version') == 1 and isinstance(row.get('id'), str)
                            and isinstance(row.get('sessionId'), str) and isinstance(row.get('kind'), str)
                            and isinstance(row.get('trackIds'), list) and isinstance(row.get('data'), dict)
                            and isinstance(row.get('at'), (int, float))):
                            self.events.append(row)
                    except (ValueError, AttributeError):
                        continue
        except FileNotFoundError:
            pass
        except OSError:
            self.warning = 'Saved memory could not be read; current-session memory remains available.'

    def sample(self, performance, plan, envelope, now=None):
        now = time.time() if now is None else now
        # The MIDI observer must never wait behind journal I/O or a prompt copy.
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.playback and now - self.playback[-1]['at'] < 0.75:
                return
            self.playback.append({'at': now, 'planId': (envelope or {}).get('plan_id', plan.get('plan_id')),
                'setRunId': self.run_id, 'crossfader': getattr(performance, 'crossfader', None),
                'planContext': plan_context(plan),
                'executionStatus': (envelope or {}).get('status', 'unknown'),
                'decks': [{'deck': deck, 'trackId': getattr(state, 'track_id', None),
                    'beat': getattr(state, 'beat_index', None), 'playing': getattr(state, 'play', None),
                    'volume': getattr(state, 'volume', None), 'lowEq': getattr(state, 'eq_low', None)}
                    for deck, state in performance.decks.items()]})
        finally:
            self.lock.release()

    def capture_context(self, captured, duration=6):
        with self.lock:
            samples = [s for s in self.playback if captured - 1 <= s['at'] <= captured + duration]
            covered = bool(samples and samples[0]['at'] <= captured + 1.5
                           and samples[-1]['at'] >= captured + duration - 1.5)
            # Compact snapshots still retain song changes during the clip.
            return {'association': 'capture_time_telemetry' if covered else 'incomplete_telemetry',
                    'samples': deepcopy(samples),
                    'note': 'Observed playback state, not confirmation of every planned action.'}

    def record(self, kind, payload, track_ids=(), at=None):
        with self.lock:
            row = {'version': 1, 'id': str(uuid4()), 'sessionId': self.session,
                   'setRunId': payload.get('setRunId', self.run_id),
                   'at': time.time() if at is None else at, 'kind': kind, 'trackIds': list(dict.fromkeys(track_ids)),
                   'data': deepcopy(payload)}
            self.events.append(row)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open('ab+') as output:
                    output.seek(0, 2)
                    if output.tell():
                        output.seek(-1, 2)
                        if output.read(1) != b'\n':
                            output.write(b'\n')  # isolate an interrupted previous append
                    output.write((json.dumps(row, ensure_ascii=False) + '\n').encode())
            except OSError:
                self.warning = 'Memory persistence failed; this session remains in RAM only.'
            return row

    def observe(self, evidence, observation, captured):
        context = self.capture_context(captured, evidence.get('durationSeconds', 6))
        tracks = [d['trackId'] for s in context['samples'] for d in s['decks']
                  if d['playing'] and d['trackId']]
        with self.lock:
            prior = [e for e in self.events if self._is_current(e)
                     and e['kind'] == 'execution' and e['at'] <= captured
                     and e['data'].get('status') in ('executing', 'completed')]
            after = prior[-1]['id'] if prior else None
        return self.record('observation', {**evidence,
            'quality': observation['quality'], 'regions': observation['regions'],
            'playback': context, 'afterExecutionEventId': after,
            'interpretation': 'Temporal association only; movement is not enjoyment or causal evidence.'}, tracks)

    def context(self, track_ids, *, active=True):
        """Small current-set context; archive retrieval is intentionally disabled."""
        now = time.time()
        with self.lock:
            observations = []
            if active:
                for event in self.events:
                    if not self._is_current(event) or event['kind'] != 'observation':
                        continue
                    try:
                        captured = datetime.fromisoformat(event['data']['capturedAt'].replace('Z', '+00:00')).timestamp()
                    except (KeyError, ValueError, TypeError):
                        continue
                    if 0 <= now - captured <= 120:
                        observations.append(deepcopy(event))
            observations = observations[-4:]
            session, run_id = self.session, self.run_id
        episodes = self.episodes.build(observations)
        # Lightweight IDs/summaries retain compatibility with saved plan viewers.
        # The planner receives capture-time details once, in recentEpisodes.
        references = []
        for event in observations:
            data = event['data']
            references.append({**{k: event[k] for k in ('id', 'sessionId', 'setRunId', 'at', 'kind', 'trackIds')},
                'data': {k: data.get(k) for k in ('jobId', 'capturedAt', 'summary')} | {'fresh': True}})
        return {'sessionId': session, 'setRunId': run_id, 'scope': 'current_set_only',
                'recentObservations': references, 'recentEpisodes': episodes,
                'recentDecisionsAndExecution': [], 'relatedHistory': [], 'relatedEpisodes': [],
                'policy': 'Use only these latest four fresh capture-time music/audience episodes from this set. '
                    'Earlier sets are archived for review, not supplied as preferences. Associate visible movement '
                    'with the playing song and confirmed actions; never infer enjoyment or causality. '
                    'Gesture requests are explicit operator instructions, not inferred preferences. '
                    'Selected actions are not execution evidence. Unknown stays unknown. '
                    'Explain relevant evidence IDs in English.'}

    def crowd_decisions(self):
        """One row per retained webcam decision, updated by its execution audit."""
        with self.lock:
            rows = {}
            observations = {(e.get('setRunId'), e['data'].get('jobId')): e['data'] for e in self.events if e['kind'] == 'observation'}
            for event in sorted(self.events, key=lambda e: e['at']):
                data = event['data']
                decision = data.get('decision')
                if isinstance(decision, dict) and decision.get('planId'):
                    key = (event.get('setRunId'), decision['planId'])
                    previous = rows.get(key, {})
                    rows[key] = {**previous, **deepcopy(decision),
                        'sessionId': event['sessionId'], 'setRunId': event.get('setRunId'),
                        'recordedAt': previous.get('recordedAt', event['at']),
                        'updatedAt': event['at'], 'historyStatus': data.get('status', 'unknown')}
                elif event['kind'] == 'execution' and data.get('requestId'):
                    for row in rows.values():
                        if (row.get('requestId') == data['requestId']
                            and row.get('setRunId') == event.get('setRunId')
                            and event['trackIds'] == [row.get('sourceTrack'), row.get('targetTrack')]):
                            row.update(historyStatus=data['status'], executionStatus=data['status'], updatedAt=event['at'])
                elif data.get('status') == 'discarded' and data.get('observationId'):
                    for row in rows.values():
                        if row.get('observationId') == data['observationId'] and row.get('setRunId') == event.get('setRunId'):
                            row.update(historyStatus='discarded', updatedAt=event['at'])
            for row in rows.values():
                if row.get('origin') != 'rolling':
                    evidence = observations.get((row.get('setRunId'), row.get('observationId')), {})
                    row.setdefault('triggerGoal', evidence.get('goal'))
                    row.setdefault('triggerSource', evidence.get('source'))
            return sorted(rows.values(), key=lambda r: r['recordedAt'], reverse=True)

    def _is_current(self, row):
        return row.get('setRunId') == self.run_id if self.run_id else (not row.get('setRunId') and row['sessionId'] == self.session)

    def begin_run(self, run_id):
        with self.lock:
            self.run_id = run_id
            self.playback.clear()

    def snapshot(self):
        with self.lock:
            current = [e for e in self.events if self._is_current(e)]
            result = {'sessionId': self.session, 'setRunId': self.run_id, 'sessionEvents': len(current),
                    'historicalEvents': sum(not self._is_current(e) for e in self.events),
                    'recent': deepcopy(current[-8:] if self.run_id else list(self.events)[-8:]),
                    'history': deepcopy(list(self.events)), 'warning': self.warning,
                    'storage': 'Local episodic memory · retained across bridge restarts'}

        result['episodes'] = self.episodes.build([e for e in result['history'] if e['kind'] == 'observation'])
        return result

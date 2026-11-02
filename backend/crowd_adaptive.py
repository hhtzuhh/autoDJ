"""Opt-in webcam feedback: stage bounded Meta choices, commit at deck rotation.

No MIDI writes here. The existing supervisor remains the execution authority.
Movement-to-intent mapping is an explicit demo heuristic, never a preference claim.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import time
from urllib.request import urlopen
from uuid import UUID

from meta_planner import MetaConfig, MINIMUM_DWELL_BEATS, load_dotenv, select_with_meta
from transition_pipeline import PairAnalysisCache, QueueSnapshot, TransitionPipeline, VersionedQueue

FRESH_SECONDS = 120
LEASE_SECONDS = 18
COOLDOWN_SECONDS = 60


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def bounded_orders(ids, locked_index):
    """Preserve played tracks and A→B; move at most one of the next three first."""
    tail = ids[locked_index + 1:locked_index + 4]
    return [ids[:locked_index + 1] + [chosen] + [t for t in tail if t != chosen]
            + ids[locked_index + 4:] for chosen in tail]


def signal_for(job):
    observation = job['observation']
    if observation['quality']['visibility'] != 'good' or observation['quality']['camera_motion'] != 'stable':
        return None, 'uncertain', 'Visibility or camera motion is unsuitable.'
    regions = observation['regions']
    if len(regions) != 1 or regions[0]['id'] != 'audience' or regions[0]['visibility'] != 'good':
        return None, 'uncertain', 'No clearly visible, fixed audience region.'
    cue = job.get('cue', {}).get('value', 'unknown')
    if cue in ('conflict', 'unknown'):
        return None, 'uncertain', 'Gesture is conflicting or uncertain.'
    if cue in ('build', 'ease'):
        return cue, 'gesture', 'Held demo gesture: ' + cue
    r = regions[0]
    spread, amplitude, continuity = (r[k]['value'] for k in (
        'movement_spread', 'movement_amplitude', 'movement_continuity'))
    if spread in ('some', 'most') and amplitude in ('moderate', 'large') and continuity == 'sustained':
        return 'build', 'natural', 'Sustained visible movement; demo policy follows movement intensity.'
    if spread in ('none', 'few') and amplitude in ('still', 'subtle') and continuity in ('absent', 'intermittent'):
        return 'ease', 'natural', 'Low visible movement; demo policy follows movement intensity.'
    return None, 'hold', 'No clear movement signal; keep the current musical direction.'


def fetch_observation(job_id):
    # Fixed local endpoint, never a URL supplied by the browser.
    if str(UUID(job_id)) != job_id:
        raise ValueError('invalid_observation_id')
    with urlopen(f'http://127.0.0.1:8767/api/crowd/jobs/{job_id}', timeout=3) as response:
        return json.loads(response.read(512 * 1024))


class CrowdAdaptiveMixin:
    def _crowd_init(self):
        if not hasattr(self, 'crowd'):
            self.crowd = dict(enabled=False, status='off', reason='Webcam adaptation is off.', session=None,
                              decision=None, lastApplied=None)
            self.crowd_lease = 0.0
            self.crowd_generation = 0
            self.crowd_pending = None
            self.crowd_busy = False
            self.crowd_previous = None
            self.crowd_seen = set()
            self.crowd_last_capture = 0.0
            self.crowd_last_apply = 0.0
            self.crowd_retry_at = 0.0

    def _crowd_stop(self, reason):
        self._crowd_init()
        self.crowd_generation += 1
        self.crowd_pending = None
        self.crowd_previous = None
        self.crowd.update(enabled=False, status='off', reason=reason)

    def _crowd_valid(self):
        self._crowd_init()
        if self.crowd['enabled'] and (time.monotonic() > self.crowd_lease
                or not getattr(self, 'auto_arm_enabled', False)
                or (getattr(self, 'envelope', None) or {}).get('status') in ('invalidated', 'cancelled')):
            self._crowd_stop('Camera heartbeat expired, Auto arm stopped, or human control invalidated the plan.')
        return self.crowd['enabled']

    def crowd_snapshot(self):
        with self.action_lock:
            self._crowd_valid()
            return deepcopy(self.crowd)

    def request_crowd_config(self, payload):
        with self.action_lock:
            self._crowd_valid()
            if payload.get('enabled') is False:
                if self.crowd['enabled'] and payload.get('session') != self.crowd['session']:
                    return 409, {'error': 'Only the owning webcam session can disable adaptation.'}
                self._crowd_stop('Stopped by operator or camera.')
                return 200, deepcopy(self.crowd)
            if payload.get('enabled') is not True:
                return 400, {'error': 'enabled_must_be_boolean'}
            session = payload.get('session', '')
            try:
                if str(UUID(session)) != session: raise ValueError()
            except (ValueError, TypeError, AttributeError):
                return 400, {'error': 'webcam_session_required'}
            if not getattr(self, 'auto_arm_enabled', False) or not getattr(self, 'continuous_set_active', False) or self._future_context() is None or self._recovery_reason():
                return 409, {'error': 'Start a live continuous set with Auto arm before enabling crowd adaptation.'}
            if self.crowd['enabled'] and payload.get('session') != self.crowd['session']:
                return 409, {'error': 'Another webcam session owns adaptation. Stop it or use human takeover first.'}
            self._crowd_stop('Starting a fresh camera session.')
            self.crowd_seen.clear()
            self.crowd_last_capture = 0
            self.crowd.update(enabled=True, session=session, status='observing', reason='Waiting for fresh webcam evidence.', decision=None)
            self.crowd_lease = time.monotonic() + LEASE_SECONDS
            return 200, deepcopy(self.crowd)

    def request_crowd_heartbeat(self, payload):
        with self.action_lock:
            if not self._crowd_valid() or payload.get('session') != self.crowd['session']:
                return 409, {'error': 'Adaptive session is off. Enable it again explicitly.'}
            self.crowd_lease = time.monotonic() + LEASE_SECONDS
            return 200, deepcopy(self.crowd)

    def request_crowd_observation(self, payload):
        try:
            job = fetch_observation(payload.get('jobId', ''))
        except Exception:
            return 400, {'error': 'A completed observation from the local webcam service is required.'}
        return self._crowd_ingest(job)

    def _crowd_ingest(self, job):
        with self.action_lock:
            if not self._crowd_valid(): return 409, {'error': 'Crowd adaptation is off.'}
            try:
                captured = timestamp(job['captureTime'])
                if (job['status'] != 'completed' or job['mode'] != 'webcam' or job['profile'] != 'webcam-v1'
                    or job['webcamSession'] != self.crowd['session'] or not 0 <= time.time() - captured <= FRESH_SECONDS
                    or job['id'] in self.crowd_seen or captured <= self.crowd_last_capture):
                    raise ValueError()
                goal, source, reason = signal_for(job)
            except (KeyError, ValueError, TypeError, AttributeError):
                return 409, {'error': 'Observation is stale, duplicated, out of order, or from another capture session.'}
            self.crowd_seen.add(job['id'])
            self.crowd_last_capture = captured
            previous = self.crowd_previous
            self.crowd_previous = (goal, captured) if source == 'natural' else None
            evidence = {'jobId': job['id'], 'capturedAt': job['captureTime'], 'goal': goal, 'source': source,
                        'reason': reason, 'summary': job['observation']['summary_en'],
                        'evidence': job['observation']['evidence']}
            self.crowd['observation'] = evidence
            if goal is None or (source == 'natural' and (not previous or previous[0] != goal or captured - previous[1] > 90)):
                # A brief neutral window is not a new command. Unreliable
                # observations cancel pending work; natural reversals need two clips.
                if source == 'uncertain':
                    self.crowd_generation += 1
                    self.crowd_pending = None
                if not self.crowd_pending and not self.crowd_busy:
                    self.crowd.update(status='hold', reason=reason if goal is None else 'Waiting for a second consistent natural-movement observation.')
                elif source == 'uncertain':
                    self.crowd.update(status='hold', reason=reason)
                return 200, deepcopy(self.crowd)
            if time.monotonic() - self.crowd_last_apply < COOLDOWN_SECONDS:
                self.crowd.update(status='hold', reason='60-second cooldown after the last applied decision.')
                return 200, deepcopy(self.crowd)
            if time.monotonic() < self.crowd_retry_at:
                self.crowd.update(status='hold', reason='Adaptive planner is cooling down after a failed request.')
                return 200, deepcopy(self.crowd)
            if self.crowd_busy:
                self.crowd.update(reason='A bounded Meta planning request is already in progress.')
                # Opposite evidence must also invalidate an in-flight selection.
                if self.crowd.get('planningGoal') != goal: self.crowd_generation += 1
                return 200, deepcopy(self.crowd)
            if self.crowd_pending and self.crowd_pending['evidence']['goal'] == goal and time.time() - timestamp(self.crowd_pending['evidence']['capturedAt']) < FRESH_SECONDS:
                return 200, deepcopy(self.crowd)
            context = self._future_context()
            if context is None or self.supervisor is None:
                self.crowd.update(status='hold', reason='Waiting for an owned current transition.')
                return 200, deepcopy(self.crowd)
            self.crowd_generation += 1
            self.crowd_pending = None
            self.crowd_busy = True
            self.crowd.update(status='planning', reason='Meta is choosing among locally feasible future orders and transitions.', planningGoal=goal)
            threading.Thread(target=self._crowd_plan_worker, args=(self.crowd_generation, context, evidence),
                             name='idj-crowd-planner', daemon=True).start()
            return 202, deepcopy(self.crowd)

    def _crowd_plan_worker(self, generation, context, evidence):
        meta_call = None
        try:
            with self.action_lock:
                if not self._crowd_valid() or generation != self.crowd_generation or context != self._future_context(): return
                active = deepcopy(self.selection['next_executable_plan'])
                old_queue = deepcopy(self.queue)
                ids = list(old_queue['ordered_track_ids'])
                source = active['deck_b']['audio_sha256']
                index = ids.index(source)
                orders = bounded_orders(ids, index)
                slots = dict(self.sampler_slots)
            if not orders: raise ValueError('No future track remains.')
            candidates = []
            intent = 'impact_at_handoff' if evidence['goal'] == 'build' else 'maintain_energy'
            minimum = int(active['deck_b']['cue_beat_index']) + int(active['mix']['bars']) * 4 + MINIMUM_DWELL_BEATS
            cache = PairAnalysisCache(self.data_root / 'artifacts/live-performance/crowd-pairs', self.analysis_catalog)
            for order in orders:
                window = tuple(order[index:index + 4])
                if any(t not in slots for t in window[1:]): continue
                pairings = tuple(cache.payload(a, b) for a, b in zip(window, window[1:]))
                rows = self._constrained_window_candidates(pairings, minimum, index, order, intent=intent)
                for row in rows[:3]:
                    row = deepcopy(row)
                    row['candidate_id'] = hashlib.sha256(json.dumps([order, row['candidate_id']]).encode()).hexdigest()[:20]
                    row['ordered_track_ids'] = order
                    row['window_track_ids'] = list(window)
                    row['queue_changed'] = order != ids
                    candidates.append(row)
            if not candidates: raise ValueError('No bounded order has a feasible transition window.')
            load_dotenv(Path(__file__).parent.parent / '.env')
            load_dotenv(self.workspace / 'experiments/meta-dj-agent/.env')
            config = MetaConfig.from_environment()
            meta_call = self._begin_meta_call(config, tuple(ids[index:index+4]), 'crowd_adaptive')
            context_data = {**evidence,
                'policy': 'Follow visible movement intensity, not presumed enjoyment. Build favors measured energy/impact; ease favors smoother/lower-impact options. Keep order when no supported improvement exists. Select only supplied candidate IDs. Each candidate carries its own track order. Bass swap is available only for beat-sync crossfades. Do not change the currently locked transition.'}
            selection = select_with_meta(candidates, queue_revision=old_queue['revision'], intent=intent,
                track_ids=tuple(ids[index:index+4]), model=config.model,
                transport=self._meta_transport(config, meta_call), crowd_context=context_data)
            self._finish_meta_call(meta_call)
            completed_call = meta_call
            meta_call = None
            selected_order = selection.candidate['ordered_track_ids']
            pipeline = TransitionPipeline(VersionedQueue(self.analysis_catalog, QueueSnapshot(
                old_queue['revision'], tuple(ids), old_queue.get('updated_at', ''))), cache)
            change, edges = pipeline.set_queue(selected_order)
            queue = {**old_queue, 'revision': change.after.revision, 'ordered_track_ids': selected_order,
                     'updated_at': change.after.updated_at}
            manifest = json.loads(self.set_list_manifest_path.read_text())
            if manifest['queue_revision'] != old_queue['revision'] or [r['track_id'] for r in manifest['ordered_tracks']] != ids:
                raise ValueError('Exact set list changed during planning.')
            entries = {r['track_id']: r for r in manifest['ordered_tracks']}
            manifest = {**manifest, 'queue_revision': queue['revision'], 'ordered_tracks': [entries[t] for t in selected_order]}
            with self.action_lock:
                if not self._crowd_valid() or generation != self.crowd_generation or context != self._future_context(): return
                compiled = self._compile_three_transition_candidate(tuple(selection.candidate['window_track_ids']), selection,
                    config.model, int(active['deck_b']['deck']), int(active['deck_a']['deck']))
                self._stamp_meta_origin(compiled, completed_call)
                compiled['queue_revision'] = queue['revision']
                compiled['crowd_evidence'] = evidence
                compiled['next_executable_plan']['plan_id'] += '-crowd-' + evidence['jobId'][:8]
                compiled['based_on_active_plan_id'], _, compiled['based_on_planning_epoch'] = context
                self.crowd_pending = dict(context=context, generation=generation, evidence=evidence, queue=queue,
                    manifest=manifest, pipeline=pipeline, edges=edges, compiled=compiled, old_queue=old_queue)
                decision = dict(reason=selection.decision.reason, model=config.model,
                    before=ids, after=selected_order, queueChanged=selected_order != ids, lockedThrough=index,
                    sourceTrack=selection.candidate['window_track_ids'][0], targetTrack=selection.candidate['window_track_ids'][1],
                    technique=compiled['transition_buffer'][0]['mode'],
                    bassSwap=bool(compiled['next_executable_plan']['mix'].get('bass_swap')),
                    bars=compiled['next_executable_plan']['mix']['bars'],
                    planId=compiled['next_executable_plan']['plan_id'], observationId=evidence['jobId'])
                self.crowd.update(status='staged', reason='Waiting for the current transition to complete; no playback change yet.', decision=decision)
                self._crowd_record('staged', {'decision': decision, 'evidence': evidence, 'compiled': compiled})
        except Exception as error:
            self._finish_meta_call(meta_call, error)
            with self.action_lock:
                if generation == self.crowd_generation:
                    self.crowd.update(status='hold', reason=f'Adaptive planning unavailable: {type(error).__name__}. Existing queue and plan preserved.')
                    self.crowd_pending = None
                    self.crowd_retry_at = time.monotonic() + 60
        finally:
            with self.action_lock:
                self.crowd_busy = False
                if self.crowd['status'] == 'planning' and not self.crowd_pending:
                    self.crowd.update(status='hold', reason='Playback context or camera evidence changed during planning; existing plan preserved.')

    def _crowd_record(self, event, payload):
        path = self.data_root / 'artifacts/live-performance/crowd-decisions.jsonl'
        with path.open('a') as out:
            out.write(json.dumps({'event': event, 'at': datetime.now(timezone.utc).isoformat(), **payload}) + '\n')

    def _crowd_commit(self, source_id, target_id, recycled_deck):
        """Called by deck-rotation worker before loading. Never mutates an active mix."""
        with self.action_lock:
            if not self._crowd_valid() or not self.crowd_pending: return target_id
            p = self.crowd_pending
            if (p['generation'] != self.crowd_generation or p['context'] != self._future_context()
                    or time.time() - timestamp(p['evidence']['capturedAt']) > FRESH_SECONDS):
                self.crowd_pending = None
                self.crowd.update(status='hold', reason='Staged evidence or playback context expired before the handoff.')
                return target_id
            perf = self.raw_performance
            plan = p['compiled']['next_executable_plan']
            source_deck = perf.decks[int(plan['deck_a']['deck'])]
            envelope = self.supervisor.manager.envelope if self.supervisor and self.supervisor.manager else None
            if (not envelope or envelope.status != 'completed' or self._executor_running()
                or source_deck.track_id != source_id or source_deck.play is not True
                or perf.decks[recycled_deck].play is True or source_deck.beat_index is None
                or int(plan['deck_a']['transition_start_beat_index']) <= int(source_deck.beat_index) + 16
                or self.auto_chain_cancel.is_set()):
                self.crowd_pending = None
                self.crowd.update(status='hold', reason='Fresh playback checks rejected the staged change.')
                return target_id
            # Both files are local-only. Atomic replacements + rollback; a crash
            # between them leaves mismatched revisions that existing startup checks reject.
            queue_path = self.data_root / 'artifacts/live-performance/queue-state-v1.json'
            old_queue_bytes = queue_path.read_bytes()
            old_manifest_bytes = self.set_list_manifest_path.read_bytes()
            try:
                if json.loads(old_queue_bytes) != p['old_queue']:
                    raise ValueError('Queue changed on disk.')
                manifest_now = json.loads(old_manifest_bytes)
                if manifest_now['queue_revision'] != self.queue['revision'] or [r['track_id'] for r in manifest_now['ordered_tracks']] != self.queue['ordered_track_ids']:
                    raise ValueError('Manifest changed on disk.')
                for path, data in ((self.set_list_manifest_path, p['manifest']), (queue_path, p['queue'])):
                    temp = path.with_suffix('.crowd.tmp')
                    temp.write_text(json.dumps(data, indent=2))
                    temp.replace(path)
                if not self._crowd_valid() or p['generation'] != self.crowd_generation or p['context'] != self._future_context():
                    raise ValueError('Control changed while committing.')
                self._crowd_record('queue_committed', {'decision': self.crowd['decision'], 'evidence': p['evidence']})
            except Exception:
                for path, data in ((queue_path, old_queue_bytes), (self.set_list_manifest_path, old_manifest_bytes)):
                    temp = path.with_suffix('.crowd-rollback.tmp')
                    temp.write_bytes(data)
                    temp.replace(path)
                self.crowd_pending = None
                self.crowd.update(status='hold', reason='Adaptive commit failed; restored previous queue and manifest.')
                return target_id
            self.queue = p['queue']
            self.transition_pipeline, self.transition_edges = p['pipeline'], p['edges']
            self.planning_epoch = getattr(self, 'planning_epoch', 0) + 1
            p['compiled']['based_on_planning_epoch'] = self.planning_epoch
            self.future_transition_plan = p['compiled']
            self.selection['following_feasible_draft'] = self._draft_from_buffer_edge(p['compiled']['transition_buffer'][0])
            self.selection['transition_buffer'] = []
            self.transition_overrides = {}
            self.crowd_pending = None
            self.crowd_last_apply = time.monotonic()
            self.crowd.update(status='committed', reason='Future queue committed. Waiting for verified deck load and plan promotion.',
                              lastApplied=deepcopy(self.crowd['decision']))
            self._append_system_event('Crowd adaptive queue committed', self.crowd['decision']['reason'])
            return plan['deck_b']['audio_sha256']

    def _crowd_execution_status(self, status, plan_id=None):
        with self.action_lock:
            self._crowd_init()
            last = self.crowd.get('lastApplied')
            if status == 'rotation_failed' and (not last or last.get('executionStatus') in ('completed', 'cancelled', 'invalidated', 'rotation_failed')): return
            if last and (plan_id is None or plan_id.startswith(last['planId'])):
                last['executionStatus'] = status
                try:
                    self._crowd_record(status, {'decision': last})
                except OSError:
                    self.crowd['auditWarning'] = 'Execution audit could not be saved; live status is still available.'

    def _crowd_observe_execution(self):
        # The high-frequency observer must never wait behind a planner request.
        if not self.action_lock.acquire(blocking=False): return
        try:
            self._crowd_valid()
            last = self.crowd.get('lastApplied')
            envelope = self.envelope or {}
            state = envelope.get('status')
            if last and str(envelope.get('plan_id', '')).startswith(last['planId']) and state in ('executing', 'completed', 'cancelled', 'invalidated') and last.get('executionStatus') != state:
                last['executionStatus'] = state
                # File I/O stays off the observer thread.
                threading.Thread(target=self._crowd_record, args=(state, {'decision': deepcopy(last)}), daemon=True).start()
        finally:
            self.action_lock.release()

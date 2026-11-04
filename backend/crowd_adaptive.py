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
from performance_memory import PerformanceMemory
from set_runs import SetRuns
from planning_history import PlanningHistory

from meta_planner import MetaConfig, MINIMUM_DWELL_BEATS, load_dotenv, select_with_meta
from performance_actions import tempo_ramp_options
from transition_pipeline import PairAnalysisCache, QueueSnapshot, TransitionPipeline, VersionedQueue

FRESH_SECONDS = 120
LEASE_SECONDS = 18


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def bounded_orders(ids, locked_index):
    """Keep the locked prefix; consider the remaining set, moving one song next.

    Up to ten diverse next-song options fit the model candidate budget. The
    unchanged order is always first. All other songs keep their relative order.
    """
    tail = ids[locked_index + 1:]
    choices = tail if len(tail) <= 10 else [tail[round(i * (len(tail)-1) / 9)] for i in range(10)]
    return [ids[:locked_index + 1] + [chosen] + [t for t in tail if t != chosen]
            for chosen in choices]


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
    if cue == 'reorder':
        return 'reorder', 'gesture', 'Held number 1: explicitly request a different remaining song order.'
    if cue in ('build', 'ease'):
        return cue, 'gesture', 'Held demo gesture: ' + cue
    return None, 'natural', 'Audience movement recorded with the song. Only explicit gestures request DJ changes.'


def fetch_observation(job_id):
    # Fixed local endpoint, never a URL supplied by the browser.
    if str(UUID(job_id)) != job_id:
        raise ValueError('invalid_observation_id')
    with urlopen(f'http://127.0.0.1:8767/api/crowd/jobs/{job_id}', timeout=3) as response:
        return json.loads(response.read(512 * 1024))


class CrowdAdaptiveMixin:
    def _memory(self):
        if not hasattr(self, 'performance_memory'):
            self.performance_memory = PerformanceMemory(self.data_root / 'artifacts/live-performance/memory/events-v1.jsonl')
        return self.performance_memory

    def _runs(self):
        if not hasattr(self, 'set_runs'):
            self.set_runs = SetRuns(self.data_root / 'artifacts/live-performance/set-runs')
            if self.set_runs.current:
                self._memory().begin_run(self.set_runs.current['id'])
        return self.set_runs

    def _run_id(self):
        return (getattr(getattr(self, 'set_runs', None), 'current', None) or {}).get('id')

    def _begin_set_run(self):
        self._crowd_discard_pending('A new set started before this change was applied.')
        run = self._runs().start(self.selection, self.queue)
        self._memory().begin_run(run['id'])
        self._crowd_init()
        self.crowd_generation += 1  # In-flight responses cannot cross a Start boundary.
        self.crowd_pending = self.crowd_requested = self.crowd_previous = None
        self.crowd_last_capture = self.crowd_last_apply = self.crowd_retry_at = 0
        self.crowd_reorder_held = False
        self.crowd_gesture_held = None
        if not self.crowd.get('observing'):
            self.crowd['enabled'] = False
        self.crowd.update(decision=None, lastApplied=None, observation=None, request=None,
            status='observing' if self.crowd['enabled'] else 'off', reason='New set started; waiting for fresh observations.')
        self.selection['set_run_id'] = run['id']
        if self.selection.get('meta_origin'):
            self.selection['meta_origin']['setRunId'] = run['id']
        self.selection['next_executable_plan']['set_run_id'] = run['id']
        if hasattr(self, 'events'):
            self.events.clear()
        if hasattr(self, 'meta_calls_lock'):
            with self.meta_calls_lock:
                for call in self.meta_calls:
                    if call['id'] == run['requestId']:
                        call['setRunId'] = run['id']
        self._memory().record('set_start', {'setRunId': run['id'], 'status': 'starting',
            'planId': run['planId'], 'requestId': run['requestId']}, run['trackIds'])
        # The reviewed request precedes Start. Copy its decision into this run,
        # preserving the original request ID and making the association explicit.
        for row in reversed(list(self._memory().events)):
            if row['kind'] == 'decision' and row['data'].get('requestId') == run['requestId'] and run['requestId']:
                payload = deepcopy(row['data'])
                payload['setRunId'] = run['id']
                if payload.get('decision'):
                    payload['decision']['setRunId'] = run['id']
                self._memory().record('decision', payload, row['trackIds'])
                break
        return run

    def _planning_memory_context(self, track_ids, *, for_gesture=False):
        return {'memory': self._memory().context(track_ids,
            active=for_gesture and bool(getattr(self, 'continuous_set_active', False)))}

    def _crowd_init(self):
        if not hasattr(self, 'crowd'):
            self.crowd = dict(enabled=False, observing=False, status='off', reason='Webcam adaptation is off.', session=None,
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
            self.crowd_requested = None
            self.crowd_reorder_held = False
            self.crowd_gesture_held = None

    def _crowd_stop(self, reason):
        self._crowd_init()
        self._crowd_discard_pending(reason)
        self.crowd_generation += 1
        self.crowd_pending = None
        self.crowd_requested = None
        self.crowd_previous = None
        self.crowd.update(enabled=False, observing=False, status='off', reason=reason)

    def _crowd_stop_observing(self, reason):
        """Stop accepting camera input without revoking an accepted operator request."""
        self._crowd_init()
        working = bool(self.crowd_requested or self.crowd_busy or self.crowd_pending)
        self.crowd.update(observing=False, enabled=working, observationReason=reason)
        if not working:
            self.crowd.update(status='off', reason=reason)
        self._memory().record('observation_stopped', {'setRunId': self._run_id(),
            'reason': reason, 'acceptedRequestRetained': working})

    def _crowd_discard_pending(self, reason):
        """Every staged proposal gets a terminal audit before it is cleared."""
        self._crowd_init()
        if not self.crowd_pending:
            return
        decision = deepcopy(self.crowd.get('decision') or {})
        if decision:
            decision.update(historyStatus='discarded', outcomeReason=reason)
            self.crowd['decision'] = decision
            try:
                self._crowd_record('discarded', {'decision': decision})
            except OSError:
                self._memory().record('decision', {'setRunId': decision.get('setRunId'),
                    'status': 'discarded', 'decision': decision})
                self.crowd['auditWarning'] = 'Proposal cancellation retained in memory; detailed audit could not be saved.'
        self.crowd_pending = None

    def _crowd_valid(self):
        self._crowd_init()
        if self.crowd['enabled'] and (not getattr(self, 'auto_arm_enabled', False)
                or (getattr(self, 'envelope', None) or {}).get('status') in ('invalidated', 'cancelled')):
            self._crowd_stop('Auto arm stopped or human control invalidated the plan.')
        elif self.crowd.get('observing') and time.monotonic() > self.crowd_lease:
            self._crowd_stop_observing('Camera heartbeat expired; accepted requests remain valid until their original expiry.')
        elif not self.crowd.get('observing') and not (self.crowd_requested or self.crowd_busy or self.crowd_pending):
            self.crowd['enabled'] = False
        return self.crowd['enabled']

    def crowd_snapshot(self):
        with self.action_lock:
            self._crowd_valid()
            return self._crowd_payload()

    def _planning_attempts(self):
        if not hasattr(self, 'planning_history'):
            self.planning_history = PlanningHistory(self.data_root / 'artifacts/live-performance/meta-calls')
        memory = self._memory()
        with memory.lock:
            events = deepcopy(list(memory.events))
        return self.planning_history.attempts(events)

    def _crowd_payload(self):
        # Every successful endpoint returns the same complete UI state.
        runs = self._runs()
        attempts = self._planning_attempts()
        decisions = self.planning_history.decorate_decisions(self._memory().crowd_decisions())
        for decision in decisions:
            decision['inTrackExecution'] = self._memory().episodes.action_execution(decision['planId'], decision.get('setRunId'))
        return {**deepcopy(self.crowd), 'setRun': deepcopy(runs.current), 'setRuns': deepcopy(runs.rows), 'memory': self._memory().snapshot(),
                'decisionHistory': decisions, 'planningAttempts': attempts}

    def request_crowd_config(self, payload):
        with self.action_lock:
            self._crowd_valid()
            if payload.get('enabled') is False:
                if self.crowd['enabled'] and payload.get('session') != self.crowd['session']:
                    return 409, {'error': 'Only the owning webcam session can disable adaptation.'}
                if payload.get('preserveAccepted') is True:
                    causes = {'camera_stopped': 'Camera stopped.', 'page_hidden': 'Browser page hidden.',
                        'page_closed': 'Browser page closed or view unmounted.',
                        'camera_interrupted': 'Camera interrupted.', 'observation_error': 'Camera observation failed.',
                        'new_gestures_stopped': 'New gestures stopped.', 'connection_lost': 'Live connection lost.'}
                    self._crowd_stop_observing(causes.get(payload.get('reason'), 'New camera observations stopped.'))
                else:
                    self._crowd_stop('Adaptive requests explicitly cancelled.')
                return 200, self._crowd_payload()
            if payload.get('enabled') is not True:
                return 400, {'error': 'enabled_must_be_boolean'}
            session = payload.get('session', '')
            try:
                if str(UUID(session)) != session: raise ValueError()
            except (ValueError, TypeError, AttributeError):
                return 400, {'error': 'webcam_session_required'}
            if not getattr(self, 'auto_arm_enabled', False) or not getattr(self, 'continuous_set_active', False) or self._future_context() is None or self._recovery_reason():
                return 409, {'error': 'Start a live continuous set with Auto arm before enabling crowd adaptation.'}
            if self.crowd.get('observing') and payload.get('session') != self.crowd['session']:
                return 409, {'error': 'Another webcam session owns adaptation. Stop it or use human takeover first.'}
            working = bool(self.crowd_requested or self.crowd_busy or self.crowd_pending)
            if not working:
                self._crowd_stop('Starting a fresh camera session.')
            self.crowd_seen.clear()
            self.crowd_reorder_held = False
            self.crowd_gesture_held = None
            self.crowd_last_capture = 0
            self.crowd.update(enabled=True, observing=True, session=session, observationReason=None)
            if not working:
                self.crowd.update(status='observing', reason='Waiting for fresh webcam evidence.', decision=None, request=None)
            self.crowd_lease = time.monotonic() + LEASE_SECONDS
            return 200, self._crowd_payload()

    def request_crowd_heartbeat(self, payload):
        with self.action_lock:
            if not self._crowd_valid() or not self.crowd.get('observing') or payload.get('session') != self.crowd['session']:
                return 409, {'error': 'Adaptive session is off. Enable it again explicitly.'}
            self.crowd_lease = time.monotonic() + LEASE_SECONDS
            return 200, self._crowd_payload()

    def request_crowd_observation(self, payload):
        try:
            job = fetch_observation(payload.get('jobId', ''))
        except Exception:
            return 400, {'error': 'A completed observation from the local webcam service is required.'}
        return self._crowd_ingest(job)

    def _crowd_ingest(self, job):
        with self.action_lock:
            status, payload = self._crowd_ingest_current(job)
            self._memory().record('feedback_status', {'setRunId': job.get('setRunId'),
                'jobId': job.get('id'), 'status': payload.get('status', 'rejected'),
                'reason': payload.get('ignoredObservation') or payload.get('error') or payload.get('reason')})
            if status < 300:
                payload = {**payload, 'memory': self._memory().snapshot()}
            return status, payload

    def _crowd_ingest_current(self, job):
        with self.action_lock:
            if not self._crowd_valid() or not self.crowd.get('observing'): return 409, {'error': 'New camera observations are off.'}
            run_id = self._run_id()
            if job.get('setRunId') != run_id:
                return 200, {**self._crowd_payload(), 'ignoredObservation': 'Capture belongs to another set run; it remains in its original log.'}
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
            evidence = {'durationSeconds': job.get('durationSeconds', 6), 'setRunId': run_id, 'jobId': job['id'], 'capturedAt': job['captureTime'], 'goal': goal, 'source': source,
                        'reason': reason, 'summary': job['observation']['summary_en'],
                        'evidence': job['observation']['evidence']}
            memory_row = self._memory().observe(evidence, job['observation'], captured)
            evidence['memoryId'] = memory_row['id']
            self.crowd['observation'] = evidence
            force_reorder = goal == 'reorder' and source == 'gesture'
            if source == 'gesture':
                if self.crowd_gesture_held == goal:
                    self.crowd['reason'] = 'Gesture already received. Lower your hands for an observed clip before repeating.'
                    return 200, self._crowd_payload()
                self.crowd_gesture_held = goal
                self.crowd_reorder_held = force_reorder
            elif source != 'uncertain':
                self.crowd_gesture_held = None
                self.crowd_reorder_held = False
            if goal is None:
                if source == 'uncertain':
                    self.crowd_generation += 1
                    self._crowd_discard_pending('Camera evidence became uncertain before application.')
                    self.crowd_requested = None
                if not self.crowd_pending and not self.crowd_busy:
                    self.crowd.update(status='hold', reason=reason)
                elif source == 'uncertain':
                    self.crowd.update(reason=reason)
                return 200, self._crowd_payload()
            self.crowd_retry_at = 0.0  # New explicit gestures may retry; held poses do not.
            if self.crowd_busy and self.crowd.get('planningGoal') == goal:
                self.crowd.update(reason='Rolling planner is using this direction; new observations have been saved.')
                return 200, self._crowd_payload()
            if self.crowd_pending and self.crowd_pending['evidence']['goal'] == goal and time.time() - timestamp(self.crowd_pending['evidence']['capturedAt']) < FRESH_SECONDS:
                return 200, self._crowd_payload()
            self.crowd['request'] = {k: deepcopy(evidence[k]) for k in ('jobId', 'setRunId', 'capturedAt', 'goal')}
            context = self._future_context()
            if context is None or self.supervisor is None:
                self.crowd.update(status='hold', reason='Waiting for an owned current transition.')
                return 200, self._crowd_payload()
            if force_reorder:
                locked_target = self.selection['next_executable_plan']['deck_b']['audio_sha256']
                ids = self.queue['ordered_track_ids']
                if locked_target not in ids or len(ids) - ids.index(locked_target) - 1 < 2:
                    self.crowd.update(status='hold', reason='Cannot reorder: fewer than two unlocked songs remain. Current handoff and order are preserved.')
                    return 200, self._crowd_payload()
                self.crowd_retry_at = 0.0
            self.crowd_generation += 1
            self._crowd_discard_pending('A newer webcam request replaced this pending proposal.')
            self.crowd_requested = evidence
            self.crowd.update(status='queued', reason='Number 1 accepted: preserving the current and next prepared handoff, then reordering later songs.' if force_reorder else 'Evidence saved. Waiting for the shared rolling planner.')
            self._schedule_future_transition_plan()
            return 202, self._crowd_payload()

    def _crowd_schedule(self, context):
        """Admission through the rolling scheduler; caller owns action_lock."""
        self._crowd_valid()
        if self.crowd_busy:
            return True
        evidence = self.crowd_requested
        if not evidence or not self.crowd['enabled']:
            return False
        if time.time() - timestamp(evidence['capturedAt']) > FRESH_SECONDS:
            self.crowd_requested = None
            self.crowd.update(status='hold', reason='Queued evidence expired; keeping the existing plan.')
            return False
        if time.monotonic() < self.crowd_retry_at:
            return False
        if evidence['goal'] == 'reorder':
            anchor = self._crowd_reorder_anchor()
            if anchor is None:
                self.crowd.update(status='queued', reason='Reorder queued: waiting for a prepared next handoff before changing later songs.')
                return False  # Let the ordinary planner supply the protected handoff.
            ids = self.queue['ordered_track_ids']
            if len(ids) - ids.index(anchor['plan']['deck_b']['audio_sha256']) - 1 < 2:
                self.crowd_requested = None
                self.crowd.update(status='hold', reason='Cannot reorder: fewer than two songs remain after the protected next handoff.')
                return False
            evidence = {**evidence, 'reorderAnchor': anchor}
        self.crowd_requested = None
        self.crowd_busy = True
        self.crowd.update(status='planning', reason='Meta is handling an explicit gesture: reorder songs or adjust future in-track actions.', planningGoal=evidence['goal'])
        threading.Thread(target=self._crowd_plan_worker, args=(self.crowd_generation, context, evidence),
                         name='idj-crowd-planner', daemon=True).start()
        return True

    @staticmethod
    def _crowd_plan_binding(plan):
        # Arm changes execution IDs, but not musical timing or deck identities.
        return {k: deepcopy(plan.get(k)) for k in ('deck_a', 'deck_b', 'mix',
            'performance_actions', 'tempo_routine', 'in_track_actions', 'automation_start_beat_index')}

    def _crowd_reorder_anchor(self):
        context = self._future_context()
        if context is None:
            return None
        active = self.selection['next_executable_plan']
        future = self.future_transition_plan
        if (isinstance(future, dict) and future.get('based_on_active_plan_id') == context[0]
                and future.get('queue_revision') == context[1]
                and future.get('based_on_planning_epoch', 0) == context[2]):
            edges = future.get('transition_buffer', [])
        else:
            edges = self.selection.get('transition_buffer', [])[1:]
        ids = self.queue['ordered_track_ids']
        source = active['deck_b']['audio_sha256']
        if not edges or source not in ids or ids.index(source) + 1 >= len(ids):
            return None
        edge = edges[0]
        target = ids[ids.index(source) + 1]
        if edge.get('source_track_id') != source or edge.get('target_track_id') != target:
            return None
        try:
            plan = self._plan_from_buffer_edge(edge, source_id=source, target_id=target,
                source_deck=int(active['deck_b']['deck']), target_deck=int(active['deck_a']['deck']),
                model=str(self.selection.get('model') or 'Meta'), suffix='reorder-anchor')
        except (KeyError, ValueError, TypeError):
            return None  # An incomplete/legacy draft is not a protected executable handoff.
        return dict(plan=plan, originalPlan=self._crowd_plan_binding(active),
            queue=deepcopy(self.queue), previousEdges=deepcopy(edges[1:]))

    def _crowd_anchor_phase(self, anchor):
        """Allow one normal promotion, never changed cues, queue or a passed anchor."""
        if self.queue != anchor['queue'] or self._future_context() is None:
            return None
        active = self._crowd_plan_binding(self.selection['next_executable_plan'])
        if active == self._crowd_plan_binding(anchor['plan']):
            return 'anchor'
        if active == anchor['originalPlan']:
            upcoming = self._crowd_reorder_anchor()
            if upcoming and self._crowd_plan_binding(upcoming['plan']) == self._crowd_plan_binding(anchor['plan']):
                return 'before'
        return None

    def _crowd_retry_reorder_after_handoff(self, generation, context, evidence, active, old_queue):
        """Replan the original request, never apply a response for an old handoff.

        Caller holds action_lock. Preserve capture time and generation so a
        retry cannot extend evidence freshness or revive a cancelled gesture.
        """
        current = self._future_context()
        if (evidence.get('goal') != 'reorder' or evidence.get('source') != 'gesture'
                or not self._crowd_valid() or generation != self.crowd_generation
                or not self.continuous_set_active or current is None or current == context
                or evidence.get('setRunId') != self._run_id()
                or not 0 <= time.time() - timestamp(evidence['capturedAt']) <= FRESH_SECONDS
                or self.crowd_requested is not None or self.crowd_pending is not None
                or self.queue['revision'] != old_queue['revision']
                or self.queue['ordered_track_ids'] != old_queue['ordered_track_ids']):
            return False
        ids = self.queue['ordered_track_ids']
        plan = self.selection['next_executable_plan']
        previous_target = active['deck_b']['audio_sha256']
        source, target = plan['deck_a']['audio_sha256'], plan['deck_b']['audio_sha256']
        if (any(t not in ids for t in (previous_target, source, target))
                or ids.index(source) < ids.index(previous_target)
                or ids.index(target) != ids.index(source) + 1
                or len(ids) - ids.index(target) - 1 < 2):
            return False
        self.crowd_requested = {k: deepcopy(v) for k, v in evidence.items() if k != 'reorderAnchor'}
        self.crowd_retry_at = 0.0
        reason = 'Playback advanced during reorder planning; retrying the same request against the new locked handoff.'
        self.crowd.update(status='queued', reason=reason)
        self._memory().record('feedback_status', {'setRunId': evidence.get('setRunId'),
            'jobId': evidence['jobId'], 'status': 'queued', 'reason': reason})
        return True

    def _crowd_plan_worker(self, generation, context, evidence):
        meta_call = None
        ids = []
        try:
            with self.action_lock:
                anchor = evidence.get('reorderAnchor')
                context_valid = self._crowd_anchor_phase(anchor) is not None if anchor else context == self._future_context()
                if not self._crowd_valid() or generation != self.crowd_generation or not context_valid: return
                active = deepcopy(self.selection['next_executable_plan'])
                original_active = deepcopy(active)
                old_queue = deepcopy(self.queue)
                ids = list(old_queue['ordered_track_ids'])
                source = active['deck_b']['audio_sha256']
                index = ids.index(source)
                # Freeze the plan being replaced, before inference or queue mutation.
                # A ready future window takes precedence over the older buffer.
                future = self.future_transition_plan
                if (isinstance(future, dict)
                    and future.get('based_on_active_plan_id') == context[0]
                    and future.get('queue_revision') == context[1]
                    and future.get('based_on_planning_epoch', 0) == context[2]):
                    previous_edges = future.get('transition_buffer', [])
                else:
                    previous_edges = self.selection.get('transition_buffer', [])[1:]
                if anchor:
                    if self._crowd_anchor_phase(anchor) is None:
                        return
                    active = deepcopy(anchor['plan'])
                    source = active['deck_b']['audio_sha256']
                    index = ids.index(source)
                    previous_edges = anchor['previousEdges']
                before_window = self._crowd_window_summary(previous_edges)
                orders = ([order for order in bounded_orders(ids, index) if order != ids]
                          if evidence['goal'] == 'reorder' else [ids])
                slots = dict(self.sampler_slots)
            if not orders: raise ValueError('No different remaining order is available.' if evidence['goal'] == 'reorder' else 'No future track remains.')
            candidate_groups = []
            intent = 'maintain_energy'
            minimum = int(active['deck_b']['cue_beat_index']) + int(active['mix'].get('bars', 0)) * 4 + int(active.get('tempo_routine', {}).get('post_beats', 0)) + MINIMUM_DWELL_BEATS
            cache = PairAnalysisCache(self.data_root / 'artifacts/live-performance/crowd-pairs', self.analysis_catalog)
            for order in orders:
                window = tuple(order[index:index + 4])
                if any(t not in slots for t in window[1:]): continue
                pairings = tuple(cache.payload(a, b) for a, b in zip(window, window[1:]))
                rows = self._constrained_window_candidates(pairings, minimum, index, order, intent=intent)
                if evidence['goal'] == 'build':
                    # Prefer an existing cue with room for a real standalone action.
                    # The model still chooses the action; full validation remains mandatory.
                    def has_action_room(row):
                        first = row['transitions'][0]
                        options = tempo_ramp_options(first)
                        if not options or not row.get('in_track_planning'):
                            return False
                        earliest = max(8, first.get('earliest_source_action_beat', 8))
                        return first['source_exit_beat_index'] - min(o['minimum_source_ramp_beats'] for o in options) - 8 - earliest >= 8
                    rows.sort(key=has_action_room, reverse=True)
                group = []
                for row in rows[:3]:
                    row = deepcopy(row)
                    row['candidate_id'] = hashlib.sha256(json.dumps([order, row['candidate_id']]).encode()).hexdigest()[:20]
                    row['ordered_track_ids'] = order
                    row['window_track_ids'] = list(window)
                    row['queue_changed'] = order != ids
                    group.append(row)
                candidate_groups.append(group)
            # Round-robin avoids spending the ten-candidate budget on only the
            # first few orders, which would hide distant songs from the model.
            candidates = [group[i] for i in range(3) for group in candidate_groups if len(group) > i][:10]
            if not candidates: raise ValueError('No bounded order has a feasible transition window.')
            load_dotenv(self.data_root / '.env')
            config = MetaConfig.from_environment()
            meta_call = self._begin_meta_call(config, tuple(ids[index:index+4]), 'crowd_adaptive')
            context_data = {**{k: v for k, v in evidence.items() if k != 'reorderAnchor'}, **self._planning_memory_context(tuple(ids), for_gesture=True),
                'requested_change': 'in_track_actions',
                'previous_window': before_window,
                'policy': 'This request concerns optional IN-TRACK actions during future songs, not just the handoff. '
                    'Keep song order. Build requests a clearly audible but bounded in-track embellishment: '
                    'consider one bass_pulse or bass_reveal in a valid future source-song slot, preferably the first source song in the window. '
                    'Ease requests fewer/gentler in-track actions, including removing them. '
                    'An explicit gesture is an operator instruction, not evidence of enjoyment. '
                    'Use musical context and timing bounds; [] remains valid when no suitable action exists, '
                    'but explain the in-track-specific reason. A transition-only change does not fulfill an in-track request. '
                    'Do not change the currently locked transition.'}
            if evidence['goal'] == 'reorder':
                context_data['requested_change'] = 'queue_order'
                context_data['policy'] = ('The operator explicitly held the number 1 gesture to request a different remaining song order. '
                    'Every supplied candidate changes the unlocked order. Choose the most musically suitable feasible option and explain the gesture request in English. '
                    'This gesture does not imply build/ease, mood or enjoyment. Preserve the locked prefix, including the protected next handoff. '
                    'Choose transition parameters and optional in-track actions only as supported; no extra action is required.')
            with self.meta_calls_lock:
                next(call for call in self.meta_calls if call['id'] == meta_call)['memoryContext'] = context_data['memory']
            selection = select_with_meta(candidates, queue_revision=old_queue['revision'], intent=intent,
                track_ids=tuple(ids[index:index+4]), model=config.model,
                transport=self._meta_transport(config, meta_call), crowd_context=context_data,
                track_context=self._planning_track_context(tuple(ids)))
            self._finish_meta_call(meta_call)
            completed_call = meta_call
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
                context_valid = self._crowd_anchor_phase(anchor) is not None if anchor else context == self._future_context()
                issue = None
                if not self._crowd_valid():
                    issue = 'Adaptive request cancelled: ' + self.crowd['reason']
                elif generation != self.crowd_generation:
                    issue = 'A newer gesture, uncertain observation, or set run replaced this request.'
                elif not 0 <= time.time() - timestamp(evidence['capturedAt']) <= FRESH_SECONDS:
                    issue = 'The original gesture expired before its response could be applied.'
                elif not context_valid:
                    issue = 'The protected handoff changed or already passed.' if anchor else 'Playback advanced during planning.'
                if issue:
                    self._finish_meta_call(completed_call, RuntimeError('active_plan_changed_during_future_meta_call'), discard_reason=issue)
                    self._crowd_retry_reorder_after_handoff(generation, context, evidence, original_active, old_queue)
                    return
                compiled = self._compile_three_transition_candidate(tuple(selection.candidate['window_track_ids']), selection,
                    config.model, int(active['deck_b']['deck']), int(active['deck_a']['deck']))
                self._stamp_meta_origin(compiled, completed_call)
                compiled['meta_origin']['memoryContext'] = context_data['memory']
                compiled['queue_revision'] = queue['revision']
                compiled['crowd_evidence'] = evidence
                compiled['next_executable_plan']['plan_id'] += '-crowd-' + evidence['jobId'][:8]
                compiled['based_on_active_plan_id'], _, compiled['based_on_planning_epoch'] = context
                self.crowd_pending = dict(context=context, generation=generation, evidence=evidence, queue=queue,
                    manifest=manifest, pipeline=pipeline, edges=edges, compiled=compiled, old_queue=old_queue, anchor=anchor)
                decision = dict(triggerGoal=evidence.get('goal'), triggerSource=evidence.get('source'), requestId=completed_call, setRunId=evidence.get('setRunId'), reason=selection.decision.reason, model=config.model,
                    requestedChange=context_data['requested_change'],
                    before=ids, after=selected_order, queueChanged=selected_order != ids, lockedThrough=index,
                    sourceTrack=selection.candidate['window_track_ids'][0], targetTrack=selection.candidate['window_track_ids'][1],
                    technique=compiled['transition_buffer'][0]['technique'],
                    bassSwap=bool(compiled['next_executable_plan']['mix'].get('bass_swap')),
                    bars=compiled['next_executable_plan']['mix'].get('bars', 0),
                    planId=compiled['next_executable_plan']['plan_id'], observationId=evidence['jobId'],
                    changeSummary=dict(beforeWindow=before_window,
                        afterWindow=self._crowd_window_summary(compiled['transition_buffer']),
                        observationSummary=evidence.get('summary'), capturedAt=evidence.get('capturedAt'),
                        selectedAt=datetime.now(timezone.utc).isoformat()),
                    memoryContext=context_data['memory'],
                    transitionActions=[e.get('routine', {}).get('actions', []) for e in compiled['transition_buffer']],
                    inTrackActions=selection.decision.in_track_actions)
                old_actions = [action for edge in before_window for action in (edge.get('inTrackActions') or [])]
                new_actions = [action for edge in decision['changeSummary']['afterWindow'] for action in (edge.get('inTrackActions') or [])]
                in_track_unchanged = context_data['requested_change'] == 'in_track_actions' and old_actions == new_actions
                if selected_order == ids and (in_track_unchanged or before_window == decision['changeSummary']['afterWindow']):
                    self.crowd_pending = None
                    # A handoff-only difference must not masquerade as fulfilling
                    # a request for an in-song effect. Keep the existing window.
                    decision['changeSummary']['afterWindow'] = deepcopy(before_window)
                    decision['inTrackActions'] = old_actions
                    decision['transitionActions'] = [edge.get('transitionActions', []) for edge in before_window]
                    decision.update(historyStatus='unchanged', outcomeReason=(
                        'No in-track action change selected; existing order and plan kept. ' if in_track_unchanged else '') + selection.decision.reason)
                    self.crowd_last_apply = time.monotonic()
                    self.crowd.update(status='hold', reason='Meta kept the existing order and actions; no application needed.', decision=decision)
                    self._crowd_record('unchanged', {'decision': decision, 'evidence': evidence})
                    return
                self.crowd.update(status='staged', reason=('Later-song reorder ready; waiting for the protected next handoff to complete.' if anchor else 'Waiting for the current transition to complete; no playback change yet.'), decision=decision)
                self._crowd_record('staged', {'decision': decision, 'evidence': evidence, 'compiled': compiled})
        except Exception as error:
            self._finish_meta_call(meta_call, error)
            with self.action_lock:
                if generation == self.crowd_generation:
                    self.crowd.update(status='hold', reason=(f'Requested reorder unavailable: {type(error).__name__}: {error}. Existing queue and plan preserved.'
                        if evidence['goal'] == 'reorder' else f'Adaptive planning unavailable: {type(error).__name__}. Existing queue and plan preserved.'))
                    self.crowd_pending = None
                    self.crowd_retry_at = time.monotonic() + 60
                self._memory().record('planning_failed', {'setRunId': evidence.get('setRunId'), 'error': type(error).__name__, 'observationId': evidence['jobId']},
                    ids)
        finally:
            with self.action_lock:
                self.crowd_busy = False
                if self.crowd['status'] == 'planning' and not self.crowd_pending:
                    self.crowd.update(status='hold', reason='Playback context or camera evidence changed during planning; existing plan preserved.')
                if self.crowd_requested:
                    self._schedule_future_transition_plan()

    @staticmethod
    def _crowd_window_summary(edges):
        return [dict(sourceTrack=e.get('source_track_id'), targetTrack=e.get('target_track_id'),
            technique=e.get('technique'), sourceExitBeat=e.get('source_exit_beat_index'),
            targetEntryBeat=e.get('target_entry_beat_index'), bars=e.get('transition_bars', e.get('bars')),
            transitionActions=deepcopy(e.get('routine', {}).get('actions')),
            inTrackActions=deepcopy(e.get('routine', {}).get('in_track_actions')))
            for e in edges]

    def _crowd_record(self, event, payload):
        run_id = payload.get('decision', {}).get('setRunId')
        payload = {**payload, 'setRunId': run_id}
        path = self.data_root / 'artifacts/live-performance/crowd-decisions.jsonl'
        with path.open('a') as out:
            out.write(json.dumps({'event': event, 'at': datetime.now(timezone.utc).isoformat(), **payload}) + '\n')
        decision = payload.get('decision', {})
        # Do not recursively store previous memory contexts inside new memories.
        compact = {k: v for k, v in decision.items() if k != 'memoryContext'}
        kind = 'execution' if event in ('executing', 'completed', 'cancelled', 'invalidated') else 'decision'
        self._memory().record(kind,
            {'setRunId': run_id, 'status': event, 'decision': compact}, [t for t in (decision.get('sourceTrack'), decision.get('targetTrack')) if t])

    def _crowd_commit(self, source_id, target_id, recycled_deck):
        """Called by deck-rotation worker before loading. Never mutates an active mix."""
        with self.action_lock:
            if not self._crowd_valid() or not self.crowd_pending: return target_id
            p = self.crowd_pending
            anchor = p.get('anchor')
            phase = self._crowd_anchor_phase(anchor) if anchor else None
            context_valid = phase is not None if anchor else p['context'] == self._future_context()
            if (p['generation'] != self.crowd_generation or not context_valid
                    or time.time() - timestamp(p['evidence']['capturedAt']) > FRESH_SECONDS):
                self._crowd_discard_pending('Staged evidence or playback context expired before the handoff.')
                self.crowd.update(status='hold', reason='Staged evidence or playback context expired before the handoff.')
                return target_id
            if phase == 'before':
                return target_id  # Preserve B→C; this proposal starts only after C enters.
            if anchor:
                p['context'] = self._future_context()
                p['compiled']['based_on_active_plan_id'], _, p['compiled']['based_on_planning_epoch'] = p['context']
            perf = self.raw_performance
            plan = p['compiled']['next_executable_plan']
            source_deck = perf.decks[int(plan['deck_a']['deck'])]
            envelope = self.supervisor.manager.envelope if self.supervisor and self.supervisor.manager else None
            if (not envelope or envelope.status != 'completed' or self._executor_running()
                or source_deck.track_id != source_id or source_deck.play is not True
                or perf.decks[recycled_deck].play is True or source_deck.beat_index is None
                or int(plan.get('automation_start_beat_index', plan['deck_a']['transition_start_beat_index'])) <= int(source_deck.beat_index) + 16
                or self.auto_chain_cancel.is_set()):
                self._crowd_discard_pending('Fresh playback checks rejected the staged change.')
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
                self._crowd_discard_pending('Adaptive commit failed; restored previous queue and manifest.')
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

    def _crowd_rotation_failed(self, reason):
        """Expose failures before queue commit as well as failures after it."""
        with self.action_lock:
            self._crowd_init()
            if self.crowd_pending:
                decision = deepcopy(self.crowd.get('decision') or {})
                decision.update(applicationStatus='failed', applicationError=reason)
                self.crowd_pending = None
                self.crowd_requested = None
                self.crowd_generation += 1
                self.crowd.update(status='application_failed',
                    reason=f'Plan selected but not applied: {reason}. Existing queue retained.', decision=decision)
                try:
                    self._crowd_record('application_failed', {'decision': decision})
                except OSError:
                    self.crowd['auditWarning'] = 'Application failure could not be saved.'
            self._crowd_execution_status('rotation_failed')

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
            execution_key = (envelope.get('plan_id'), state)
            if state in ('executing', 'completed', 'cancelled', 'invalidated') and execution_key != getattr(self, '_memory_execution_key', None):
                self._memory_execution_key = execution_key
                plan = self.selection.get('next_executable_plan', {})
                tracks = [plan.get(k, {}).get('audio_sha256') for k in ('deck_a', 'deck_b')]
                payload = {'setRunId': plan.get('set_run_id', self._run_id()), 'planId': execution_key[0], 'status': state,
                    'requestId': self.selection.get('meta_origin', {}).get('requestId'),
                    'selectedInTrackActions': deepcopy(plan.get('in_track_actions', [])),
                    'note': 'Supervisor lifecycle confirmation, not individual action telemetry.'}
                threading.Thread(target=self._memory().record,
                    args=('execution', payload, [t for t in tracks if t], time.time()), daemon=True).start()
            if last and str(envelope.get('plan_id', '')).startswith(last['planId']) and state in ('executing', 'completed', 'cancelled', 'invalidated') and last.get('executionStatus') != state:
                last['executionStatus'] = state
                # File I/O stays off the observer thread.
                threading.Thread(target=self._crowd_record, args=(state, {'decision': deepcopy(last)}), daemon=True).start()
        finally:
            self.action_lock.release()

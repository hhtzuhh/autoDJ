"""Read-only, process-local evidence for the agent-flow display."""
from copy import deepcopy
import time
from uuid import uuid4


class LiveFlowTelemetry:
    def __init__(self):
        self.session_id = str(uuid4())
        self.started_at = time.time() * 1000
        self.observed_at = None
        self.last_execution = None
        self.completed = set()

    def observe(self, supervisor):
        self.observed_at = time.time() * 1000
        manager = getattr(supervisor, 'manager', None)
        envelope = getattr(manager, 'envelope', None)
        if envelope is None:
            return
        status, plan_id = str(envelope.status), str(envelope.plan_id)
        if status in {'executing', 'completed', 'invalidated', 'cancelled'}:
            previous = self.last_execution or {}
            if (previous.get('planId'), previous.get('status')) != (plan_id, status):
                self.last_execution = {'planId': plan_id, 'status': status, 'observedAt': self.observed_at}
        if status == 'completed':
            self.completed.add(plan_id)

    def snapshot(self, supervisor, *, continuous=False, activating=False, rotating=False):
        manager = getattr(supervisor, 'manager', None)
        envelope = getattr(manager, 'envelope', None)
        executor = getattr(supervisor, 'executor', None)
        return {
            'sessionId': self.session_id, 'startedAt': self.started_at,
            'observerUpdatedAt': self.observed_at,
            'ownedPlanId': str(envelope.plan_id) if envelope else None,
            'ownedPlanStatus': str(envelope.status) if envelope else None,
            'executorRunning': executor is not None and executor.poll() is None,
            'continuousSetActive': bool(continuous), 'activationInProgress': bool(activating),
            'rotationInProgress': bool(rotating), 'completedTransitions': len(self.completed),
            'lastExecution': deepcopy(self.last_execution),
        }

import type {MemoryContext, MemoryEvent} from './PlanningMemory';
import type {InTrackAction, TransitionPlan, Track} from './types';

export interface WindowEdge {
  sourceTrack: string; targetTrack: string; technique?: string;
  sourceExitBeat?: number; targetEntryBeat?: number; bars?: number;
  transitionActions?: TransitionPlan["performanceActions"] | null; inTrackActions?: InTrackAction[] | null;
}
export interface SetRun {id: string; number: number; startedAt: string; status: string; planId: string}
export const belongsToRun = (row: {setRunId?: string | null}, scope: string) => scope === 'all' || (row.setRunId ?? '') === scope;

export interface CrowdDecision {
  inTrackExecution?: {started: string[]; completed: string[]};
  requestedChange?: 'queue_order' | 'in_track_actions'; outcomeReason?: string;
  triggerGoal?: string | null; triggerSource?: string | null; planningScope?: string;
  setRunId?: string | null; origin?: 'rolling'; requestId?: string;
  sessionId?: string; recordedAt?: number; updatedAt?: number; historyStatus?: string;
  reason: string; model: string; before: string[]; after: string[]; queueChanged: boolean;
  lockedThrough: number; sourceTrack: string; targetTrack: string; technique: string;
  bassSwap: boolean; bars: number; planId: string; observationId: string; executionStatus?: string;
  memoryContext?: MemoryContext; inTrackActions?: InTrackAction[] | null; transitionActions?: unknown[][];
  applicationStatus?: string; applicationError?: string;
  changeSummary?: {beforeWindow: WindowEdge[]; afterWindow: WindowEdge[];
    observationSummary?: string; capturedAt?: string; selectedAt?: string};
}
export interface PlanningAttempt {
  recoveredBy?: string; recoveredAt?: number;
  id: string; setRunId?: string | null; sessionId: string; startedAt?: number; updatedAt: number;
  status: string; scope?: string; model: string; detail?: string | null;
  goal?: string; signalSource?: string; observationId?: string; requestSaved: boolean; responseReceived: boolean;
  rawText?: string; finishReason?: string; modelReason?: string | null;
  transitionFunctions?: string[][] | null; inTrackCount?: number | null;
}
export function planningAttemptStatus(row: PlanningAttempt, sessionId?: string): string {
  if (row.recoveredBy && (row.status === 'timeout' || row.status === 'failed')) return 'Earlier failure · later background plan ready';
  if (row.status === 'discarded') return 'Response discarded · not applied';
  if (row.status === 'timeout') return 'Timed out · not applied';
  if (row.status === 'failed') return 'Planning failed · not applied';
  if (row.status === 'calling') return row.sessionId === sessionId ? 'Calling Meta…' : 'Interrupted · outcome unconfirmed';
  return 'Response received · application unconfirmed';
}
export function planningLogRows(state?: AdaptiveState | null) {
  return [
    ...decisionRows(state).map(row => ({kind: 'decision' as const, row, at: row.recordedAt ?? (Date.parse(row.changeSummary?.selectedAt ?? '') / 1000 || 0)})),
    ...(state?.planningAttempts ?? []).map(row => ({kind: 'attempt' as const, row, at: row.updatedAt})),
  ].sort((a,b) => b.at - a.at);
}
export interface AdaptiveState {
  observing?: boolean; observationReason?: string | null;
  request?: {jobId: string; setRunId?: string | null; capturedAt: string; goal: string} | null;
  planningAttempts?: PlanningAttempt[];
  setRun?: SetRun | null; setRuns?: SetRun[];
  enabled: boolean; status: string; reason: string; decision?: CrowdDecision; lastApplied?: CrowdDecision;
  memory?: {episodes?: import("./memoryEpisodes").MemoryEpisode[]; sessionId: string; sessionEvents: number; historicalEvents: number; recent: MemoryEvent[]; history?: MemoryEvent[]; setRunId?: string | null; warning?: string | null; storage: string};
  observation?: {summary: string; goal: string | null; source: string};
  decisionHistory?: CrowdDecision[];
}

export function reorderProgress(state: AdaptiveState | null | undefined, runId: string | undefined, currentOrder: string[]) {
  const request = state?.request;
  if (!state || !request || request.goal !== 'reorder' || !runId || request.setRunId !== runId) return null;
  const matches = (d?: CrowdDecision) => d?.setRunId === runId && d.observationId === request.jobId;
  const applied = matches(state.lastApplied) ? state.lastApplied : undefined;
  if (applied && JSON.stringify(applied.after) === JSON.stringify(currentOrder)) {
    return {phase: 'applied', title: 'New order applied', detail: 'The performance queue now uses the new order. Audio follows the scheduled cues.', decision: applied};
  }
  const decision = matches(state.decision) ? state.decision : undefined;
  if (decision && ['discarded', 'cancelled', 'invalidated', 'application_failed'].includes(decision.historyStatus ?? '')) {
    return {phase: 'cancelled', title: 'Reorder not applied', detail: decision.outcomeReason ?? state.reason};
  }
  if (state.status === 'staged' && decision?.queueChanged) {
    if (JSON.stringify(decision.before) !== JSON.stringify(currentOrder)) {
      return {phase: 'waiting', title: 'Queue changed · awaiting revalidation', detail: 'This earlier proposal is not shown as the active order.'};
    }
    return {phase: 'pending', title: 'New order ready · pending handoff', detail: 'The new order is shown below. Preserved songs play first; the order applies at the protected handoff.', decision};
  }
  if (state.status === 'planning') return {phase: 'planning', title: 'Gesture received · planning new order…', detail: state.reason};
  if (state.status === 'queued') return {phase: 'received', title: 'Gesture received · reorder queued', detail: state.reason};
  return {phase: 'waiting', title: state.status === 'off' ? 'Reorder cancelled' : 'Reorder waiting / not applied', detail: state.reason};
}

export function songTitle(track?: Pick<Track, 'title'> & Partial<Pick<Track, 'artist'>>): string {
  if (!track) return 'Unknown song';
  let title = track.title.replace(/\s*[-–—]\s*(?:www\.)?FesliyanStudios\.com\b.*$/i, '').trim();
  const artist = track.artist?.trim();
  if (artist && artist !== 'Local analyzed track' && title.endsWith(` - ${artist}`)) title = title.slice(0, -artist.length - 3).trim();
  return title || track.title;
}

export function decisionRows(state?: AdaptiveState | null): CrowdDecision[] {
  const rows = new Map((state?.decisionHistory ?? []).map(row => [`${row.setRunId ?? ''}:${row.planId}`, row]));
  for (const row of [state?.decision, state?.lastApplied]) {
    if (row) {const key = `${row.setRunId ?? ''}:${row.planId}`; rows.set(key, {...rows.get(key), ...row});}
  }
  return [...rows.values()].sort((a,b) =>
    (b.recordedAt ?? (Date.parse(b.changeSummary?.selectedAt ?? '') / 1000 || 0))
    - (a.recordedAt ?? (Date.parse(a.changeSummary?.selectedAt ?? '') / 1000 || 0)));
}

export function decisionLogStatus(state: AdaptiveState, row: CrowdDecision): string {
  const status = row.executionStatus ?? row.historyStatus;
  if (status === 'unchanged') return 'No change · existing plan kept';
  if (status === 'selected_not_executed' && row.origin === 'rolling') return 'Plan retained · execution not confirmed';
  if (status === 'completed') return 'First handoff completed';
  if (status === 'executing') return row.sessionId && row.sessionId !== state.memory?.sessionId ? 'Execution started · final result unavailable' : 'Running · waiting for cue or mixing';
  if (status === 'cancelled' || status === 'invalidated') return 'Cancelled';
  if (status === 'rotation_failed') return 'Applied · next step failed';
  if (row.applicationStatus === 'failed' || status === 'application_failed') return 'Not applied · failed';
  if (status === 'discarded') return 'Not applied';
  if (['queue_committed', 'promoted', 'armed'].includes(status ?? '') || state.lastApplied?.planId === row.planId) return 'Applied · awaiting execution';
  return state.decision?.planId === row.planId && state.status === 'staged' ? 'Pending application' : 'Not applied · no application recorded';
}

export function changeStatus(state: AdaptiveState, decision: CrowdDecision): string {
  if (decision.historyStatus === 'unchanged') return 'No change · existing plan kept';
  if (state.lastApplied?.planId === decision.planId) {
    const status = state.lastApplied.executionStatus;
    if (status === 'completed') return 'First handoff completed';
    if (status === 'executing') return 'First routine running · may be waiting for its cue';
    if (status === 'cancelled' || status === 'invalidated' || status === 'rotation_failed') return `Applied · ${status.replaceAll('_', ' ')}`;
    return 'Applied to queue · handoff not yet confirmed';
  }
  if (decision.applicationStatus === 'failed') return 'Not applied · failed';
  return state.status === 'staged' ? 'Proposed · waiting for current handoff' : 'Not applied';
}

// A pure reorder has the same identities. Loading another set is not a reorder.
export function reorderedIds(before: string[], after: string[]): string[] {
  if (before.length !== after.length || new Set(before).size !== before.length
      || after.some(id => !before.includes(id))) return [];
  return after.filter((id, index) => before[index] !== id);
}

// Compare choices, not only the technique name: cue / BPM / duration matter too.
export function decisionChanges(row: CrowdDecision): {transition: string; inTrack: string} {
  const summary = row.changeSummary;
  if (!summary) return {transition: 'Transition · comparison unavailable',
    inTrack: row.inTrackActions?.length ? `In-track · ${row.inTrackActions.length} selected` : Array.isArray(row.inTrackActions) ? 'In-track · none selected' : 'In-track · not recorded'};
  const signature = (edge: WindowEdge) => JSON.stringify([edge.sourceTrack, edge.targetTrack,
    edge.technique, edge.sourceExitBeat, edge.targetEntryBeat, edge.transitionActions]);
  const changed = summary.afterWindow.filter((edge, i) => !summary.beforeWindow[i] || signature(edge) !== signature(summary.beforeWindow[i])).length;
  const count = summary.afterWindow.reduce((n, e) => n + (e.inTrackActions?.length ?? 0), 0);
  const beforeActions = summary.beforeWindow.flatMap(e => e.inTrackActions ?? []);
  const afterActions = summary.afterWindow.flatMap(e => e.inTrackActions ?? []);
  const recorded = summary.afterWindow.every(e => Array.isArray(e.inTrackActions));
  return {transition: !summary.beforeWindow.length ? 'Transition · new plan (no prior comparison)'
    : changed ? `Transition · ${changed} future handoff${changed === 1 ? '' : 's'} changed` : 'Transition · unchanged',
    inTrack: count ? JSON.stringify(beforeActions) === JSON.stringify(afterActions)
      ? `In-track · unchanged (${count} planned)` : `In-track · ${count} selected / updated`
      : recorded ? beforeActions.length ? 'In-track · removed from the future plan' : 'In-track · none selected' : 'In-track · not recorded'};
}

export function decisionTrigger(row: Pick<CrowdDecision, 'origin' | 'planningScope' | 'triggerGoal' | 'triggerSource'>): string {
  if (row.origin === 'rolling') return row.planningScope === 'current_plan'
    ? 'Trigger · initial / current-plan request' : row.planningScope === 'future_buffer' ? 'Trigger · automatic rolling refresh' : 'Trigger · rolling planner (earlier trigger details unavailable)';
  if (row.triggerSource === 'gesture') {
    if (row.triggerGoal === 'reorder') return 'Trigger · number 1 gesture (reorder)';
    if (row.triggerGoal === 'build') return 'Trigger · arms-up gesture (more in-track effect)';
    if (row.triggerGoal === 'ease') return 'Trigger · crossed-arms gesture (less in-track effect)';
  }
  if (row.triggerSource === 'natural') return `Trigger · repeated natural movement (${row.triggerGoal ?? 'unknown'})`;
  return 'Trigger · webcam feedback';
}

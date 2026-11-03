import type {Track} from './types';
import type {AdaptiveState, PlanningAttempt} from './crowdChanges';
import {planningLogRows, planningAttemptStatus, decisionLogStatus, songTitle, decisionChanges, decisionTrigger} from './crowdChanges';
import {EdgeSummary} from './CrowdPlanChange';
import {InTrackActions} from './InTrackActions';
import {PlanningMemory} from './PlanningMemory';

function AttemptRow({row, sessionId}: {row: PlanningAttempt; sessionId?: string}) {
  return <li className="crowd-decision-row">
    <div className="crowd-decision-row__header"><time>{new Date(row.updatedAt * 1000).toLocaleTimeString('en-GB')}</time>
      <strong>{planningAttemptStatus(row, sessionId)}</strong></div>
    <p className="crowd-note">{row.setRunId ?? 'Unassigned'} · {row.scope === 'crowd_adaptive' ? 'Webcam request' : 'Rolling planner'}{row.goal && ` · ${row.goal} signal`}</p>
    <p className="crowd-note">{decisionTrigger({origin: row.scope === 'crowd_adaptive' ? undefined : 'rolling', planningScope: row.scope, triggerGoal: row.goal, triggerSource: row.signalSource})}</p>
    <p>{row.detail === 'RuntimeError' && row.status === 'discarded' ? 'Active plan or camera context changed before application.' : row.detail ?? 'This attempt has no accepted plan recorded.'}</p>
    <p className="plan-action-heading">{row.responseReceived ? 'Meta response received · not an executable plan' : 'No saved Meta response'}</p>
    {row.transitionFunctions && <p>Transition response: {row.transitionFunctions.map(edge => edge.map(fn => fn.replaceAll('_', ' ')).join(' + ')).join(' → ')}</p>}
    {row.inTrackCount != null && <p className="plan-action-heading">In-track response · {row.inTrackCount === 0 ? 'none selected' : `${row.inTrackCount} proposed`} · not applied</p>}
    {row.modelReason && <p>{row.modelReason}</p>}
    <details><summary>Attempt details</summary>
      <p className="crowd-note">Request {row.id} · {row.model}{row.observationId && ` · Observation ${row.observationId.slice(0,8)}`}</p>
      {row.startedAt && <p>Started {new Date(row.startedAt * 1000).toLocaleTimeString('en-GB')}</p>}
      <p className="crowd-note">Saved model output may be unvalidated or incomplete. It was not applied by this attempt.</p>
      {row.rawText && <details><summary>Raw model output · {row.finishReason ?? 'unknown finish status'}</summary><pre>{row.rawText}</pre></details>}
    </details>
  </li>;
}

export function CrowdDecisionLog({adaptive, tracks}: {adaptive?: AdaptiveState | null; tracks: (Pick<Track, 'id' | 'title'> & Partial<Pick<Track, 'artist'>>)[]}) {
  const rows = planningLogRows(adaptive);
  const title = (id: string) => songTitle(tracks.find(track => track.id === id));
  return <article className="crowd-region crowd-decision-log">
    <h3>3 · AI decisions</h3>
    <p className="crowd-note">{adaptive?.enabled ? adaptive.reason : 'Adaptive DJ off · decisions and unsuccessful attempts remain below.'}</p>
    {!rows.length && <p className="crowd-note">No planning requests or decisions recorded in this set run yet.</p>}
    <ol aria-label="Webcam decision history">
      {rows.map(entry => {
        if (entry.kind === 'attempt') return <AttemptRow key={`attempt:${entry.row.setRunId ?? ''}:${entry.row.id}`} row={entry.row} sessionId={adaptive?.memory?.sessionId} />;
        const row = entry.row;
        const selectedAt = row.changeSummary?.selectedAt ?? (row.recordedAt ? new Date(row.recordedAt * 1000).toISOString() : undefined);
        return <li key={`${row.setRunId ?? ''}:${row.planId}`} className="crowd-decision-row">
          <div className="crowd-decision-row__header">
            <time dateTime={selectedAt}>{selectedAt ? new Date(selectedAt).toLocaleTimeString('en-GB') : 'Earlier decision'}</time>
            <strong>{decisionLogStatus(adaptive!, row)}</strong>
          </div>
          <p className="crowd-note">{row.setRunId ?? 'Unassigned'} · {row.origin === 'rolling' ? 'Rolling planner' : 'Webcam request'}</p>
          <p className="crowd-note">{decisionTrigger(row)}</p>
          {row.requestedChange && <p className="crowd-note">Request · {row.requestedChange === 'queue_order' ? 'Change remaining song order' : 'Adjust in-track actions during future songs'}</p>}
          {row.outcomeReason && <p>{row.outcomeReason}</p>}
          {row.origin === 'rolling' && row.observationId && <p className="crowd-note">Context · recent webcam observations included; no new gesture required.</p>}
          <p className="plan-action-heading">{decisionChanges(row).transition}</p>
          <p className="plan-action-heading">{decisionChanges(row).inTrack}</p>
          {row.inTrackExecution && (row.inTrackExecution.started.length > 0 || row.inTrackExecution.completed.length > 0) &&
            <p className="crowd-note">Confirmed in-track execution · first song: {row.inTrackExecution.started.length} started / {row.inTrackExecution.completed.length} completed
              {row.inTrackExecution.completed.length > 0 && ` · ${row.inTrackExecution.completed.map(fn => fn.replaceAll('_', ' ')).join(', ')}`}</p>}
          <div className="crowd-decision-order">
            {row.queueChanged && <p><span>Before</span>{row.before.slice(row.lockedThrough + 1).map(title).join(' → ')}</p>}
            <p><span>{row.queueChanged ? 'Selected' : 'Order kept'}</span>{row.after.slice(row.lockedThrough + 1).map(title).join(' → ')}</p>
          </div>
          <details><summary>Decision details</summary>
            <p>{row.reason}</p>
            {row.applicationError && <p className="crowd-warning">{row.applicationError}</p>}
            <p className="crowd-note">{row.observationId ? `Observation ${row.observationId.slice(0, 8)} ·` : ''} {row.model}. Completion refers to the first handoff, not the whole window.</p>
            <h3>Transition</h3><p>{title(row.sourceTrack)} → {title(row.targetTrack)} · {row.technique.replaceAll('_', ' ')}</p>
            <InTrackActions actions={row.inTrackActions} />
            <PlanningMemory context={row.memoryContext} />
            {row.changeSummary && <details><summary>Transition / in-track · before and after</summary>
              <h4>Before</h4>{row.changeSummary.beforeWindow.map((edge, i) => <EdgeSummary key={i} edge={edge} title={title} />)}
              <h4>Selected</h4>{row.changeSummary.afterWindow.map((edge, i) => <EdgeSummary key={i} edge={edge} title={title} />)}
            </details>}
            <details><summary>Action parameters</summary><pre>{JSON.stringify(row.transitionActions, null, 2)}</pre></details>
          </details>
        </li>;
      })}
    </ol>
  </article>;
}

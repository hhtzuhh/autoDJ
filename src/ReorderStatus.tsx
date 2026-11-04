import type {AdaptiveState} from './crowdChanges';
import {reorderProgress, songTitle} from './crowdChanges';
import type {Track} from './types';

export function ReorderStatus({adaptive, tracks, runId, connected}: {
  adaptive?: AdaptiveState | null; tracks: Track[]; runId?: string; connected: boolean;
}) {
  const progress = reorderProgress(adaptive, runId, tracks.map(t => t.id));
  if (!progress) return null;
  const decision = progress.decision;
  return <aside className={`reorder-status reorder-status--${progress.phase}`} aria-label="Gesture reorder status">
    <div role="status" aria-live="polite">
      <strong>{connected ? progress.title : 'Disconnected · reorder status unconfirmed'}</strong>
      <p>{connected ? progress.detail : 'Reconnect to confirm whether the request was applied.'}</p>
      {adaptive?.observing === false && ['received', 'planning', 'pending'].includes(progress.phase) &&
        <p>Camera observation is off. This accepted request continues; Take over cancels automation.</p>}
    </div>
    {connected && decision && progress.phase === 'pending' && <>
      <p className="reorder-status__label">Proposed order · not yet applied</p>
      <ol aria-label="Proposed song order">
        {decision.after.map((id, index) => <li key={id}>
          <span>{songTitle(tracks.find(t => t.id === id))}</span>
          <small>{index <= decision.lockedThrough ? 'Preserved' : 'Pending'}
            {decision.before.indexOf(id) !== index ? ` · ${decision.before.indexOf(id) + 1} → ${index + 1}` : ''}</small>
        </li>)}
      </ol>
    </>}
  </aside>;
}

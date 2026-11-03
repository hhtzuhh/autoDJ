import type {AdaptiveState, WindowEdge} from './crowdChanges';
import {changeStatus, songTitle, decisionChanges} from './crowdChanges';
import type {Track} from './types';

export function EdgeSummary({edge, title}: {edge: WindowEdge; title: (id: string) => string}) {
  return <div className="change-edge">
    <strong>{title(edge.sourceTrack)} → {title(edge.targetTrack)}</strong>
    <span>{edge.technique?.replaceAll('_', ' ') ?? 'Technique not recorded'}
      {edge.bars != null && edge.bars > 0 && ` · ${edge.bars} bars`}</span>
    <small>Handoff beat {edge.sourceExitBeat ?? '—'} · target cue {edge.targetEntryBeat ?? '—'}</small>
    <div className="plan-action-heading">Transition</div>
    {edge.transitionActions == null ? <small>Action parameters not recorded.</small> : <ul>
      {edge.transitionActions.map((a, i) => <li key={i}>{a.function.replaceAll('_', ' ')}
        {a.deck && ` · ${a.deck}`} · beat {a.start_beat}
        {a.target_bpm != null && ` → ${a.target_bpm} BPM`}
        {a.duration_beats != null && ` · ${a.duration_beats} beats`}
        {a.curve && ` · ${a.curve.replaceAll('_', ' ')}`}
        {a.fade_out_seconds != null && ` · fade ${a.fade_out_seconds}s`}</li>)}</ul>}
    <div className="plan-action-heading">In-track</div>
    {edge.inTrackActions == null ? <small>Not recorded.</small> : edge.inTrackActions.length === 0
      ? <small>None selected.</small> : <ul>{edge.inTrackActions.map((a, i) => <li key={i}>
        {a.function.replaceAll('_', ' ')} · beat {a.start_beat} · {a.duration_beats} beats · depth {a.depth}
      </li>)}</ul>}
  </div>;
}

export function CrowdPlanChange({adaptive, tracks}: {adaptive?: AdaptiveState | null; tracks: Track[]}) {
  const decision = adaptive?.decision ?? adaptive?.lastApplied;
  if (!adaptive || !decision) return null;
  const title = (id: string) => songTitle(tracks.find(t => t.id === id));
  const summary = decision.changeSummary;
  const before = decision.before.slice(decision.lockedThrough);
  const after = decision.after.slice(decision.lockedThrough);
  return <aside className="crowd-plan-change" aria-label="Webcam plan change">
    <span className="eyebrow">Webcam → future plan</span>
    <p className="change-status" role="status">{changeStatus(adaptive, decision)}</p>
    <p>{summary?.observationSummary ?? 'See the linked observation for camera evidence.'}</p>
    <small>Observation {decision.observationId.slice(0, 8)}{summary?.capturedAt && ` · ${new Date(summary.capturedAt).toLocaleTimeString('en-GB')}`}</small>
    {decision.queueChanged ? <div className="change-order">
      <p><b>Before · next handoff</b>{before.slice(0, 2).map(title).join(' → ')}</p>
      <p><b>After{adaptive.lastApplied?.planId === decision.planId ? '' : ' (proposed)'}</b>{after.slice(0, 2).map(title).join(' → ')}</p>
    </div> : <p className="panel-details__note">Song order retained. Compare transition and in-track choices below.</p>}
    <p className="plan-action-heading">{decisionChanges(decision).transition}</p>
    <p className="plan-action-heading">{decisionChanges(decision).inTrack}</p>
    <p>{decision.reason}</p>
    <details><summary>Compare plans · before / after</summary>
      <p className="panel-details__note">The playing handoff stays locked. This compares the future window; completion refers only to its first handoff.</p>
      {decision.queueChanged && <div className="change-order"><p><b>Previous remaining order</b>{before.map(title).join(' → ')}</p><p><b>Selected remaining order</b>{after.map(title).join(' → ')}</p></div>}
      {summary ? <div className="plan-comparison">
        <section><h4>Before · retained plan</h4>{summary.beforeWindow.length
          ? summary.beforeWindow.map((e,i) => <EdgeSummary key={i} edge={e} title={title} />)
          : <p>No prior future plan was recorded.</p>}</section>
        <section><h4>After · webcam decision</h4>{summary.afterWindow.map((e,i) => <EdgeSummary key={i} edge={e} title={title} />)}</section>
      </div> : <p>Before/after action snapshots were not saved for this older decision.</p>}
    </details>
  </aside>;
}

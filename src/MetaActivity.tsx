import { useEffect, useState } from "react";
import type { CopilotState } from "./types";
import {rollingPlannerView} from "./rollingPlanner";

const labels = {
  calling: "Calling Meta",
  succeeded: "Succeeded",
  timeout: "Timed out",
  failed: "Failed",
  discarded: "Discarded · plan changed",
};

export function RollingPlanner({ state }: { state: CopilotState }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const calls = state.plan.metaCalls ?? [];
  const view = rollingPlannerView(state);
  const {ready, futureDepth: targetDepth, latestCall} = view;
  const elapsed = latestCall ? Math.max(0, Math.floor(((latestCall.finishedAt ?? now) - latestCall.startedAt) / 1000)) : 0;
  const offline = state.dataSource === "live" && !state.connected;
  const status = state.plan.futurePlanStatus ?? "idle";
  const timing = state.plan.refillTiming;
  const error = state.plan.futurePlanError;
  const noCandidates = error?.includes("no_feasible_future");
  const phaseLabels = {
    preparing_request: "Preparing request", waiting_for_response: "Waiting for API response",
    reading_response: "Reading response", validating_response: "Validating selection", finished: "Finished",
  };
  const renderCall = (call: (typeof calls)[number]) => {
    const positions = call.trackIds.map(id => state.queue.findIndex(track => track.id === id));
    const edges = positions.slice(0, -1).map((position, index) =>
      position < 0 || positions[index + 1] < 0 ? "Outside active queue" : `${position + 1}→${positions[index + 1] + 1}`,
    ).join(" · ");
    const route = call.trackIds.map(id => state.queue.find(track => track.id === id)?.title ?? "Track outside active queue").join(" → ");
    const elapsed = Math.max(0, Math.floor(((call.finishedAt ?? now) - call.startedAt) / 1000));
    return <li key={call.id} id={call.id} className={`meta-call meta-call--${call.status}`}>
      <div className="meta-call__heading">
        <strong>{call.scope === "future_buffer" ? "Future window" : call.scope === "crowd_adaptive" ? "Crowd adaptation" : "Initial / current window"} · {edges}</strong>
        <span>{labels[call.status]}</span>
      </div>
      <p className="meta-call__route">{route}</p>
      <div className="meta-call__timing">
        <time dateTime={new Date(call.startedAt).toISOString()}>{new Date(call.startedAt).toLocaleTimeString("en-GB")}</time>
        <span>{elapsed}s{call.status === "calling" ? ` / ${call.timeoutSeconds}s timeout` : " elapsed"}</span>
        {call.attempt > 1 && <span>Attempt {call.attempt}</span>}
        <span>{call.model}</span>
        <span title={call.id}>Request {call.id.slice(-8)}</span>
        {call.reasoningEffort && <span>Reasoning: {call.reasoningEffort}</span>}
      </div>
      {call.status === "calling" && call.phase && <p className="meta-call__reason">{phaseLabels[call.phase]}</p>}
      {call.reasoningTokens != null && <p className="meta-call__reason">
        {call.reasoningTokens} reasoning tokens · {call.completionTokens} total output tokens
        {call.responseHeadersSeconds != null && ` · response headers after ${call.responseHeadersSeconds}s`}
      </p>}
      {call.status === "timeout" && <p className="meta-call__reason">No response within {call.timeoutSeconds}s. Existing valid plans were kept.</p>}
      {call.status === "discarded" && <p className="meta-call__reason">The active plan or queue changed before this result could be applied.</p>}
      {call.status === "failed" && <p className="meta-call__reason">{call.detail ?? "The response could not be used."}</p>}
    </li>;
  };
  return <section className="panel rolling-panel" aria-label="Rolling planner">
    <div className="panel__header">
      <div><span className="eyebrow">Planning ahead</span><h2>Rolling planner</h2></div>
      <strong className={`count-badge ${ready < targetDepth ? "meta-activity__short" : "meta-activity__full"}`}>
        {offline ? "Disconnected" : !view.tracks.length ? "Not started" : targetDepth ? `${ready}/${targetDepth} future ready` : "Queue tail"}
      </strong>
    </div>
    {state.dataSource === "simulation" && <p className="meta-activity__warning">Simulation · no live Meta requests.</p>}
    <p className="meta-activity__summary" aria-live="polite">
      {offline ? "Showing the last observed planning state."
        : view.activeCall ? "Meta is planning a window. Retained decisions are shown below."
        : !view.tracks.length ? "Build a performance plan in Agent flow to start the first window."
        : targetDepth === 0 ? "End of queue · no later transitions to prepare."
        : status === "degraded" ? `Future planning needs attention${state.plan.futureRetryInSeconds ? ` · retry in ${Math.ceil(state.plan.futureRetryInSeconds)}s` : ""}.`
        : ready === targetDepth ? "Future transitions are ready for promotion."
        : "Waiting for the next planning opportunity."}
    </p>
    {view.tracks.length > 0 && <div className="rolling-window">
      <span className="eyebrow">{view.windowLabel}</span>
      <div className="rolling-window__route" aria-label="Planning window songs">
        {view.tracks.map((track, index) => <span key={`${track.id}-${index}`} title={track.title}>
          {index > 0 && <i aria-hidden="true">→</i>}<b>{track.position || "—"}</b><span>{track.title}</span>
        </span>)}
      </div>
      <ol className="rolling-window__edges" aria-label="Transition planning status">
        {view.edges.map(edge => <li key={`${edge.source.id}-${edge.target.id}`}>
          <span title={`${edge.source.title} → ${edge.target.title}`}>Song {edge.source.position || "—"} → {edge.target.position || "—"}</span>
          <strong className={`rolling-state rolling-state--${edge.tone}`}>{edge.label}</strong>
        </li>)}
      </ol>
      {state.plan.activeTransitionLocked && <p className="panel-details__note">Current handoff locked · replanning applies to later transitions.</p>}
    </div>}
    {latestCall ? <div className={`meta-call meta-call--${latestCall.status} latest-call`} aria-label="Latest Meta request">
      <div className="meta-call__heading"><strong>Latest Meta request</strong><span>{labels[latestCall.status]}</span></div>
      <div className="meta-call__timing"><span>{elapsed}s elapsed</span><span>{latestCall.model}</span></div>
      {latestCall.status === "calling" && latestCall.phase && <p className="meta-activity__summary">{phaseLabels[latestCall.phase]}</p>}
    </div> : <p className="meta-activity__summary">No Meta requests in this session.</p>}
    {noCandidates && <p className="meta-activity__warning">No feasible future window yet. No Meta request was sent for this attempt.</p>}
    {timing?.status === "at_risk" && targetDepth > 0 && <p className="meta-activity__warning">Short planning window · the future buffer may run low.</p>}
    <details className="panel-details">
      <summary>Request history ({calls.length})</summary>
      <p className="panel-details__note">These are API attempts. Transition readiness is shown above; a successful request does not mean playback executed.</p>
      {timing && <p className="panel-details__note">Refill runway: {timing.minimumRunwaySeconds ?? "unknown"}s · timeout and retry budget: {timing.budgetSeconds}s.</p>}
      {error && <p className="meta-activity__warning">{error}</p>}
      {calls.length > 0 && <ol className="meta-activity__calls">{calls.map(renderCall)}</ol>}
    </details>
  </section>;
}

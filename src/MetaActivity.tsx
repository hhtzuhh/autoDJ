import { useEffect, useState } from "react";
import type { CopilotState } from "./types";

const labels = {
  calling: "Calling Meta",
  succeeded: "Applied",
  timeout: "Timed out",
  failed: "Failed",
  discarded: "Discarded · plan changed",
};

export function MetaActivity({ state }: { state: CopilotState }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  if (state.dataSource !== "live") return null;
  const calls = state.plan.metaCalls ?? [];
  const sourceIndex = state.queue.findIndex(track => track.id === state.plan.sourceTrackId);
  const targetDepth = sourceIndex < 0 ? 0 : Math.min(3, state.queue.length - sourceIndex - 2);
  const ready = state.transitions.filter(edge => {
    const index = state.queue.findIndex(track => track.id === edge.sourceTrackId);
    return index > sourceIndex && index <= sourceIndex + targetDepth
      && edge.status === "ready" && edge.selectedBy === "meta";
  }).length;
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
        <strong>{call.scope === "future_buffer" ? "Buffer" : "Current plan"} · {edges}</strong>
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
  return <section className="meta-activity" aria-label="Meta call activity">
    <div className="meta-activity__header">
      <h3>Meta calls</h3>
      <strong className={ready < targetDepth ? "meta-activity__short" : "meta-activity__full"}>
        Future buffer {ready}/{targetDepth} ready
      </strong>
    </div>
    <p className="meta-activity__summary" aria-live="polite">
      {targetDepth === 0 ? "Queue tail · no later transitions to plan."
        : status === "planning" ? "Planning in the background · existing valid plans stay available."
        : status === "degraded" ? `Buffer degraded · ${state.plan.futureRetryInSeconds ? `retry in ${Math.ceil(state.plan.futureRetryInSeconds)}s` : "waiting for the next valid planning opportunity"}.`
        : status === "ready" ? "The future window is ready."
        : "No background request running."}
    </p>
    <p className="meta-activity__summary">Request history below records attempts, not whether an existing transition plan is still valid. See each queue edge for its retained decision.</p>
    {noCandidates && <p className="meta-activity__warning">No time-consistent local window was found. No Meta request was sent for this attempt.</p>}
    {timing?.status === "at_risk" && targetDepth > 0 && <p className="meta-activity__warning">
      Short refill window: about {timing.minimumRunwaySeconds}s available vs {timing.budgetSeconds}s reserved for a timeout and retry. Buffer may run low.
    </p>}
    {calls.length === 0 ? <p className="meta-activity__summary">No Meta calls recorded in this bridge session.</p>
      : <><ol className="meta-activity__calls">{calls.slice(0, 3).map(renderCall)}</ol>
        {calls.length > 3 && <details><summary>Earlier calls ({calls.length - 3})</summary>
          <ol className="meta-activity__calls">{calls.slice(3).map(renderCall)}</ol>
        </details>}</>}
  </section>;
}

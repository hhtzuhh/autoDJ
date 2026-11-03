import type {CopilotState, TransitionEdgeStatus} from "./types";

export function rollingPlannerView(state: CopilotState) {
  const p = state.plan;
  const sourceIndex = state.queue.findIndex(t => t.id === p.sourceTrackId);
  const futureDepth = sourceIndex < 0 ? 0 : Math.max(0, Math.min(3, state.queue.length - sourceIndex - 2));
  const futurePairs = state.queue.slice(sourceIndex + 1, sourceIndex + futureDepth + 2);
  const ready = sourceIndex < 0 ? 0 : futurePairs.slice(0, -1).filter((source, index) =>
    state.transitions.some(e => e.sourceTrackId === source.id && e.targetTrackId === futurePairs[index + 1].id
      && e.selectedBy === "meta" && ["ready", "armed", "executing"].includes(e.status)),
  ).length;
  const calls = p.metaCalls ?? [];
  const activeCall = calls.find(c => c.status === "calling");
  const futureIds = p.futurePlanningWindowTrackIds ?? [];
  const decisionIds = p.planningWindowTrackIds ?? [];
  const ids = activeCall?.trackIds ?? (futureIds.length ? futureIds : decisionIds.length ? decisionIds
    : sourceIndex < 0 ? [] : state.queue.slice(sourceIndex, sourceIndex + 4).map(t => t.id));
  const tracks = ids.map(id => ({id, position: state.queue.findIndex(t => t.id === id) + 1,
    title: state.queue.find(t => t.id === id)?.title ?? `Track ${id.slice(0, 8)}`}));
  const edges = tracks.slice(0, -1).map((source, index) => {
    const target = tracks[index + 1];
    const edge = state.transitions.find(e => e.sourceTrackId === source.id && e.targetTrackId === target.id);
    const current = source.id === p.sourceTrackId && target.id === p.targetTrackId;
    const status = current ? p.status : edge?.status;
    const selectedBy = current ? p.selectedBy : edge?.selectedBy;
    const terminal = status === "cancelled" || status === "invalidated" || status === "expired";
    const needsAttention = terminal || status === "blocked" || status === "needs_human";
    const metaReady = selectedBy === "meta" && ["ready", "armed", "executing", "completed", "planned", "feasible"].includes(status ?? "");
    const planning = activeCall?.trackIds.some((id, i) => id === source.id && activeCall.trackIds[i + 1] === target.id);
    const labels: Partial<Record<TransitionEdgeStatus, string>> = {
      ready: "Ready", armed: "Armed", executing: "Executing", completed: "Completed", planned: "Planned", feasible: "Planned",
      cancelled: "Cancelled", invalidated: "Invalidated", expired: "Expired", blocked: "Blocked", needs_human: "Needs attention",
    };
    return {source, target,
      label: needsAttention ? labels[status as TransitionEdgeStatus] : metaReady ? labels[status as TransitionEdgeStatus]
        : planning || status === "planning" || status === "replanning" ? "Planning"
        : selectedBy === "fixture" ? "Demo plan" : selectedBy === "deterministic_fallback" ? "Local standby" : "Awaiting Meta",
      tone: needsAttention ? "warning" : metaReady ? "ready" : planning ? "planning" : "pending",
    };
  });
  return {tracks, edges, ready, futureDepth, activeCall, latestCall: activeCall ?? calls[0],
    windowLabel: activeCall ? "Planning window" : futureIds.length ? "Future window" : "Latest decision window"};
}

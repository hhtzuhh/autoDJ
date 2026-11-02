import type {CopilotState, PreparedTrack, WebcamFlowState} from "./types";

export type NodeStatus = "done" | "running" | "waiting" | "error" | "idle" | "skipped" | "stale";
export type FlowNode = {id: string; title: string; owner: string; status: NodeStatus; label: string; detail: string; evidence: unknown};
export type CallRecord = {id: string; scope: string; model: string; status: string; startedAt: number; finishedAt?: number | null; seconds?: number; tokens?: number | null; detail?: string | null};
const node = (id: string, title: string, owner: string, status: NodeStatus, label: string, detail: string, evidence: unknown): FlowNode =>
  ({id, title, owner, status, label, detail, evidence});

export function analysisTrackRows(tracks: PreparedTrack[]) {
  return tracks.map((track, index) => {
    const run = track.analysisRun;
    const existing = track.analysisStatus === "ready" && ["cache", "available"].includes(track.analysisOrigin ?? "");
    const neural = !existing && run?.status === "completed" && run.neuralNet === "computed";
    const neuralCache = !existing && run?.neuralNet === "cache";
    const route = existing ? "metadata" : neural ? "neural" : run || track.analysisOrigin === "computed" ? "pipeline" : "unknown";
    const source = existing ? "Existing analysis metadata" : neural ? "Neural net + DSP" : neuralCache ? "Cached neural result + DSP"
      : run?.engine === "all_in_one_plus_dsp" ? "All-In-One + DSP" : run?.engine === "custom" ? "Custom analyzer"
      : track.analysisOrigin === "computed" ? "Computed analysis · engine unrecorded"
      : track.analysisStatus === "ready" ? "Existing result · origin unrecorded" : "Awaiting analyzer";
    const note = existing ? track.analysisOrigin === "cache" ? "Reused in this build; no new analysis job" : "Available on disk; original run unrecorded"
      : neural ? "Neural inference completed in the recorded job" : neuralCache ? "Network output reused; local DSP recomputed"
      : run ? "Neural inference is not confirmed until the job completes"
      : "No per-job execution evidence recorded";
    return {track, index, run, route, source, note};
  });
}

// Pure projection of observed state. No timers advance a node, and no node sends commands.
export function deriveAgentFlow(state: CopilotState, webcam: WebcamFlowState | null, disconnected = false, now = Date.now()) {
  const b = state.setBuilder;
  const live = state.dataSource === "live";
  const telemetry = state.flowTelemetry;
  const fresh = live && state.connected && !disconnected && telemetry?.observerUpdatedAt != null
    && now - telemetry.observerUpdatedAt < 10_000;
  const selectedTracks = b?.queue ?? [];
  const tracks = b?.library ?? selectedTracks;
  const readyTracks = tracks.filter(t => t.analysisStatus === "ready");
  const runningTracks = tracks.filter(t => t.analysisStatus === "analyzing");
  const failedTracks = tracks.filter(t => t.analysisStatus === "failed");
  const cached = readyTracks.filter(t => t.analysisOrigin === "cache").length;
  const available = readyTracks.filter(t => t.analysisOrigin === "available").length;
  const computed = readyTracks.filter(t => t.analysisOrigin === "computed").length;
  const edges = b?.transitions ?? [];
  const readyEdges = edges.filter(e => e.status === "analyzed" || e.status === "ready");
  const blockedEdges = edges.filter(e => e.status === "blocked");
  const calls = b?.curatorCalls ?? [];
  const currentCalls = b?.buildId ? calls.filter(c => c.buildId === b.buildId) : calls;
  const curationCalls = currentCalls.filter(c => c.stage === "curation");
  const curation = curationCalls.at(-1);
  const postRequired = edges.some(e => e.reason === "clean_break_required" || e.status === "blocked");
  const post = b?.postAnalysisPlanning;
  const preparing = b?.status === "curating";
  const preparation = [
    node("library", "Local library", "FILES + METADATA", b ? "done" : "idle", b ? `${b.catalogCount} tracks available` : "Waiting for library",
      "Reads local audio identities and file tags. Unknown genres remain unknown.", {catalogCount: b?.catalogCount ?? null, fields: ["title", "artist", "genre", "duration", "known BPM", "analysis status"]}),
    node("curation", "Select & order", "META · MUSE SPARK", preparing ? "running" : b?.error ? "error" : b?.buildPhase === "library_analysis" ? "waiting" : curation?.status === "failed" || curation?.status === "truncated" ? "error" : selectedTracks.length ? "done" : "idle",
      preparing ? "Selecting tracks…" : b?.error ? "Build blocked" : b?.buildPhase === "library_analysis" ? "Waiting for library analysis" : selectedTracks.length ? `${selectedTracks.length} tracks selected` : "Build / choose playlist",
      "Meta receives the analyzed library: measured BPM, structure and DSP summaries. Genre is from file tags; relative energy is not a cross-song energy ranking.", {request: b?.prompt, reason: b?.reason, call: curation ?? null, error: b?.error, selected: selectedTracks.map(t => t.title)}),
    node("analysis", "Analyze library", "ALL-IN-ONE + LOCAL DSP", failedTracks.length ? "error" : runningTracks.length ? "running" : readyTracks.length && readyTracks.length === tracks.length ? "done" : tracks.length ? "waiting" : "idle",
      runningTracks.length ? `${runningTracks.length} analyzing · ${readyTracks.length}/${tracks.length} ready` : `${readyTracks.length}/${tracks.length} ready${cached ? ` · ${cached} cached` : ""}${available ? ` · ${available} existing metadata` : ""}`,
      `Beat grid, structure, bar features and cue candidates. ${cached} metadata results reused in the recorded build; ${available} already available without original run history; ${computed} pipeline results computed. See the track table for confirmed neural inference versus internal cache reuse.`,
      tracks.map(t => ({track: t.title, status: t.analysisStatus, origin: t.analysisOrigin ?? "unknown", bpm: t.bpm, analysisSummary: t.analysisSummary}))),
    node("pairs", "Find transitions", "LOCAL PAIR ANALYZER", preparing ? "waiting" : blockedEdges.length ? "error" : edges.some(e => e.status === "analyzing") ? "running" : edges.length && readyEdges.length === edges.length ? "done" : tracks.length ? "waiting" : "idle",
      `${readyEdges.length}/${edges.length} pairs ready${blockedEdges.length ? ` · ${blockedEdges.length} blocked` : ""}`,
      "Tests adjacent entry/exit candidates, energy and bass overlap. Analyzed candidates are not yet executable Meta plans.", edges.map(e => ({source: e.sourceTrackId, target: e.targetTrackId, status: e.status, candidates: e.candidateCount, reason: e.reason}))),
    node("refine", "Refine the order", "META · WHEN NEEDED", preparing ? "waiting" : post === "failed" ? "error" : post === "running" ? "running" : post === "completed" ? "done" : !postRequired && edges.length && readyEdges.length === edges.length ? "skipped" : tracks.length ? "waiting" : "idle",
      post === "failed" ? "Planning failed" : post === "running" ? "Reordering from measured BPM…" : post === "completed" ? "Order validated" : !postRequired && edges.length && readyEdges.length === edges.length ? "Not required" : "Waiting for pair analysis",
      "An optional second Meta pass reduces incompatible adjacencies. It does not replace the live transition planner.", {status: post, error: b?.error, calls: currentCalls.filter(c => c.stage === "post_analysis" || !c.stage)}),
    node("export", "Export playlist", "LOCAL FILE OUTPUT", telemetry?.activationInProgress ? "running" : b?.activation ? "done" : b?.activationReady ? "waiting" : "idle",
      telemetry?.activationInProgress ? "Loading & verifying set…" : b?.activation ? "Playlist exported" : b?.activationReady ? "Ready to export" : "Waiting for prepared set",
      "Writes the Mixxx playlist. Open it and select the first row, then Start set. Exporting does not play audio.", b?.activation ? {status: b.activation.status, queueRevision: b.activation.queueRevision, trackCount: b.activation.trackCount} : null),
  ];
  // Analysis precedes curation; this branch deliberately ends at selected order.
  [preparation[1], preparation[2]] = [preparation[2], preparation[1]];
  if (b?.selectionOnly) preparation.splice(3);
  if (!live || !state.connected || disconnected) preparation.forEach(n => {n.status = "stale"; n.label = !live ? "No live preparation data" : "Last known · disconnected";});
  const p = state.plan;
  const planningCalls = p.metaCalls ?? [];
  const calling = planningCalls.filter(c => c.status === "calling");
  const owned = telemetry?.ownedPlanStatus;
  const recovery = Boolean(p.recoveryReason) || ["invalidated", "cancelled"].includes(p.status);
  const playing = state.decks.filter(d => d.playing);
  const liveNodes = [
    node("observe", "Observe", "MIXXX STATE", fresh ? "done" : "stale", fresh ? `${playing.length} deck${playing.length === 1 ? "" : "s"} playing` : live ? "No fresh observer snapshot" : "DJ simulation",
      "The local observer reads track identity, beat and human actions. No model is called to read playback state.", {revision: state.performanceRevision, observerUpdatedAt: telemetry?.observerUpdatedAt, decks: state.decks.map(d => ({deck: d.deck, title: d.track?.title, playing: d.playing, beat: d.beatIndex}))}),
    node("plan", "Choose a plan", "META · MUSE SPARK", calling.length ? "running" : p.futurePlanStatus === "degraded" ? "error" : recovery ? "waiting" : p.selectedBy === "meta" && ["ready", "armed", "executing"].includes(p.status) ? "done" : "idle",
      calling.length ? `${calling.length} model call${calling.length === 1 ? "" : "s"} in flight` : p.futurePlanStatus === "degraded" ? "Future planning degraded" : recovery ? "Waiting for recovery" : p.selectedBy === "meta" ? "Retained Meta plan" : "No new Meta plan",
      "Meta selects a bounded candidate. Calls are triggered by planning needs, not by every observer update; future plans can be prepared while music plays.", {selectedBy: p.selectedBy, planId: p.id, reason: p.reason, origin: p.metaOrigin, futureStatus: p.futurePlanStatus, futureError: p.futurePlanError}),
    node("validate", "Validate & arm", "LOCAL SUPERVISOR", recovery ? "waiting" : owned === "armed" ? "waiting" : owned === "executing" ? "done" : p.status === "ready" ? "done" : "idle",
      recovery ? "Human recovery required" : owned === "armed" ? "Armed · waiting for cue" : p.status === "ready" ? "Compiled · preflight on arm" : owned === "executing" ? "Preflight passed" : "Waiting for a valid plan",
      "Checks identity, queue revision, timing and ownership. Ready is not playing; armed waits for the scheduled beat.", {planStatus: p.status, ownedStatus: owned, queueRevision: state.queueRevision, autoArm: state.autoArmEnabled, recovery: p.recoveryReason}),
    node("execute", "Execute transition", "LOCAL AUDIO ENGINE", owned === "executing" && telemetry?.executorRunning ? "running" : owned === "armed" ? "waiting" : "idle",
      owned === "executing" && telemetry?.executorRunning ? "Executor running" : owned === "armed" ? "Waiting for cue" : playing.length ? "Music playing · no transition" : "No transition executing",
      "The local executor controls Mixxx. A model reply or committed queue alone never lights this node as executing.", {ownedPlanId: telemetry?.ownedPlanId, ownedPlanStatus: owned, executorRunning: telemetry?.executorRunning ?? false, technique: p.transitionMode, bars: p.bars}),
    node("verify", "Verify & continue", "OBSERVER → NEXT WINDOW", telemetry?.rotationInProgress ? "running" : recovery ? "waiting" : telemetry?.lastExecution?.status === "completed" ? "done" : "idle",
      telemetry?.rotationInProgress ? "Loading & verifying next track…" : recovery ? "Loop paused for human control" : telemetry?.lastExecution?.status === "completed" ? "Handoff observed complete" : "Waiting for observed completion",
      "After completion, verify the next load, promote a future plan and refill the buffer. The counter includes only completions observed in this bridge session.", {completedThisSession: telemetry?.completedTransitions ?? 0, lastExecution: telemetry?.lastExecution, continuousSetActive: telemetry?.continuousSetActive}),
  ];
  if (!fresh) liveNodes.forEach(n => {n.status = "stale"; n.label = live ? "Live evidence unavailable" : "Simulation · not live evidence";});
  const a = webcam?.adaptive;
  const j = webcam?.job;
  const visionInFlight = j?.status === "running" && j.stage.includes("Muse Spark");
  const crowd = [
    node("camera", "Capture audience", "COMPUTER WEBCAM", webcam?.error ? "error" : webcam?.running ? "running" : "idle", webcam?.running ? webcam.stage : "Camera off",
      "Six-second silent clips. Switching between these app views keeps capture mounted; hiding the browser tab stops it.", {running: webcam?.running ?? false, stage: webcam?.stage, error: webcam?.error}),
    node("vision", "Understand movement", "META · MUSE SPARK", visionInFlight ? "running" : j?.status === "failed" ? "error" : j?.status === "completed" ? "done" : j?.status === "running" ? "waiting" : "idle",
      visionInFlight ? "Video understanding in flight" : j?.status === "completed" ? "Last clip analyzed" : j?.status === "running" ? j.stage : "Waiting for a clip",
      "Describes visible activity and held demo gestures, with evidence. No emotion or music-preference inference.", {jobId: j?.id, status: j?.status, summary: j?.observation?.summary_en, cue: j?.cue, modelSeconds: j?.modelSeconds, remoteCleanup: j?.remoteFileCleanup}),
    node("policy", "Gate the feedback", "LOCAL DEMO POLICY", !a?.enabled ? "idle" : a.status === "planning" ? "running" : ["staged", "committed"].includes(a.status) ? "done" : "waiting", a?.enabled ? a.status : "Adaptive DJ off",
      a?.reason ?? "Checks visibility, two consistent natural observations or held cues, freshness, camera lease and cooldown.", {enabled: a?.enabled ?? false, status: a?.status, reason: a?.reason}),
    node("stage", "Stage a future change", "META PLAN → LOCAL HANDOFF", a?.enabled && a.status === "planning" ? "running" : a?.enabled && a.status === "staged" ? "waiting" : a?.lastApplied ? ["rotation_failed", "invalidated", "cancelled"].includes(a.lastApplied.executionStatus ?? "") ? "error" : "done" : "idle",
      a?.enabled && a.status === "staged" ? "Staged · waiting for handoff" : a?.enabled && a.status === "planning" ? "Selecting future order…" : a?.lastApplied ? `Last change: ${a.lastApplied.executionStatus ?? "queue committed"}` : "No crowd-driven change",
      "Keeps the locked transition unchanged. A staged change can expire; a queue commit is not proof of audio execution.", {decision: a?.decision, lastApplied: a?.lastApplied}),
  ];
  if (!fresh && a?.enabled) crowd.slice(2).forEach(n => {n.status = "stale"; n.label = "Waiting for live DJ evidence";});
  const records: CallRecord[] = [
    ...calls.map((c, i) => ({id: c.id ?? `curator-${c.startedAt}-${i}`, scope: c.stage === "curation" ? "Set curation" : c.stage === "post_analysis" ? "Post-analysis order" : "Set preparation", model: c.model, status: c.status,
      startedAt: Date.parse(c.startedAt), finishedAt: c.finishedAt ? Date.parse(c.finishedAt) : null, seconds: c.responseSeconds, tokens: c.completionTokens})),
    ...planningCalls.map(c => ({id: c.id, scope: c.scope === "crowd_adaptive" ? "Crowd-driven planning" : c.scope === "future_buffer" ? "Future buffer" : "Current transition", model: c.model, status: c.status, startedAt: c.startedAt, finishedAt: c.finishedAt, seconds: c.responseSeconds, tokens: c.completionTokens, detail: c.detail})),
    ...(j?.requestedAt && (visionInFlight || j.status === "completed" || j.status === "failed") ? [{id: j.id, scope: "Webcam understanding", model: j.model ?? "muse-spark-1.3", status: j.status === "running" ? "calling" : j.status,
      startedAt: Date.parse(j.requestedAt), finishedAt: j.finishedAt ? Date.parse(j.finishedAt) : null, seconds: j.modelSeconds}] : []),
  ].sort((x, y) => y.startedAt - x.startedAt).slice(0, 12);
  return {preparation, liveNodes, crowd, records, fresh, completed: fresh ? telemetry?.completedTransitions ?? 0 : null,
    inFlight: fresh ? calling.length + calls.filter(c => c.status === "calling").length + Number(visionInFlight) : null};
}

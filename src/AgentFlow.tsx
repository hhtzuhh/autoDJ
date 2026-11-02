import {useEffect, useMemo, useState} from "react";
import {deriveAgentFlow} from "./flowProjection";
import type {FlowNode} from "./flowProjection";
import type {CopilotState, WebcamFlowState} from "./types";
import "./agent-flow.css";
import {PlaylistDialog} from "./PlaylistDialog";
import {LibraryDialog} from "./LibraryDialog";
import type {BuildResult} from "./playlistRequest";

function FlowStep({item, number, selected, onSelect}: {item: FlowNode; number: number; selected: boolean; onSelect: () => void}) {
  return <button className={`flow-node flow-node--${item.status} ${selected ? "is-selected" : ""}`}
    onClick={onSelect} aria-pressed={selected} aria-label={`${item.title}: ${item.label}`}>
    <span className="flow-node__top"><span className="flow-node__number">{String(number).padStart(2, "0")}</span><span className="flow-light" /></span>
    <span className="flow-node__owner">{item.owner}</span>
    <strong>{item.title}</strong>
    {item.id === "library" && <span className="flow-node__action">View all songs ↗</span>}
    {item.id === "analysis" && <span className="flow-node__action">Review / analyze songs ↗</span>}
    {item.id === "curation" && <span className="flow-node__action">Build / choose playlist ↗</span>}
    <span className="flow-node__status">{item.label}</span>
  </button>;
}
function Lane({nodes, selected, setSelected, name}: {nodes: FlowNode[]; selected: string; setSelected: (id: string) => void; name: string}) {
  return <div className={`flow-lane flow-lane--${name} ${nodes.length === 3 ? "flow-lane--three" : ""}`}>
    {nodes.map((item, index) => <FlowStep key={item.id} item={item} number={index + 1} selected={selected === item.id} onSelect={() => setSelected(item.id)} />)}
  </div>;
}
const time = (at: number) => Number.isFinite(at) ? new Date(at).toLocaleTimeString([], {hour12: false}) : "—";

export function AgentFlow({state, webcam, disconnected, active, disabled, onBuild, onAnalyze}: {state: CopilotState; webcam: WebcamFlowState | null; disconnected: boolean; active: boolean; disabled: boolean; onBuild: (prompt: string, count: number) => Promise<BuildResult>; onAnalyze: (trackIds: string[]) => Promise<BuildResult>}) {
  const [selected, setSelected] = useState("analysis");
  const [playlistOpen, setPlaylistOpen] = useState(false);
  const [libraryPanel, setLibraryPanel] = useState<"library" | "analysis" | null>(null);
  const selectNode = (id: string) => {
    setSelected(id);
    if (id === "curation") setPlaylistOpen(true);
    if (id === "library" || id === "analysis") setLibraryPanel(id);
  };
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  const flow = useMemo(() => deriveAgentFlow(state, webcam, disconnected, now), [state, webcam, disconnected, now]);
  const detail = [...flow.preparation, ...flow.liveNodes, ...flow.crowd].find(n => n.id === selected)!;
  const inFlight = flow.records.filter(c => c.status === "calling");
  return <div className="agent-flow">
    <section className="flow-intro">
      <div><span className="eyebrow">Full-stack execution map</span><h1>Inside the AI DJ</h1>
        <p>Follow the evidence from a set request to an actual handoff.</p></div>
      <div className={`flow-connection ${flow.fresh ? "is-live" : ""}`}><span className="flow-light" />
        {flow.fresh ? "Live observer connected" : state.dataSource === "simulation" ? "DJ simulation · no live execution evidence" : "Waiting for fresh live telemetry"}</div>
    </section>
    <div className="flow-summary">
      <div><span>Prepared tracks</span><strong>{state.dataSource === "live" && !disconnected ? state.setBuilder?.queue.length ?? "—" : "—"}</strong><small>Selection, not playback</small></div>
      <div><span>Model calls in flight</span><strong>{flow.inFlight ?? "—"}</strong><small>Observed requests only</small></div>
      <div><span>Handoffs completed</span><strong>{flow.completed ?? "—"}</strong><small>This bridge session</small></div>
      <div><span>Execution mode</span><strong className="flow-summary__mode">{flow.fresh ? state.autoArmEnabled ? "Auto arm" : "Human approval" : "Unverified"}</strong><small>Human takeover stays authoritative</small></div>
    </div>
    <div className="flow-legend" aria-label="Node status legend">
      {[["running", "Working"], ["done", "Complete / available"], ["waiting", "Waiting"], ["error", "Failed / blocked"], ["idle", "Idle / no evidence"]].map(([kind, label]) =>
        <span key={kind} className={`flow-node--${kind}`}><i className="flow-light" />{label}</span>)}
      <span className="flow-legend__hint">Select a node to inspect its evidence</span>
    </div>
    <section className="flow-section" aria-labelledby="preparation-flow-title">
      <div className="flow-section__heading"><div><span className="flow-section__index">01</span><h2 id="preparation-flow-title">Analyze first, then select</h2></div><span>Local library → Analyze → Meta selection</span></div>
      <Lane nodes={flow.preparation} selected={selected} setSelected={selectNode} name="prepare" />
      <p className="flow-section__note">All library tracks must finish analysis before Meta selects a playlist. Existing results can be reused. This stage ends at song selection and order; transition preparation is paused.</p>

    </section>
    <section className="flow-section flow-section--loop" aria-labelledby="live-flow-title">
      <div className="flow-section__heading"><div><span className="flow-section__index">02</span><h2 id="live-flow-title">The live agent loop</h2></div><span>Triggered by state changes · no continuous LLM polling</span></div>
      <Lane nodes={flow.liveNodes} selected={selected} setSelected={setSelected} name="live" />
      <div className={`flow-return ${flow.fresh && inFlight.length ? "is-active" : ""}`} aria-hidden="true"><span>↶</span><i /><b>Next track · changed queue · human action · crowd feedback</b><i /><span>↵</span></div>
      <div className="flow-callout"><span className="flow-callout__symbol">↻</span><p><strong>{flow.fresh && inFlight.length ? "Meta is working on a bounded decision." : "Observe continuously. Call Meta when a decision is needed."}</strong>
        <span>Future plans may be prepared while the current track plays. Only the local executor controls the timed handoff.</span></p></div>
    </section>
    <section className="flow-section" aria-labelledby="crowd-flow-title">
      <div className="flow-section__heading"><div><span className="flow-section__index">03</span><h2 id="crowd-flow-title">Audience feedback</h2></div><span>Optional branch → future plans only</span></div>
      <Lane nodes={flow.crowd} selected={selected} setSelected={setSelected} name="crowd" />
      <p className="flow-section__note">Movement is not enjoyment. Uncertain or expired evidence holds the current direction; staged changes wait for a verified handoff.</p>
    </section>
    <div className="flow-inspection">
      <section className={`flow-detail flow-node--${detail.status}`} aria-labelledby="flow-detail-title">
        <div className="flow-detail__heading"><span className="flow-light" /><span className="eyebrow">Selected node · {detail.owner}</span></div>
        <h2 id="flow-detail-title">{detail.title}</h2><strong className="flow-detail__status">{detail.label}</strong><p>{detail.detail}</p>
        <details><summary>Inspect reported data</summary><pre>{JSON.stringify(detail.evidence ?? {status: "No evidence available"}, null, 2)}</pre></details>
      </section>
      <section className="flow-requests" aria-labelledby="flow-requests-title">
        <div className="flow-section__heading"><h2 id="flow-requests-title">Actual model requests</h2><span>Latest retained records</span></div>
        {!flow.records.length && <p className="flow-empty">No request records available. Viewing this page does not call a model.</p>}
        <div className="flow-request-list">{flow.records.map(call => <article key={call.id} className={`flow-request flow-request--${call.status}`}>
          <div><span>{time(call.startedAt)}</span><strong>{call.scope}</strong><b>{call.status === "calling" && !flow.fresh && call.scope !== "Webcam understanding" ? "last known: calling" : call.status}</b></div>
          <p>{call.model}<span>{call.seconds != null ? `${call.seconds.toFixed(1)}s response` : call.status === "calling" && flow.fresh ? `${Math.max(0, (now - call.startedAt) / 1000).toFixed(0)}s elapsed` : "Timing unavailable"}{call.tokens != null ? ` · ${call.tokens.toLocaleString()} output tokens` : ""}</span></p>
          {call.detail && <small>{call.detail}</small>}
        </article>)}</div>
        <p className="flow-section__note">Completed API requests are not completed audio transitions. Old preparation records can survive a bridge restart.</p>
      </section>
    </div>
    <LibraryDialog open={libraryPanel !== null && active} mode={libraryPanel ?? "library"} onClose={() => setLibraryPanel(null)} state={state} disabled={disabled || disconnected} disconnected={disconnected} now={now} onAnalyze={onAnalyze} />
    <PlaylistDialog open={playlistOpen && active} onClose={() => setPlaylistOpen(false)} state={state} disabled={disabled || disconnected} onBuild={onBuild} />
  </div>;
}

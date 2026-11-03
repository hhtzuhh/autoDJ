import { AgentFlow } from "./AgentFlow";
import type {BuildResult} from "./playlistRequest";
import { CrowdWebcam } from "./CrowdWebcam";
import { useEffect, useMemo, useState } from "react";
import { event, initialState, tracks } from "./demo";
import { MetaActivity } from "./MetaActivity";
import { CrowdObservation } from "./CrowdObservation";
import type {
  CopilotState,
  WebcamFlowState,
  DeckState,
  LifecycleStatus,
  PerformanceEvent,
  Track,
  TransitionEdge,
} from "./types";

const waveform = [
  24, 42, 31, 68, 54, 82, 45, 73, 91, 62, 49, 77, 58, 88, 66, 41, 71, 96,
  78, 56, 84, 48, 63, 90, 69, 38, 74, 52, 86, 65, 44, 79, 57, 92, 70, 47,
  81, 61, 75, 50, 89, 67, 43, 72, 55, 85, 64, 46,
];

const statusLabel: Record<LifecycleStatus, string> = {
  draft: "Draft",
  ready: "Plan ready",
  armed: "Armed",
  executing: "Executing",
  completed: "Completed",
  invalidated: "Invalidated",
  cancelled: "Human takeover",
  replanning: "Replanning",
};

function formatTime(seconds: number) {
  const safe = Math.max(0, Math.round(seconds));
  return `${Math.floor(safe / 60)}:${String(safe % 60).padStart(2, "0")}`;
}

function Icon({ name }: { name: "spark" | "play" | "shield" | "radio" }) {
  const paths = {
    spark: "M12 2l1.6 5.1L19 9l-5.4 1.9L12 16l-1.6-5.1L5 9l5.4-1.9L12 2zm6 12l.9 2.7L22 18l-3.1 1.3L18 22l-.9-2.7L14 18l3.1-1.3L18 14z",
    play: "M8 5v14l11-7L8 5z",
    shield: "M12 2l8 3v6c0 5-3.4 9.7-8 11-4.6-1.3-8-6-8-11V5l8-3zm-1 14l5-5-1.4-1.4-3.6 3.6-1.6-1.6L8 13l3 3z",
    radio: "M5 9v6h3l4 4V5L8 9H5zm11.5 3a4.5 4.5 0 01-2.2 3.9l1 1.7a6.5 6.5 0 000-11.2l-1 1.7a4.5 4.5 0 012.2 3.9z",
  };
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d={paths[name]} />
    </svg>
  );
}

interface WaveformWindow {
  cueBeat: number;
  mixStartBeat: number;
  mixEndBeat: number;
  cuePosition: number;
  mixStartPosition: number;
  mixEndPosition: number;
}

const ACTIVE_PLAN_STATUSES = ["draft", "ready", "armed", "executing", "replanning"];

function Waveform({
  progress,
  accent,
  window,
  entry,
}: {
  progress: number;
  accent: string;
  window?: WaveformWindow;
  entry?: { beat: number; position: number };
}) {
  return (
    <div className="waveform-block">
      <div className={`waveform waveform--${accent}`} aria-label="Track waveform">
        <div className="waveform__bars">
          {waveform.map((height, index) => (
            <span
              key={index}
              className={index / waveform.length <= progress ? "is-played" : ""}
              style={{ height: `${height}%` }}
            />
          ))}
        </div>
        {window && (
          <>
            <div
              className="waveform__mix-window"
              style={{
                left: `${window.mixStartPosition * 100}%`,
                width: `${Math.max(0, window.mixEndPosition - window.mixStartPosition) * 100}%`,
              }}
              title={`Mix window: beat ${window.mixStartBeat}–${window.mixEndBeat}`}
            />
            <div
              className="waveform__cue-marker"
              style={{ left: `${window.cuePosition * 100}%` }}
              title={`Cue / prepare: beat ${window.cueBeat}`}
            />
          </>
        )}
        {entry && (
          <div
            className="waveform__entry-marker"
            style={{ left: `${entry.position * 100}%` }}
            title={`Target entry: beat ${entry.beat}`}
          />
        )}
        <div className="waveform__needle" style={{ left: `${progress * 100}%` }} />
      </div>
      {(window || entry) && (
        <div className={`waveform-labels ${window ? "has-window" : ""}`}>
          {window && (
            <span
              className="is-cue"
              style={{ left: `${window.cuePosition * 100}%` }}
            >
              Cue {window.cueBeat}
            </span>
          )}
          {window && (
            <span
              className="is-mix"
              style={{
                left: `${((window.mixStartPosition + window.mixEndPosition) / 2) * 100}%`,
              }}
            >
              Mix {window.mixStartBeat}–{window.mixEndBeat}
            </span>
          )}
          {entry && (
            <span
              className="is-entry"
              style={{ left: `${entry.position * 100}%` }}
            >
              In {entry.beat}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function DeckCard({ deck, state }: { deck: DeckState; state: CopilotState }) {
  const progress = deck.track
    ? Math.min(1, deck.positionSeconds / deck.track.durationSeconds)
    : 0;
  const planIsActive = ACTIVE_PLAN_STATUSES.includes(state.plan.status);
  const planWindow = planIsActive && deck.track?.id === state.plan.sourceTrackId
    ? {
        cueBeat: state.plan.sourceCueBeat,
        mixStartBeat: state.plan.sourceExitBeat,
        mixEndBeat: state.plan.sourceExitBeat + state.plan.bars * 4,
        cuePosition: state.plan.sourceCuePosition,
        mixStartPosition: state.plan.sourceMixStartPosition,
        mixEndPosition: state.plan.sourceMixEndPosition,
      }
    : undefined;
  const entryMarker = planIsActive && deck.track?.id === state.plan.targetTrackId
    ? {
        beat: state.plan.targetEntryBeat,
        position: state.plan.targetEntryPosition,
      }
    : undefined;
  return (
    <article className={`deck-card ${deck.playing ? "is-playing" : ""}`}>
      <div className="deck-card__topline">
        <span className="eyebrow">Deck {deck.deck}</span>
        <span className={`transport ${deck.playing ? "is-on" : ""}`}>
          <i /> {deck.playing ? "Playing" : "Standby"}
        </span>
      </div>
      {deck.track ? (
        <>
          <div className="deck-card__identity">
            <div>
              <h3>{deck.track.title}</h3>
            </div>
          </div>
          <Waveform
            progress={progress}
            accent={deck.track.accent}
            window={planWindow}
            entry={entryMarker}
          />
          <div className="deck-card__readout">
            <div>
              <strong>{formatTime(deck.positionSeconds)}</strong>
              <span>/ {formatTime(deck.track.durationSeconds)}</span>
            </div>
            <div className="deck-metrics">
              <span><b>{deck.track.bpm}</b> BPM</span>
              <span><b>{deck.beatIndex ?? "—"}</b> beat</span>
              <span><b>{deck.syncRole}</b> sync</span>
            </div>
          </div>
        </>
      ) : (
        <div className="empty-deck">No track loaded</div>
      )}
    </article>
  );
}

function TrackMini({ track }: { track: Track }) {
  return (
    <div className="track-mini">
      <div className={`track-dot track-dot--${track.accent}`} />
      <div>
        <strong>{track.title}</strong>
        <span>{track.bpm} BPM · {track.energy} energy</span>
      </div>
    </div>
  );
}

function PlanPanel({ state }: { state: CopilotState }) {
  if (!state.plan.sourceTrackId || !state.plan.targetTrackId) return <section className="panel plan-panel">
    <div className="panel__header"><div><span className="eyebrow">Before playback</span><h2>No active set yet</h2></div></div>
    <p className="set-builder__message">Build a set from your local songs. After analysis finishes, export the Mixxx playlist and start the set to request its first transition plan.</p>
    <MetaActivity state={state} />
  </section>;
  const source = state.queue.find((track) => track.id === state.plan.sourceTrackId)!;
  const target = state.queue.find((track) => track.id === state.plan.targetTrackId)!;
  const isFixture = state.plan.selectedBy === "fixture";
  const sourceIndex = state.queue.findIndex(
    (track) => track.id === state.plan.sourceTrackId,
  );
  const planningMode = state.plan.planningMode
    ?? (state.plan.transitionMode === "clean_break"
      ? "pair_clean_break"
      : "rolling_abc");
  const inferredWindowIds = ["rolling_abc", "rolling_abcd"].includes(planningMode) && sourceIndex >= 0
    ? state.queue.slice(sourceIndex, sourceIndex + (planningMode === "rolling_abcd" ? 4 : 3)).map((track) => track.id)
    : [state.plan.sourceTrackId, state.plan.targetTrackId];
  const planningWindow = (state.plan.planningWindowTrackIds ?? inferredWindowIds)
    .map((trackId) => state.queue.find((track) => track.id === trackId))
    .filter(Boolean) as Track[];
  const futurePlanningWindow = (state.plan.futurePlanningWindowTrackIds ?? [])
    .map((trackId) => state.queue.find((track) => track.id === trackId))
    .filter(Boolean) as Track[];
  const activeEdge = state.transitions.find(
    (edge) => edge.sourceTrackId === state.plan.sourceTrackId
      && edge.targetTrackId === state.plan.targetTrackId,
  );
  const followingTrack = sourceIndex >= 0 ? state.queue[sourceIndex + 2] : undefined;
  const inferredFollowingDraft = followingTrack
    ? state.transitions.find(
      (edge) => edge.sourceTrackId === state.plan.targetTrackId
        && edge.targetTrackId === followingTrack.id
        && edge.selectedBy === "meta"
        && edge.status === "feasible",
    )
    : undefined;
  const scopeLabel = planningMode === "rolling_abcd" || planningMode === "rolling_tail"
    ? `1 plan · ${Math.max(1, planningWindow.length - 1)} transitions`
    : planningMode === "rolling_abc"
      ? "Legacy 2-transition window"
    : planningMode === "promoted_rolling_draft"
      ? "Promoted rolling draft"
      : "2-track clean-break pair";
  const isTerminal = ["completed", "invalidated", "cancelled"].includes(
    state.plan.status,
  );
  return (
    <section className="panel plan-panel">
      <div className="panel__header">
        <div>
          <span className="eyebrow">
            {isTerminal ? "Latest transition" : "Next transition"}
          </span>
          <h2>AI plan</h2>
        </div>
        <span className={`status-chip status-chip--${state.plan.status}`}>
          {state.plan.status === "replanning" && <i />}
          {statusLabel[state.plan.status]}
        </span>
      </div>

      <div className="plan-route">
        <TrackMini track={source} />
        <div className="plan-route__bridge">
          <span>
            {state.plan.technique ? state.plan.technique.replaceAll("_", " ") : state.plan.transitionMode === "clean_break"
              ? "Clean break"
              : `${state.plan.bars} bars`}
          </span>
          <div><i /><i /><i /><i /></div>
        </div>
        <TrackMini track={target} />
      </div>

      <div className="planning-scope">
        <div className="planning-scope__header">
          <span>Meta decision scope</span>
          <strong>{scopeLabel}</strong>
        </div>
        <div className="planning-scope__tracks">
          {planningWindow.map((track, index) => (
            <span key={track.id}>
              {index > 0 && <i>→</i>}
              <b>{track.title}</b>
            </span>
          ))}
        </div>
        <p>
          {activeEdge?.candidateCount ?? "—"} local candidates → 1 Meta decision.
          {planningMode === "rolling_abcd" || planningMode === "rolling_tail"
            ? " The first edge is executable; the remaining edges are preserved as Meta plans awaiting promotion."
            : state.plan.followingDraft || inferredFollowingDraft
            ? " The first edge is executable; the second edge is preserved as a Meta draft, not armed."
            : planningMode === "promoted_rolling_draft"
              ? " This executable plan was promoted from the prior three-track Meta decision."
              : " Only this edge was decided by Meta; later edges are analyzer standby only."}
        </p>
        {(state.plan.activeTransitionLocked || (state.plan.futurePlanStatus && state.plan.futurePlanStatus !== "idle")) && (
          <div className="planning-scope__header">
            <span>{state.plan.activeTransitionLocked ? "Active edge locked" : "Future transitions"}</span>
            <strong>
              Future window {state.plan.futurePlanStatus ?? "idle"}
            </strong>
          </div>
        )}
        {futurePlanningWindow.length > 0 && (
          <div className="planning-scope__tracks">
            {futurePlanningWindow.map((track, index) => (
              <span key={`future-${track.id}`}>
                {index > 0 && <i>→</i>}
                <b>{track.title}</b>
              </span>
            ))}
          </div>
        )}
      </div>

      <MetaActivity state={state} />

      {!!state.plan.performanceActions?.length && <div className="planning-scope">
        <strong>Performance plan · one Meta decision</strong>
        <p>Beat 0 is the handoff cue. Negative beats prepare the outgoing song; later actions shape the incoming song.</p>
        {state.plan.performanceActions.map((action, index) => <p key={index}>
          <b>{action.function.replaceAll("_", " ")}</b> · beat {action.start_beat}
          {action.deck && ` · ${action.deck} deck`}
          {action.target_bpm != null && ` → ${action.target_bpm} BPM`}
          {action.duration_beats != null && ` · ${action.duration_beats / 4} bars`}
          {action.curve && ` · ${action.curve.replaceAll("_", " ")}`}
          {action.fade_out_seconds != null && ` · fade ${action.fade_out_seconds}s`}
        </p>)}
      </div>}
      <div className="cue-grid">
        {state.plan.technique && <div><span>Technique</span><strong>{state.plan.technique.replaceAll("_", " ")}</strong></div>}
        <div>
          <span>Cue / prepare</span>
          <strong>Beat {state.plan.sourceCueBeat}</strong>
        </div>
        <div>
          <span>{state.plan.transitionMode === "clean_break" ? "Handoff" : "Mix window"}</span>
          <strong>
            {state.plan.transitionMode === "clean_break"
              ? `Cut at beat ${state.plan.sourceExitBeat}`
              : `Beat ${state.plan.sourceExitBeat}–${state.plan.sourceExitBeat + state.plan.bars * 4}`}
          </strong>
        </div>
        <div>
          <span>Target cue</span>
          <strong>Beat {state.plan.targetEntryBeat}</strong>
        </div>
        <div>
          <span>Confidence</span>
          <strong>{Math.round(state.plan.confidence * 100)}%</strong>
        </div>
      </div>

      <div className="decision-card">
        <div className="decision-card__icon"><Icon name="spark" /></div>
        <div>
          <div className="decision-card__topline">
            <strong>{state.plan.intent}</strong>
            <span>{isFixture ? "Demo fixture" : state.plan.model}</span>
          </div>
          <p>{state.plan.reason}</p>
          {isFixture && (
            <small>No live model call in simulation mode.</small>
          )}
        </div>
      </div>

      {state.plan.invalidationReason && (
        <div className="warning-callout">
          <strong>Old plan invalidated</strong>
          <span>{state.plan.invalidationReason}</span>
        </div>
      )}
    </section>
  );
}

function QueuePanel({
  state,
  disabled,
  onReorder,
}: {
  state: CopilotState;
  disabled: boolean;
  onReorder: (trackIds: string[]) => Promise<void>;
}) {
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const builder = state.dataSource === "live" ? state.setBuilder : undefined;
  const prepared = Boolean(builder?.queue.length);
  const queueDragLocked = true;
  const shownQueue = prepared ? builder!.queue : state.queue;
  const liveEdges = new Map(
    (state.transitions ?? []).map((transition) => [
      `${transition.sourceTrackId}:${transition.targetTrackId}`,
      transition,
    ]),
  );
  const transitions = prepared
    ? builder!.transitions.map((transition) => (
      liveEdges.get(`${transition.sourceTrackId}:${transition.targetTrackId}`) ?? transition
    ))
    : state.transitions ?? (state.dataSource === "simulation" ? initialState.transitions : []);
  const edgeAfter = new Map(
    transitions.map((transition) => [
      transition.sourceTrackId,
      transition,
    ]),
  );
  const liveCurrentIndex = state.queue.findIndex((track) => track.state === "current");
  const liveBuffer = liveCurrentIndex >= 0
    ? state.queue.slice(liveCurrentIndex, liveCurrentIndex + 5)
    : [];
  const readyBufferEdges = liveBuffer.slice(1, 4).filter((track) => {
    const edge = edgeAfter.get(track.id);
    return edge && ["ready", "armed", "executing"].includes(edge.status);
  }).length;
  const dropBefore = (targetId: string) => {
    if (!builder || !draggedId || draggedId === targetId) return;
    const ordered = builder.orderedTrackIds.filter((id) => id !== draggedId);
    ordered.splice(ordered.indexOf(targetId), 0, draggedId);
    setDraggedId(null);
    void onReorder(ordered);
  };
  return (
    <section className="panel queue-panel">
      <div className="panel__header">
        <div>
          <span className="eyebrow">
            {prepared ? `Preparation revision ${builder!.queueRevision}` : `Revision ${state.queueRevision}`}
          </span>
          <h2>{prepared ? "Tonight’s set" : "Queue"}</h2>
        </div>
        <span className="count-badge">
          {liveBuffer.length >= 2
            ? `${readyBufferEdges}/${Math.min(3, Math.max(0, liveBuffer.length - 2))} future transitions ready · ${liveBuffer.length} tracks`
            : `${shownQueue.length} tracks`}
        </span>
      </div>
      <div className="queue-list" role="list" aria-label="Performance queue">
        {shownQueue.map((track, index) => {
          const transition = edgeAfter.get(track.id);
          const bufferSlot = liveBuffer.findIndex((item) => item.id === track.id);
          const inboundTransition = bufferSlot > 0
            ? edgeAfter.get(liveBuffer[bufferSlot - 1].id)
            : null;
          const trackIsBufferReady = bufferSlot === 0 || Boolean(
            inboundTransition
            && ["ready", "armed", "executing"].includes(inboundTransition.status),
          );
          const liveTrack = state.queue.find((item) => item.id === track.id)
            ?? ("state" in track ? track : null);
          const analysisStatus = "analysisStatus" in track ? track.analysisStatus : null;
          return (
            <div
              className={`queue-item ${draggedId === track.id ? "is-dragging" : ""}`}
              key={track.id}
              role="listitem"
              draggable={prepared && !disabled && !queueDragLocked}
              onDragStart={() => setDraggedId(track.id)}
              onDragEnd={() => setDraggedId(null)}
              onDragOver={(event) => prepared && event.preventDefault()}
              onDrop={() => dropBefore(track.id)}
            >
              <div className={`queue-row queue-row--${liveTrack?.state ?? "queued"} ${bufferSlot >= 0 ? "is-buffered" : ""}`}>
                <span className="queue-row__number">
                  {liveTrack?.state === "current" ? <Icon name="play" /> : index + 1}
                </span>
                <div className="queue-row__identity">
                  <strong>{track.title}</strong>
                  <span>
                    {track.bpm ? `${Math.round(track.bpm)} BPM` : "BPM pending"}
                  </span>
                </div>
                <span className={`queue-row__state ${analysisStatus ? `analysis--${analysisStatus}` : ""}`}>
                  {bufferSlot >= 0
                    ? `${bufferSlot === 0 ? "Live" : trackIsBufferReady ? "Ready" : "Pending"} · ${bufferSlot + 1}/${liveBuffer.length}`
                    : liveTrack?.state ?? analysisStatus}
                </span>
              </div>
              {transition && (
                <TransitionReadiness
                  edge={transition}
                  insideBuffer={bufferSlot >= 0 && bufferSlot < liveBuffer.length - 1}
                  currentBeat={state.decks.find(
                    (deck) => deck.playing && deck.track?.id === transition.sourceTrackId,
                  )?.beatIndex}
                />
              )}
            </div>
          );
        })}
      </div>
      <div className="queue-note">
        <Icon name="shield" />
        <p>
          {prepared
            ? "Queue reordering is temporarily locked while the rolling transition planner is being validated."
            : "Only adjacent pairs are analyzed. Reordering invalidates affected edges."}
        </p>
      </div>
    </section>
  );
}

function TransitionReadiness({
  edge,
  currentBeat,
  insideBuffer = true,
}: {
  edge: TransitionEdge;
  currentBeat?: number | null;
  insideBuffer?: boolean;
}) {
  const selectedBy = edge.status !== "planning" && edge.selectedBy === "meta"
    ? "Meta"
    : null;
  const nextTiming = edge.sourceExitBeat != null && edge.targetEntryBeat != null
    ? `Beat ${edge.sourceExitBeat} → ${edge.targetEntryBeat}${edge.bars ? ` · ${edge.bars} bars` : ""}`
    : null;
  const beatsToFinalArm = currentBeat != null && edge.sourceExitBeat != null
    ? edge.sourceExitBeat - 16 - currentBeat
    : null;
  const usesMeta = edge.selectedBy === "meta";
  const displayStatus = edge.status === "completed"
      ? usesMeta ? "Meta completed" : "Completed"
      : edge.status === "planning"
      ? "Calling Meta…"
      : edge.status === "planned"
        ? "Meta planned · not armed"
      : edge.status === "needs_human"
        ? "Needs human"
        : !usesMeta
          ? insideBuffer ? "Meta not planned" : "Waiting beyond buffer"
        : edge.status === "ready"
          ? "Meta plan ready"
          : edge.status === "armed"
            ? "Meta armed"
            : edge.status === "executing"
              ? "Meta executing"
              : edge.status === "feasible"
                  ? "Future draft"
                  : edge.status.replaceAll("_", " ");
  return (
    <div className={`transition-edge transition-edge--${edge.status}`}>
      <i />
      <span>{displayStatus}</span>
      {selectedBy && <span>{selectedBy}</span>}
      {nextTiming && <small>{nextTiming}</small>}
      {usesMeta && edge.metaOrigin && <small title={`Request ${edge.metaOrigin.requestId} · candidate ${edge.metaOrigin.candidateId ?? "pair selection"}`}>
        Plan from Meta · {new Date(edge.metaOrigin.selectedAt).toLocaleTimeString("en-GB")}
        {" · "}{edge.metaOrigin.requestId.slice(-8)}
      </small>}
      {usesMeta && !edge.metaOrigin && <small>Earlier Meta decision · request history unavailable</small>}
      <small>{edge.candidateCount} candidates</small>
      {beatsToFinalArm != null && ["planning", "planned"].includes(edge.status) && (
        <small className={beatsToFinalArm <= 32 ? "is-warning" : ""}>
          {beatsToFinalArm > 0
            ? `${beatsToFinalArm} beats until final safe arm`
            : "Final safe arm point passed"}
        </small>
      )}
      {edge.reason && (usesMeta || edge.status === "needs_human") && (
        <small title={edge.reason}>{edge.reason.replaceAll("_", " ")}</small>
      )}
    </div>
  );
}

function EventLog({ events }: { events: PerformanceEvent[] }) {
  return (
    <section className="panel event-panel">
      <div className="panel__header panel__header--compact">
        <div>
          <span className="eyebrow">Authoritative timeline</span>
          <h2>Performance events</h2>
        </div>
        <span className="revision-label">Latest first</span>
      </div>
      <div className="event-list">
        {events.slice(0, 5).map((item) => (
          <div className="event-row" key={item.id}>
            <time>{item.time}</time>
            <span className={`event-dot event-dot--${item.kind}`} />
            <div>
              <strong>{item.title}</strong>
              <p>{item.detail}</p>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function App() {
  const [view, setView] = useState<"control" | "flow">(() => new URLSearchParams(window.location.search).get("view") === "flow" ? "flow" : "control");
  const [webcamTelemetry, setWebcamTelemetry] = useState<WebcamFlowState | null>(null);
  const switchView = (next: "control" | "flow") => {
    setView(next);
    const url = new URL(window.location.href);
    if (next === "flow") url.searchParams.set("view", "flow"); else url.searchParams.delete("view");
    window.history.replaceState(null, "", url);
  };
  const [state, setState] = useState<CopilotState>(initialState);
  const [isReplanning, setIsReplanning] = useState(false);
  const [bridgeError, setBridgeError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [actionPending, setActionPending] = useState(false);
  const liveRequested = useMemo(
    () => new URLSearchParams(window.location.search).get("source") === "live",
    [],
  );

  const currentDeck = useMemo(
    () => state.decks.find((deck) => deck.playing) ?? state.decks[1],
    [state.decks],
  );
  const activeSourceDeck = useMemo(
    () => state.decks.find(
      (deck) => deck.playing && deck.track?.id === state.plan.sourceTrackId,
    ),
    [state.decks, state.plan.sourceTrackId],
  );
  const planCanStillExecute = ACTIVE_PLAN_STATUSES.includes(state.plan.status);
  const needsRecovery = Boolean(state.plan.recoveryReason)
    || ["cancelled", "invalidated"].includes(state.plan.status);
  const beatsUntilCue = !planCanStillExecute || activeSourceDeck?.beatIndex == null
    ? null
    : state.plan.sourceCueBeat - activeSourceDeck.beatIndex;

  useEffect(() => {
    if (state.dataSource !== "simulation" || !currentDeck.playing) return;
    const timer = window.setInterval(() => {
      setState((current) => ({
        ...current,
        decks: current.decks.map((deck) =>
          deck.playing
            ? {
                ...deck,
                positionSeconds: Math.min(
                  deck.track?.durationSeconds ?? deck.positionSeconds,
                  deck.positionSeconds + 0.46,
                ),
              }
            : deck,
        ) as [DeckState, DeckState],
      }));
    }, 460);
    return () => window.clearInterval(timer);
  }, [currentDeck.playing, state.dataSource]);

  useEffect(() => {
    if (!liveRequested) return;
    let cancelled = false;
    const readLiveState = async () => {
      try {
        const response = await fetch("http://127.0.0.1:8765/api/state", {
          cache: "no-store",
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error ?? `Bridge returned ${response.status}`);
        }
        if (!cancelled) {
          setState(payload as CopilotState);
          setBridgeError(null);
        }
      } catch (error) {
        if (!cancelled) {
          setBridgeError(error instanceof Error ? error.message : String(error));
          setState(current => ({...current, connected: false}));
        }
      }
    };
    void readLiveState();
    const timer = window.setInterval(() => void readLiveState(), 500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [liveRequested]);

  const extendPerformance = () => {
    if (state.dataSource !== "simulation" || isReplanning) return;
    setIsReplanning(true);
    setState((current) => ({
      ...current,
      performanceRevision: current.performanceRevision + 1,
      decks: current.decks.map((deck) =>
        deck.deck === 2 ? { ...deck, beatIndex: 318, positionSeconds: 147.15 } : deck,
      ) as [DeckState, DeckState],
      plan: {
        ...current.plan,
        status: "replanning",
        invalidationReason: "Human extended B beyond the original exit at beat 288.",
      },
      events: [
        event(
          `evt-${Date.now()}`,
          "human",
          "Human extended the phrase",
          "Old B→C cue expired. Bass keeps playing; no backward seek.",
        ),
        ...current.events,
      ],
    }));

    window.setTimeout(() => {
      setState((current) => ({
        ...current,
        plan: {
          ...current.plan,
          id: "fixture-q3-live-replan",
          status: "ready",
          sourceCueBeat: 340,
          sourceExitBeat: 348,
          sourceCuePosition: 0.806,
          sourceMixStartPosition: 0.825,
          sourceMixEndPosition: 0.844,
          targetEntryBeat: 228,
          targetEntryPosition: 0.69,
          bars: 2,
          confidence: 0.71,
          reason:
            "The old cue is gone. This is the next analyzed downbeat with enough runway to finish safely.",
          selectedBy: "deterministic_fallback",
          invalidationReason: "Replaced expired beat 288 plan from actual beat 318.",
          performanceRevision: current.performanceRevision,
        },
        events: [
          event(
            `evt-${Date.now()}-ready`,
            "system",
            "Recovery plan ready in 8.4 ms",
            "New exit beat 348; source playback was never interrupted.",
          ),
          ...current.events,
        ],
      }));
      setIsReplanning(false);
    }, 1050);
  };

  const armPlan = () => {
    if (state.dataSource !== "simulation" || state.plan.status !== "ready") return;
    setState((current) => ({
      ...current,
      plan: { ...current.plan, status: "armed" },
      events: [
        event(
          `evt-${Date.now()}`,
          "executor",
          "Plan armed",
          "Queue, track identity, analysis key, and timing preconditions match.",
        ),
        ...current.events,
      ],
    }));
    window.setTimeout(() => {
      setState((current) => ({
        ...current,
        plan: { ...current.plan, status: "executing" },
        events: [
          event(
            `evt-${Date.now()}-exec`,
            "executor",
            "Local executor started",
            "Beat scheduling is local; the model is not in the audio loop.",
          ),
          ...current.events,
        ],
      }));
    }, 700);
  };

  const takeOver = () => {
    if (state.dataSource !== "simulation") return;
    setIsReplanning(false);
    setState((current) => ({
      ...current,
      plan: {
        ...current.plan,
        status: "cancelled",
        invalidationReason: "Human mixer takeover — automation cleanup skipped.",
      },
      events: [
        event(
          `evt-${Date.now()}`,
          "human",
          "Human takeover",
          "Future automation stopped. Current playback and mixer state are preserved.",
        ),
        ...current.events,
      ],
    }));
  };

  const reset = () => {
    if (state.dataSource !== "simulation") return;
    setIsReplanning(false);
    setState({
      ...initialState,
      events: initialState.events.map((item) => ({ ...item })),
    });
  };

  const sendLiveAction = async (action: "plan" | "arm" | "takeover" | "recover") => {
    setActionPending(true);
    setActionNotice(null);
    try {
      const response = await fetch(`http://127.0.0.1:8765/api/${action}`, {
        method: "POST",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.message ?? payload.error ?? `Bridge returned ${response.status}`);
      }
      if (action === "takeover" && payload.status === "cancelled") {
        setState((current) => ({
          ...current,
          plan: {
            ...current.plan,
            status: "cancelled",
            invalidationReason:
              payload.invalidation_reason ?? "human_dashboard_takeover",
          },
          events: [
            event(
              `evt-${Date.now()}-takeover`,
              "human",
              "Human takeover",
              "Future automation stopped. Current playback and mixer state are preserved.",
            ),
            ...current.events,
          ],
        }));
      }
      setActionNotice(payload.message ?? `${action} accepted`);
    } catch (error) {
      setActionNotice(
        `Rejected: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      setActionPending(false);
    }
  };

  const setAutoArm = async (enabled: boolean) => {
    setActionPending(true);
    setActionNotice(null);
    try {
      const response = await fetch("http://127.0.0.1:8765/api/auto-arm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error ?? `Bridge returned ${response.status}`);
      }
      setState((current) => ({ ...current, autoArmEnabled: enabled }));
      setActionNotice(payload.message ?? `Auto arm ${enabled ? "enabled" : "disabled"}`);
    } catch (error) {
      setActionNotice(
        `Rejected: ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      setActionPending(false);
    }
  };

  const buildSet = async (prompt: string, count: number): Promise<BuildResult> => {
    setActionPending(true);
    setActionNotice("Analyzing the local library before Meta selects songs…");
    try {
      const response = await fetch("http://127.0.0.1:8765/api/set-builder/build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, count }),
      });
      const payload = await response.json();
      if (!response.ok || payload.status === "error") throw new Error(payload.error ?? `Bridge returned ${response.status}`);
      setActionNotice("Build accepted. Local analysis and Muse Spark descriptions must complete for the entire library before selection.");
      return {ok: true, buildId: payload.buildId};
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setActionNotice(`Rejected: ${message}`);
      return {ok: false, error: message};
    } finally {
      setActionPending(false);
    }
  };

  const analyzeLibrary = async (trackIds: string[]): Promise<BuildResult> => {
    setActionPending(true);
    try {
      const response = await fetch("http://127.0.0.1:8765/api/set-builder/analyze", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({trackIds}),
      });
      const payload = await response.json();
      if (!response.ok || payload.status === "error") throw new Error(payload.error ?? "Could not start library analysis");
      setActionNotice("Selected-song analysis accepted. Meta selection waits for a playlist request.");
      return {ok: true};
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setActionNotice(`Rejected: ${message}`);
      return {ok: false, error: message};
    } finally { setActionPending(false); }
  };

  const describeMusic = async (trackIds: string[]): Promise<BuildResult> => {
    setActionPending(true);
    try {
      const response = await fetch("http://127.0.0.1:8765/api/set-builder/music-features", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({trackIds}),
      });
      const payload = await response.json();
      if (!response.ok || payload.status === "error") throw new Error(payload.error ?? "Could not start Muse music analysis");
      return {ok: true};
    } catch (error) {
      return {ok: false, error: error instanceof Error ? error.message : String(error)};
    } finally {setActionPending(false);}
  };

  const reorderSet = async (orderedTrackIds: string[]) => {
    setActionPending(true);
    setActionNotice("Updating only the changed adjacent transitions…");
    try {
      const response = await fetch("http://127.0.0.1:8765/api/queue/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ orderedTrackIds }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error ?? `Bridge returned ${response.status}`);
      setActionNotice("Queue reordered; retained transition analyses were preserved.");
    } catch (error) {
      setActionNotice(`Rejected: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setActionPending(false);
    }
  };

  const sendSetAction = async (action: "prepare-transitions" | "activate" | "build-performance" | "start"): Promise<BuildResult> => {
    setActionPending(true);
    setActionNotice(null);
    try {
      const response = await fetch(
        `http://127.0.0.1:8765/api/set-builder/${action}`,
        { method: "POST" },
      );
      const payload = await response.json();
      if (!response.ok || payload.status === "error") throw new Error(payload.message ?? payload.error ?? `Bridge returned ${response.status}`);
      const message = action === "activate" && payload.playlist
        ? `${payload.message} Playlist: ${payload.playlist}`
        : payload.message ?? (action === "prepare-transitions" ? "Preparing adjacent transition candidates in the selected order." : `${action} accepted`);
      setActionNotice(message);
      return {ok: true, message};
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setActionNotice(`Rejected: ${message}`);
      return {ok: false, error: message};
    } finally {
      setActionPending(false);
    }
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand__mark"><Icon name="radio" /></div>
          <div>
            <strong>AI DJ Copilot</strong>
            <span>Human-first live performance</span>
          </div>
        </div>
        <div className="topbar__status">
          <span className={`source-badge source-badge--${state.dataSource}`}>
            <i /> {state.dataSource === "live" ? "Live Mixxx" : "DJ simulation"}
          </span>
          <span className={`connection ${state.connected ? "" : "is-off"}`}>
            <i /> {state.connected ? "State connected" : "Disconnected"}
          </span>
          <span className="revision">P{state.performanceRevision} · Q{state.queueRevision}</span>
        </div>
      </header>

      <main>
        {liveRequested && bridgeError && (
          <div className="bridge-error" role="alert">
            <strong>Waiting for Mixxx bridge</strong>
            <span>{bridgeError}</span>
          </div>
        )}
        <nav className="view-tabs" role="tablist" aria-label="Dashboard view" onKeyDown={event => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "Home" ? "control" : event.key === "End" ? "flow" : view === "control" ? "flow" : "control";
          switchView(next);
          event.currentTarget.querySelectorAll<HTMLButtonElement>("button")[next === "control" ? 0 : 1].focus();
        }}>
          <button id="control-tab" role="tab" aria-controls="control-view" aria-selected={view === "control"} tabIndex={view === "control" ? 0 : -1} onClick={() => switchView("control")}>Control room</button>
          <button id="flow-tab" role="tab" aria-controls="flow-view" aria-selected={view === "flow"} tabIndex={view === "flow" ? 0 : -1} onClick={() => switchView("flow")}>Agent flow</button>
        </nav>
        <div id="flow-view" role="tabpanel" aria-labelledby="flow-tab" hidden={view !== "flow"}>
          <AgentFlow state={state} webcam={webcamTelemetry} disconnected={Boolean(bridgeError) || (liveRequested && state.dataSource !== "live")} active={view === "flow"} disabled={actionPending || state.plan.status === "executing"} onBuild={buildSet} onAnalyze={analyzeLibrary} onDescribeMusic={describeMusic} onSetAction={sendSetAction} />
        </div>
        <div id="control-view" role="tabpanel" aria-labelledby="control-tab" hidden={view !== "control"}>
        <section className="hero-strip">
          <span className="eyebrow">Live control room</span>
          <div className="cue-countdown">
            <span>Next cue</span>
            <strong>{beatsUntilCue == null ? "—" : Math.max(0, beatsUntilCue)}</strong>
            <small>beats</small>
          </div>
        </section>

        <div className="deck-grid">
          {state.decks.map((deck) => (
            <DeckCard deck={deck} state={state} key={deck.deck} />
          ))}
        </div>

        <div className="workspace-grid">
          <PlanPanel state={state} />
          <QueuePanel
            state={state}
            disabled={actionPending || state.plan.status === "executing"}
            onReorder={reorderSet}
          />
          <EventLog events={state.events} />
        </div>
        <CrowdWebcam live={state.dataSource === "live" && state.connected} tracks={state.queue} onTelemetry={setWebcamTelemetry} />
        <CrowdObservation />
        </div>
      </main>

      <footer className="control-dock">
        <div className="control-dock__context">
          <span>{state.dataSource === "live" ? "Live observer" : "Demo controls"}</span>
          <p>
            {state.dataSource === "live"
              ? state.autoArmEnabled
                ? "The target is three future-ready transitions. Ready transitions auto-arm after deck rotation."
                : "Automation is suspended. Start a reviewed set or recover before resuming Auto arm."
              : "DJ controls are simulated. Crowd observation uses real Meta requests for uploads and while webcam capture is on. Adaptive DJ requires Live Mixxx."}
          </p>
          {state.dataSource === "live" && actionNotice && (
            <small className="action-notice">{actionNotice}</small>
          )}
        </div>
        {state.dataSource === "simulation" ? (
          <div className="control-dock__actions">
            <button className="button button--ghost" onClick={reset}>Reset</button>
            <button
              className="button button--secondary"
              onClick={extendPerformance}
              disabled={isReplanning || state.plan.status === "cancelled"}
            >
              Extend B past cue
            </button>
            <button
              className="button button--primary"
              onClick={armPlan}
              disabled={state.plan.status !== "ready" || isReplanning}
            >
              <Icon name="play" /> Approve & arm
            </button>
            <button className="button button--danger" onClick={takeOver}>
              Take over
            </button>
          </div>
        ) : (
          <div className="control-dock__actions">
            <button
              className="button button--ghost"
              onClick={() => { window.location.href = "/"; }}
            >
              Open simulation
            </button>
            <button
              className={`button ${state.autoArmEnabled ? "button--toggle-on" : "button--ghost"}`}
              disabled={actionPending || state.autoArmEnabled}
              onClick={() => void setAutoArm(true)}
            >
              {state.autoArmEnabled ? "Auto arm · Always on" : "Resume Auto arm"}
            </button>
            <button
              className="button button--secondary"
              disabled={actionPending || state.queue.length < 2 || state.plan.status === "executing"}
              onClick={() => void sendLiveAction("plan")}
            >
              <Icon name="spark" /> Plan with Meta
            </button>
            <button
              className="button button--primary"
              disabled={actionPending || state.plan.status !== "ready" || !activeSourceDeck}
              onClick={() => void sendLiveAction("arm")}
            >
              <Icon name="play" /> {state.plan.status === "ready" && !activeSourceDeck ? "Play source to arm" : "Approve & arm"}
            </button>
            {needsRecovery && <button
              className="button button--primary"
              disabled={actionPending || state.decks.filter(deck => deck.playing).length > 1}
              onClick={() => void sendLiveAction("recover")}
              title="Plan forward from the observed position. Never rewind or restart the source deck."
            >
              {state.decks.some(deck => deck.playing) ? "Recover & arm" : "Recover from position"}
            </button>}
            <button
              className="button button--danger"
              disabled={actionPending || state.plan.status !== "executing"}
              onClick={() => void sendLiveAction("takeover")}
            >
              Take over
            </button>
          </div>
        )}
      </footer>
    </div>
  );
}

export default App;

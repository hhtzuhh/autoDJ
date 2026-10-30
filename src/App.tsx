import { useEffect, useMemo, useState } from "react";
import { event, initialState, tracks } from "./demo";
import type {
  CopilotState,
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
  ready: "Ready",
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

function Waveform({ progress, accent }: { progress: number; accent: string }) {
  return (
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
      <div className="waveform__needle" style={{ left: `${progress * 100}%` }} />
    </div>
  );
}

function DeckCard({ deck }: { deck: DeckState }) {
  const progress = deck.track
    ? Math.min(1, deck.positionSeconds / deck.track.durationSeconds)
    : 0;
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
          <Waveform progress={progress} accent={deck.track.accent} />
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
  const source = state.queue.find((track) => track.id === state.plan.sourceTrackId)!;
  const target = state.queue.find((track) => track.id === state.plan.targetTrackId)!;
  const isFixture = state.plan.selectedBy === "fixture";
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
          <span>{state.plan.bars} bars</span>
          <div><i /><i /><i /><i /></div>
        </div>
        <TrackMini track={target} />
      </div>

      <div className="cue-grid">
        <div>
          <span>Exit source</span>
          <strong>Beat {state.plan.sourceExitBeat}</strong>
        </div>
        <div>
          <span>Enter target</span>
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

function QueuePanel({ state }: { state: CopilotState }) {
  const transitions =
    state.transitions
    ?? (state.dataSource === "simulation" ? initialState.transitions : []);
  const edgeAfter = new Map(
    transitions.map((transition) => [
      transition.sourceTrackId,
      transition,
    ]),
  );
  return (
    <section className="panel queue-panel">
      <div className="panel__header">
        <div>
          <span className="eyebrow">Revision {state.queueRevision}</span>
          <h2>Queue</h2>
        </div>
        <span className="count-badge">{state.queue.length} tracks</span>
      </div>
      <div className="queue-list" role="list" aria-label="Performance queue">
        {state.queue.map((track, index) => {
          const transition = edgeAfter.get(track.id);
          return (
            <div className="queue-item" key={track.id} role="listitem">
              <div className={`queue-row queue-row--${track.state}`}>
                <span className="queue-row__number">
                  {track.state === "current" ? <Icon name="play" /> : index + 1}
                </span>
                <div className="queue-row__identity">
                  <strong>{track.title}</strong>
                  <span>{track.bpm} BPM · {track.energy}</span>
                </div>
                <span className="queue-row__state">{track.state}</span>
              </div>
              {transition && <TransitionReadiness edge={transition} />}
            </div>
          );
        })}
      </div>
      <div className="queue-note">
        <Icon name="shield" />
        <p>Only adjacent pairs are analyzed. Reordering invalidates affected edges.</p>
      </div>
    </section>
  );
}

function TransitionReadiness({ edge }: { edge: TransitionEdge }) {
  const selectedBy = edge.selectedBy?.replace("deterministic_fallback", "fallback");
  return (
    <div className={`transition-edge transition-edge--${edge.status}`}>
      <i />
      <span>{edge.status}</span>
      {selectedBy && <span>{selectedBy}</span>}
      <small>{edge.candidateCount} candidates</small>
      {edge.reason && <small title={edge.reason}>details</small>}
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
  const planCanStillExecute = ["draft", "ready", "armed", "executing", "replanning"]
    .includes(state.plan.status);
  const beatsUntilCue = !planCanStillExecute || activeSourceDeck?.beatIndex == null
    ? null
    : state.plan.sourceExitBeat - activeSourceDeck.beatIndex;

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
          sourceExitBeat: 348,
          targetEntryBeat: 228,
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

  const sendLiveAction = async (action: "arm" | "takeover") => {
    setActionPending(true);
    setActionNotice(null);
    try {
      const response = await fetch(`http://127.0.0.1:8765/api/${action}`, {
        method: "POST",
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error ?? `Bridge returned ${response.status}`);
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
            <i /> {state.dataSource === "live" ? "Live Mixxx" : "Simulation"}
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
        <section className="hero-strip">
          <span className="eyebrow">Live control room</span>
          <div className="cue-countdown">
            <span>Next cue</span>
            <strong>{beatsUntilCue == null ? "—" : Math.max(0, beatsUntilCue)}</strong>
            <small>beats</small>
          </div>
        </section>

        <div className="deck-grid">
          {state.decks.map((deck) => <DeckCard deck={deck} key={deck.deck} />)}
        </div>

        <div className="workspace-grid">
          <PlanPanel state={state} />
          <QueuePanel state={state} />
          <EventLog events={state.events} />
        </div>
      </main>

      <footer className="control-dock">
        <div className="control-dock__context">
          <span>{state.dataSource === "live" ? "Live observer" : "Demo controls"}</span>
          <p>
            {state.dataSource === "live"
              ? "Live Mixxx sync is active. Arm and takeover stay behind local lifecycle checks."
              : "Every action is labeled simulation until the live adapter is connected."}
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
              className="button button--primary"
              disabled={actionPending}
              onClick={() => void sendLiveAction("arm")}
            >
              <Icon name="play" /> Approve & arm
            </button>
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

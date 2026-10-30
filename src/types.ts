export type LifecycleStatus =
  | "draft"
  | "ready"
  | "armed"
  | "executing"
  | "completed"
  | "invalidated"
  | "cancelled"
  | "replanning";

export type DataSource = "simulation" | "live";

export interface Track {
  id: string;
  title: string;
  artist: string;
  bpm: number;
  durationSeconds: number;
  energy: "low" | "medium" | "high";
  accent: "violet" | "cyan" | "amber";
}

export interface DeckState {
  deck: 1 | 2;
  track: Track | null;
  playing: boolean;
  beatIndex: number | null;
  positionSeconds: number;
  syncRole: "leader" | "follower" | "off";
}

export interface QueueItem extends Track {
  state: "played" | "current" | "next" | "queued";
}

export interface PreparedTrack {
  id: string;
  title: string;
  artist: string;
  genre: string;
  bpm: number;
  durationSeconds: number;
  analysisStatus: "pending" | "analyzing" | "ready" | "failed";
}

export type TransitionEdgeStatus =
  | "pending"
  | "analyzing"
  | "analyzed"
  | "planning"
  | "ready"
  | "feasible"
  | "blocked"
  | "invalidated";

export interface TransitionEdge {
  id: string;
  sourceTrackId: string;
  targetTrackId: string;
  status: TransitionEdgeStatus;
  candidateCount: number;
  selectedBy: "meta" | "deterministic_fallback" | "fixture" | null;
  reason: string | null;
  queueRevision: number;
  sourceExitBeat?: number;
  targetEntryBeat?: number;
  bars?: number;
}

export interface TransitionPlan {
  id: string;
  status: LifecycleStatus;
  sourceTrackId: string;
  targetTrackId: string;
  sourceCueBeat: number;
  sourceExitBeat: number;
  sourceCuePosition: number;
  sourceMixStartPosition: number;
  sourceMixEndPosition: number;
  targetEntryBeat: number;
  targetEntryPosition: number;
  bars: number;
  transitionMode?: "beat_sync_crossfade" | "clean_break";
  intent: string;
  confidence: number;
  reason: string;
  selectedBy: "meta" | "deterministic_fallback" | "fixture";
  model: string | null;
  queueRevision: number;
  performanceRevision: number;
  invalidationReason: string | null;
}

export interface PerformanceEvent {
  id: string;
  time: string;
  kind: "system" | "human" | "meta" | "executor";
  title: string;
  detail: string;
}

export interface CopilotState {
  dataSource: DataSource;
  connected: boolean;
  queueRevision: number;
  performanceRevision: number;
  crossfader: number;
  decks: [DeckState, DeckState];
  queue: QueueItem[];
  transitions: TransitionEdge[];
  plan: TransitionPlan;
  events: PerformanceEvent[];
  setBuilder?: {
    status: "idle" | "curating" | "analyzing" | "planning" | "ready" | "blocked" | "error";
    prompt: string;
    model: string | null;
    reason: string | null;
    error: string | null;
    queueRevision: number;
    orderedTrackIds: string[];
    queue: PreparedTrack[];
    transitions: TransitionEdge[];
    catalogCount: number;
    activationReady: boolean;
    preparationReady: boolean;
    postAnalysisPlanning: "idle" | "pending" | "running" | "completed" | "failed";
    updatedAt: string;
    activation?: {
      status: "playlist_ready";
      queueRevision: number;
      trackCount: number;
      playlist: string;
    } | null;
  };
}

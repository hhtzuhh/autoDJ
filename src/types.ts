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
}

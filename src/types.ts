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

export interface TransitionPlan {
  id: string;
  status: LifecycleStatus;
  sourceTrackId: string;
  targetTrackId: string;
  sourceExitBeat: number;
  targetEntryBeat: number;
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
  plan: TransitionPlan;
  events: PerformanceEvent[];
}


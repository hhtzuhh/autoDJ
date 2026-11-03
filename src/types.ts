export type LifecycleStatus =
  | "draft"
  | "ready"
  | "armed"
  | "executing"
  | "completed"
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

export interface MusicFeatureResult {
  status: "pending" | "queued" | "running" | "ready" | "failed";
  model?: string; modelReturned?: string; origin?: string;
  content?: string | null; error?: string | null;
  jobId?: string; startedAt?: string; requestStartedAt?: string | null; finishedAt?: string;
  elapsedSeconds?: number; finishReason?: string; rawAvailable?: boolean;
  usage?: {prompt_tokens?: number; completion_tokens?: number; total_tokens?: number} | null;
}

export interface PreparedTrack {
  audioFormat?: string;
  musicFeatures?: MusicFeatureResult | null;
  analysisSummary?: Record<string, unknown> | null;
  analysisOrigin?: "cache" | "computed" | "available" | "pending" | "unknown";
  analysisRun?: {
    jobId: string;
    buildId?: string | null;
    engine: string;
    runner: string;
    status: "running" | "completed" | "failed" | "interrupted";
    startedAt: string;
    finishedAt: string | null;
    durationSeconds: number | null;
    neuralNet: "cache" | "computed" | "unverified";
  } | null;
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
  | "standby"
  | "planning"
  | "planned"
  | "ready"
  | "armed"
  | "executing"
  | "completed"
  | "cancelled"
  | "feasible"
  | "blocked"
  | "needs_human"
  | "expired"
  | "invalidated";

export interface MetaOrigin {
  requestId: string;
  requestedAt: number;
  selectedAt: number;
  candidateId: string | null;
  model: string;
}

export interface TransitionEdge {
  id: string;
  sourceTrackId: string;
  targetTrackId: string;
  status: TransitionEdgeStatus;
  candidateCount: number;
  standbyReady?: boolean;
  selectedBy: "meta" | "deterministic_fallback" | "fixture" | null;
  reason: string | null;
  queueRevision: number;
  sourceExitBeat?: number;
  targetEntryBeat?: number;
  bars?: number;
  metaOrigin?: MetaOrigin | null;
}

export interface TransitionPlan {
  recoveryReason?: string | null;
  metaOrigin?: MetaOrigin | null;
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
  technique?: string;
  performanceActions?: {function: string; start_beat: number; duration_beats?: number; target_bpm?: number; deck?: string; curve?: string; fade_out_seconds?: number}[];
  tempoRoutine?: {common_bpm: number; source_native_bpm: number; target_native_bpm: number; pre_beats: number; post_beats: number} | null;
  intent: string;
  confidence: number;
  reason: string;
  selectedBy: "meta" | "deterministic_fallback" | "fixture" | null;
  model: string | null;
  queueRevision: number;
  performanceRevision: number;
  invalidationReason: string | null;
  planningWindowTrackIds?: string[];
  planningMode?: "pair_clean_break" | "rolling_abc" | "rolling_abcd" | "rolling_tail" | "promoted_rolling_draft";
  activeTransitionLocked?: boolean;
  metaCalls?: {
    id: string;
    scope: "future_buffer" | "current_plan" | "crowd_adaptive";
    model: string;
    trackIds: string[];
    status: "calling" | "succeeded" | "timeout" | "failed" | "discarded";
    attempt: number;
    startedAt: number;
    finishedAt: number | null;
    timeoutSeconds: number;
    detail: string | null;
    phase?: "preparing_request" | "waiting_for_response" | "reading_response" | "validating_response" | "finished";
    reasoningEffort?: string;
    maxTokens?: number;
    responseHeadersSeconds?: number;
    responseSeconds?: number;
    requestBytes?: number;
    promptTokens?: number | null;
    completionTokens?: number | null;
    reasoningTokens?: number | null;
  }[];
  refillTiming?: {
    budgetSeconds: number;
    minimumRunwaySeconds: number | null;
    status: "unknown" | "sufficient" | "at_risk";
  } | null;
  futurePlanError?: string | null;
  futureRetryInSeconds?: number;
  futurePlanStatus?: "idle" | "planning" | "ready" | "degraded";
  futurePlanningWindowTrackIds?: string[];
  followingDraft?: {
    technique?: string | null;
    sourceTrackId: string;
    targetTrackId: string;
    sourceExitBeat: number;
    targetEntryBeat: number;
    bars: number;
    status: string;
  } | null;
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
  autoArmEnabled: boolean;
  queueRevision: number;
  performanceRevision: number;
  crossfader: number;
  decks: [DeckState, DeckState];
  queue: QueueItem[];
  transitions: TransitionEdge[];
  plan: TransitionPlan;
  events: PerformanceEvent[];
  flowTelemetry?: LiveFlowTelemetry;
  setBuilder?: {
    techniques?: {id: string; mode: string; description: string; tradeoff: string}[];
    actionCatalog?: Record<string, {purpose: string; parameters: Record<string, unknown>; timing: string; tradeoff: string; optional: boolean}>;
    performancePlan?: {
      status: "idle" | "building" | "ready" | "stale" | "failed" | "started";
      error?: string | null;
      planId?: string;
      model?: string;
      reason?: string;
      trackIds?: string[];
      transitions?: {source_track_id: string; target_track_id: string; source_exit_beat_index: number; target_entry_beat_index: number; transition_bars: number; technique: string; routine?: {common_bpm?: number; actions: {function: string; start_beat: number; duration_beats?: number; target_bpm?: number; deck?: string; curve?: string; fade_out_seconds?: number}[]}}[];
    };
    selectionOnly?: boolean;
    selectionSource?: {kind: "mixxx_playlist"; filename: string};
    preserveSelectedOrder?: boolean;
    transitionsRequested?: boolean;
    buildPhase?: "music_features" | "analysis_only" | "library_ready" | "library_analysis" | "curation" | "selection_complete" | "transitions";
    library?: PreparedTrack[];
    buildId?: string;
    curatorCalls?: CuratorCall[];
    status: "idle" | "curating" | "analyzing" | "planning" | "ready" | "selected" | "blocked" | "error";
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

export interface CuratorCall {
  id?: string;
  stage?: "curation" | "post_analysis";
  buildId?: string;
  model: string;
  status: "calling" | "completed" | "failed" | "truncated";
  attempt: number;
  startedAt: string;
  finishedAt?: string;
  maxTokens: number;
  responseSeconds?: number;
  completionTokens?: number;
  reasoningTokens?: number;
}

export interface LiveFlowTelemetry {
  sessionId: string;
  startedAt: number;
  observerUpdatedAt: number | null;
  ownedPlanId: string | null;
  ownedPlanStatus: LifecycleStatus | null;
  executorRunning: boolean;
  continuousSetActive: boolean;
  activationInProgress: boolean;
  rotationInProgress: boolean;
  completedTransitions: number;
  lastExecution: {planId: string; status: LifecycleStatus; observedAt: number} | null;
}

export interface WebcamFlowState {
  running: boolean;
  starting: boolean;
  stage: string;
  error: string | null;
  job: {id: string; status: string; stage: string; model?: string; modelSeconds?: number;
    requestedAt?: string; finishedAt?: string; remoteFileCleanup?: string;
    observation?: {summary_en: string}; cue?: {value: string}} | null;
  adaptive: {enabled: boolean; status: string; reason: string;
    decision?: {reason: string; planId: string; queueChanged: boolean; observationId: string};
    lastApplied?: {reason: string; planId: string; executionStatus?: string; observationId: string}} | null;
}

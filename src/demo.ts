import type {
  CopilotState,
  PerformanceEvent,
  QueueItem,
  Track,
} from "./types";

export const tracks: Record<string, Track> = {
  boots: {
    id: "demo-boots",
    title: "Boots & Cats",
    artist: "Development fixture",
    bpm: 128,
    durationSeconds: 196,
    energy: "medium",
    accent: "cyan",
  },
  bass: {
    id: "demo-bass",
    title: "Bass Addict",
    artist: "Development fixture",
    bpm: 130,
    durationSeconds: 188,
    energy: "high",
    accent: "violet",
  },
  boomerang: {
    id: "demo-boomerang",
    title: "Boomerang",
    artist: "Development fixture",
    bpm: 135,
    durationSeconds: 146,
    energy: "high",
    accent: "amber",
  },
};

const now = (offsetSeconds = 0) => {
  const date = new Date(Date.now() - offsetSeconds * 1000);
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
};

export const event = (
  id: string,
  kind: PerformanceEvent["kind"],
  title: string,
  detail: string,
): PerformanceEvent => ({ id, kind, title, detail, time: now() });

const queue: QueueItem[] = [
  { ...tracks.boots, state: "played" },
  { ...tracks.bass, state: "current" },
  { ...tracks.boomerang, state: "next" },
];

export const initialState: CopilotState = {
  dataSource: "simulation",
  connected: true,
  autoArmEnabled: false,
  queueRevision: 3,
  performanceRevision: 18,
  crossfader: 1,
  decks: [
    {
      deck: 1,
      track: tracks.boots,
      playing: false,
      beatIndex: 192,
      positionSeconds: 91.4,
      syncRole: "off",
    },
    {
      deck: 2,
      track: tracks.bass,
      playing: true,
      beatIndex: 272,
      positionSeconds: 126.1,
      syncRole: "leader",
    },
  ],
  queue,
  transitions: [
    {
      id: "fixture-edge-boots-bass",
      sourceTrackId: tracks.boots.id,
      targetTrackId: tracks.bass.id,
      status: "analyzed",
      candidateCount: 25,
      selectedBy: null,
      reason: null,
      queueRevision: 3,
    },
    {
      id: "fixture-edge-bass-boomerang",
      sourceTrackId: tracks.bass.id,
      targetTrackId: tracks.boomerang.id,
      status: "ready",
      candidateCount: 25,
      selectedBy: "fixture",
      reason: "Demo-only selection; no live model call was made.",
      queueRevision: 3,
    },
  ],
  plan: {
    id: "fixture-q3-bass-boomerang",
    status: "ready",
    sourceTrackId: tracks.bass.id,
    targetTrackId: tracks.boomerang.id,
    sourceCueBeat: 280,
    sourceExitBeat: 288,
    sourceCuePosition: 0.686,
    sourceMixStartPosition: 0.706,
    sourceMixEndPosition: 0.784,
    targetEntryBeat: 72,
    targetEntryPosition: 0.218,
    bars: 8,
    intent: "Maintain energy",
    confidence: 0.86,
    reason:
      "Preserves the high-energy phrase while keeping a feasible path into the next track.",
    selectedBy: "fixture",
    model: null,
    queueRevision: 3,
    performanceRevision: 18,
    invalidationReason: null,
  },
  events: [
    {
      id: "evt-3",
      time: now(2),
      kind: "system",
      title: "Plan ready",
      detail: "Local validation accepted the constrained transition.",
    },
    {
      id: "evt-2",
      time: now(4),
      kind: "meta",
      title: "Fixture decision loaded",
      detail: "UI-only simulation — no live model request was made.",
    },
    {
      id: "evt-1",
      time: now(8),
      kind: "system",
      title: "Queue revision 3",
      detail: "Three analyzed tracks; two adjacent pair caches ready.",
    },
  ],
};

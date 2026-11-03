import type {InTrackAction, TransitionPlan} from './types';

export interface MemoryMoment {
  planId?: string; sourceTrack?: string; targetTrack?: string; sourceTitle?: string; targetTitle?: string;
  technique?: string; sourceExitBeat?: number; targetEntryBeat?: number; commonBpm?: number;
  transitionActions?: TransitionPlan['performanceActions'] | null; inTrackActions?: InTrackAction[] | null;
  transitionStatus: string; executionEvidence: boolean; lifecycleStatus?: string;
  inTrackActiveDuringClip: InTrackAction[]; inTrackCompletedDuringClip: InTrackAction[];
}
export interface MemoryEpisode {
  id: string; setRunId?: string | null; jobId?: string; capturedAt: string; durationSeconds: number;
  ageSeconds?: number; fresh?: boolean; association: string; playbackState: 'playing' | 'stopped' | 'unknown';
  tracks: {id: string; title?: string | null; deck: number; firstBeat?: number; lastBeat?: number}[];
  moments: MemoryMoment[];
  crowd: {summary?: string; visibility?: string; cameraMotion?: string; signalSource?: string; signalGoal?: string | null;
    movement_spread: string; movement_amplitude: string; movement_continuity: string; within_clip_trend: string};
  use: string;
}
export function transitionAtCapture(moment: MemoryMoment): string {
  return ({waiting_for_handoff: 'Handoff had not started', handoff_started_during_clip: 'Handoff started during this clip',
    handoff_in_progress: 'Handoff in progress', completed_during_clip: 'Routine completed during this clip',
    completed_before_clip: 'Routine completed before this clip', interrupted_during_clip: 'Interrupted during this clip',
    interrupted_before_clip: 'Already interrupted before this clip'} as Record<string, string>)[moment.transitionStatus]
    ?? 'Selected plan · execution at capture unconfirmed';
}
export function inTrackAtCapture(moment: MemoryMoment): string {
  if (moment.inTrackActiveDuringClip.length) return `Observed execution: ${moment.inTrackActiveDuringClip.map(a => a.function.replaceAll('_', ' ')).join(', ')}`;
  if (Array.isArray(moment.inTrackActions) && !moment.inTrackActions.length) return 'None selected for this song';
  if (moment.inTrackActions?.length) return `${moment.inTrackActions.length} planned · no action execution confirmed in this clip`;
  return 'Not recorded · cannot confirm whether an action occurred';
}

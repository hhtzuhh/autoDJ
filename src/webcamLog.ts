export interface CameraJob {
  setRunId?: string | null;
  id: string; status: string; stage: string; model?: string; modelSeconds?: number;
  requestedAt?: string; finishedAt?: string; modelRequestedAt?: string; modelRespondedAt?: string;
  captureTime?: string; webcamSession?: string; clipAvailable?: boolean; rawResponseAvailable?: boolean;
  error?: string; remoteFileCleanup?: string;
  observation?: {summary_en: string; quality: {visibility: string; camera_motion: string};
    regions?: {id: string; movement_spread: {value: string}; movement_amplitude: {value: string};
      movement_continuity: {value: string}; within_clip_trend: {value: string}}[];
    evidence: {id: string; start_seconds: number; end_seconds: number; observation: string}[]};
  cue?: {value: string};
}

export interface CaptureEntry {
  setRunId?: string | null;
  id: string; sessionId: string; capturedAt: string;
  phase: 'recording' | 'uploading' | 'submitted' | 'stopped' | 'failed';
  jobId?: string; job?: CameraJob; note?: string;
}

export function mergeObservationLog(captures: CaptureEntry[], jobs: CameraJob[]): CaptureEntry[] {
  const rows = captures.map(row => ({...row}));
  for (const job of jobs) {
    const existing = rows.find(row => row.jobId === job.id ||
      (row.sessionId === job.webcamSession && row.capturedAt === job.captureTime));
    if (existing) {
      const terminal = existing.job && ['completed', 'failed'].includes(existing.job.status);
      // A slower history poll must not regress a result already received directly.
      Object.assign(existing, {setRunId: job.setRunId ?? existing.setRunId, jobId: job.id, job: terminal && job.status === 'running' ? existing.job : job,
        phase: 'submitted', note: undefined});
    }
    else rows.push({id: job.id, setRunId: job.setRunId, sessionId: job.webcamSession ?? '', capturedAt: job.captureTime ?? job.requestedAt ?? '',
      phase: 'submitted', jobId: job.id, job});
  }
  return rows.sort((a, b) => b.capturedAt.localeCompare(a.capturedAt));
}

export function observationStatus(row: CaptureEntry): string {
  const job = row.job;
  if (job?.status === 'failed') return 'Failed';
  if (job?.status === 'completed') return 'Completed';
  if (job?.modelRespondedAt || job?.observation) return 'Response received · finishing cleanup';
  if (job?.modelRequestedAt) return 'Calling Muse Spark…';
  if (job) return job.stage;
  return {recording: 'Recording 6 seconds…', uploading: 'Uploading clip…', submitted: 'Submitted · waiting for status',
    stopped: 'Capture stopped', failed: 'Capture / upload failed'}[row.phase];
}

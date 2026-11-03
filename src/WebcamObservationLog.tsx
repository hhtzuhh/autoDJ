import {useEffect, useState} from 'react';
import type {MemoryEvent} from './PlanningMemory';
import {belongsToRun} from './crowdChanges';
import {mergeObservationLog, observationStatus} from './webcamLog';
import type {CameraJob, CaptureEntry} from './webcamLog';

const timeLabel = (value?: string) => value ? new Date(value).toLocaleTimeString('en-GB') : '—';
function RawResponse({jobId}: {jobId: string}) {
  const [raw, setRaw] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  async function load() {
    if (raw || loading) return;
    setLoading(true); setError(null);
    try {
      const response = await fetch(`/api/crowd/jobs/${jobId}/response`, {signal: AbortSignal.timeout(15_000)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? 'Raw response unavailable.');
      setRaw(JSON.stringify(data, null, 2));
    } catch (cause) {setError(cause instanceof Error ? cause.message : 'Raw response unavailable.');}
    finally {setLoading(false);}
  }
  return <details onToggle={event => {if (event.currentTarget.open) void load();}}>
    <summary>Raw Meta response</summary>
    {loading && <p className="crowd-note">Loading saved response…</p>}
    {error && <p className="crowd-warning">{error} <button onClick={() => void load()}>Retry</button></p>}
    {raw && <pre>{raw}</pre>}
  </details>;
}

export function WebcamObservationLog({captures, scope = '', feedback = []}: {captures: CaptureEntry[]; scope?: string; feedback?: MemoryEvent[]}) {
  const [jobs, setJobs] = useState<CameraJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const response = await fetch('/api/crowd/jobs?mode=webcam', {signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)])});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error ?? 'Observation history unavailable.');
        if (!controller.signal.aborted) {setJobs(data.jobs); setError(null);}
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Observation history unavailable.');
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 1500);
      }
    }
    void poll();
    return () => {controller.abort(); clearTimeout(timer);};
  }, []);
  const rows = mergeObservationLog(captures, jobs).filter(row => belongsToRun(row, scope));
  return <article className="crowd-region webcam-observation-log">
    {error && <p className="crowd-warning" role="status">{error} Saved entries remain visible; status updates will retry.</p>}
    <details className="observation-disclosure">
    <summary><strong>1 · Observed · Capture log</strong><span>{rows.length} captures{rows[0] ? ` · ${timeLabel(rows[0].capturedAt)} · ${observationStatus(rows[0])}` : ' · no captures yet'}</span></summary>
    <p className="crowd-note">{rows.length} captures · newest first. Every observation appears here, whether Adaptive DJ is on or off. Record 6 seconds → wait for Meta and cleanup → pause 5 seconds → repeat.</p>
    {!rows.length && <p className="crowd-note">No captures recorded in this set run.</p>}
    <ol className="webcam-log-list" aria-label="Webcam capture history">
      {rows.map(row => {
        const job = row.job;
        const dj = [...feedback].reverse().find(e => e.kind === 'feedback_status' && e.data.jobId === row.jobId);
        const modelStarted = job?.modelRequestedAt;
        const responseKnown = Boolean(job?.modelRespondedAt || job?.rawResponseAvailable || job?.observation);
        return <li key={row.id} className="webcam-log-entry">
          <div className="webcam-log-heading"><time dateTime={row.capturedAt}>{timeLabel(row.capturedAt)}</time>
            <strong>{observationStatus(row)}</strong></div>
          <p className="crowd-note">{row.setRunId ?? 'Unassigned'} · {job?.model ?? 'Muse Spark'} · {row.jobId ? `Job ${row.jobId.slice(0, 8)}` : 'Local capture'}<br />
            {modelStarted ? `Meta call sent ${timeLabel(modelStarted)}` : responseKnown ? 'Meta response received · request timestamp unavailable for this older record'
              : job?.status === 'failed' ? 'Meta call not confirmed · see error below' : row.phase === 'uploading' || row.phase === 'submitted' ? 'Waiting for service confirmation of the Meta call' : 'Meta call not sent'}
            {job?.modelRespondedAt && ` · response ${timeLabel(job.modelRespondedAt)}`}
            {job?.modelSeconds != null && ` · ${job.modelSeconds.toFixed(1)}s model time`}
          </p>
          {job?.observation && <>
            <p className="webcam-log-summary">{job.observation.summary_en}</p>
            <p className="crowd-note">Gesture: {job.cue?.value ?? 'unknown'} · Visibility: {job.observation.quality.visibility} · Camera: {job.observation.quality.camera_motion}</p>
            {job.observation.regions?.map(region => <p className="crowd-note" key={region.id}>Movement: {region.movement_spread.value} · Amplitude: {region.movement_amplitude.value} · Continuity: {region.movement_continuity.value} · Trend: {region.within_clip_trend.value}</p>)}
          </>}
          {job?.status === "completed" && <p className="crowd-note"><strong>DJ feedback:</strong> {dj?.data.reason ?? "No DJ feedback recorded for this observation."}</p>}
          {(job?.error || row.note) && <p className="crowd-warning">{job?.error ?? row.note}</p>}
          {job?.remoteFileCleanup === 'failed' && <p className="crowd-warning">Remote file cleanup failed. Capture has stopped.</p>}
          {job?.clipAvailable && <details><summary>Captured clip · {job.observation?.evidence.length ?? 0} evidence notes</summary>
            <video controls muted playsInline preload="none" src={`/api/crowd/jobs/${job.id}/clip`} aria-label={`Captured clip at ${timeLabel(row.capturedAt)}`} />
            <ul>{job.observation?.evidence.map(e => <li key={e.id}>{e.start_seconds}–{e.end_seconds}s: {e.observation}</li>)}</ul>
          </details>}
          {job?.rawResponseAvailable && <RawResponse jobId={job.id} />}
          {job && <details><summary>Structured observation & request status</summary><pre>{JSON.stringify(job, null, 2)}</pre></details>}
        </li>;
      })}
    </ol>
    </details>
  </article>;
}

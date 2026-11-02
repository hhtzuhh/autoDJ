import { useEffect, useRef, useState } from "react";

type Metric = { value: string; evidence_ids: string[] };
type Evidence = { id: string; region_id: string; start_seconds: number; end_seconds: number; observation: string };
type Observation = {
  schema_version: "crowd-v1";
  quality: { visibility: string; camera_motion: string; limitations: string[] };
  excluded_subjects: string[];
  regions: {
    id: string; location: string; audience_basis: string; visibility: string; movement_types: string[];
    movement_spread: Metric; movement_amplitude: Metric; movement_continuity: Metric;
    within_clip_trend: Metric; region_flow: Metric;
  }[];
  evidence: Evidence[];
  summary_en: string;
};
type Job = {
  id: string; status: "running" | "completed" | "failed"; stage: string; model: string;
  requestedAt: string; finishedAt?: string; sourceStartSeconds: number; durationSeconds?: number;
  modelSeconds?: number; clipAvailable?: boolean; observation?: Observation; error?: string;
  remoteFileCleanup?: "deleted" | "failed" | "not_uploaded"; storageWarning?: string;
};
type Service = { keyConfigured: boolean; activeJobId: string | null; model: string };
const API = "/api/crowd";
const SAVED_JOB = "crowd-observation-job-v1";
const humanize = (value: string) => value.replaceAll("_", " ");
const metrics = [
  ["movement_spread", "Movement spread"], ["movement_amplitude", "Amplitude"],
  ["movement_continuity", "Continuity"], ["within_clip_trend", "Within-clip trend"],
  ["region_flow", "Region flow"],
] as const;

async function getJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {...options, signal: options?.signal ?? AbortSignal.timeout(60_000)});
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload) throw new Error(payload?.error ?? "Observation service is unavailable. Start it, then reconnect.");
  return payload as T;
}

// Independent component: no CopilotState, queue mutations, or playback callbacks.
export function CrowdObservation() {
  const [service, setService] = useState<Service | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [sourceURL, setSourceURL] = useState<string>();
  const [start, setStart] = useState("0");
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [videoNotice, setVideoNotice] = useState<string | null>(null);
  const [connectionAttempt, setConnectionAttempt] = useState(0);
  const [pollAttempt, setPollAttempt] = useState(0);
  const video = useRef<HTMLVideoElement>(null);
  const stopAt = useRef<number | null>(null);

  useEffect(() => {
    if (!file) { setSourceURL(undefined); return; }
    const url = URL.createObjectURL(file); setSourceURL(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  useEffect(() => {
    const controller = new AbortController();
    void getJSON<Service>(`${API}/status`, {signal: controller.signal}).then(status => {
      setService(status); setError(null);
      let previous: string | null = null;
      try { previous = sessionStorage.getItem(SAVED_JOB); } catch { /* Storage is optional. */ }
      const id = status.activeJobId ?? previous;
      if (id) { setJobId(id); setBusy(Boolean(status.activeJobId)); setPollAttempt(value => value + 1); }
    }).catch(() => {
      if (!controller.signal.aborted) { setService(null); setError("Observation service is offline. Start it, then reconnect."); }
    });
    return () => controller.abort();
  }, [connectionAttempt]);

  useEffect(() => {
    if (!jobId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await getJSON<Job>(`${API}/jobs/${jobId}`, {signal: controller.signal});
        setJob(result); setBusy(result.status === "running"); setError(null);
        if (result.status === "running") timer = setTimeout(() => void poll(), 1200);
      } catch (cause) {
        if (controller.signal.aborted) return;
        setError(`${cause instanceof Error ? cause.message : "Could not read observation."} Reconnect to check its status; this does not submit another analysis.`);
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [jobId, pollAttempt]);

  function chooseFile(next: File | null) {
    setFile(next); setJob(null); setJobId(null); setError(null); setVideoNotice(null); stopAt.current = null;
    try { sessionStorage.removeItem(SAVED_JOB); } catch { /* Storage is optional. */ }
    if (next && (next.size === 0 || next.size > 40 * 1024 * 1024)) setError("Choose a nonempty video up to 40 MB. Trim larger files first.");
  }
  async function analyze() {
    if (!file || busy) return;
    setBusy(true); setJob(null); setJobId(null); setError(null); setVideoNotice(null); stopAt.current = null;
    video.current?.pause();
    try {
      const accepted = await getJSON<{id: string}>(`${API}/analyze?start=${encodeURIComponent(start)}`, {
        method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: file,
      });
      try { sessionStorage.setItem(SAVED_JOB, accepted.id); } catch { /* Storage is optional. */ }
      setJobId(accepted.id);
    } catch (cause) {
      setBusy(false); setError(cause instanceof Error ? cause.message : "Upload failed.");
      // A lost upload response may still have started a job. Check status before
      // permitting another submission; this GET never retries a model request.
      try {
        const status = await getJSON<Service>(`${API}/status`); setService(status);
        if (status.activeJobId) { setJobId(status.activeJobId); setBusy(true); }
      } catch { setService(null); }
    }
  }
  function reviewEvidence(evidence: Evidence) {
    const player = video.current;
    if (!player || !job?.clipAvailable) return;
    player.currentTime = evidence.start_seconds; stopAt.current = evidence.end_seconds;
    void player.play().catch(() => setVideoNotice("Press Play to review the selected evidence."));
  }
  function download() {
    if (!job) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(job, null, 2)], {type: "application/json"}));
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = `crowd-observation-${job.id}.json`;
    anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const validInput = file && file.size > 0 && file.size <= 40 * 1024 * 1024
    && start.trim() !== "" && Number.isFinite(Number(start)) && Number(start) >= 0 && Number(start) <= 86400;
  const observation = job?.status === "completed" ? job.observation : undefined;
  const clipURL = job?.clipAvailable ? `${API}/jobs/${job.id}/clip` : undefined;

  return <section className="panel crowd-panel" aria-labelledby="crowd-title">
    <div className="panel__header">
      <div><span className="eyebrow">Meta vision · Experimental</span><h2 id="crowd-title">Crowd observation</h2></div>
      <span className="crowd-badge">Observation only</span>
    </div>
    <p className="crowd-intro">See visible movement across audience areas. Upload a clip for Muse Spark to describe; queue and transitions stay under their existing controls.</p>
    <div className="crowd-controls">
      <label>Video file <span>MP4, MOV, WebM or AVI · up to 40 MB</span>
        <input type="file" accept="video/mp4,video/quicktime,video/webm,video/x-msvideo,.mkv" disabled={busy}
          onChange={event => chooseFile(event.target.files?.[0] ?? null)} />
      </label>
      <label>Start (seconds)<input type="number" min="0" max="86400" step="0.1" value={start} disabled={busy}
        onChange={event => setStart(event.target.value)} /></label>
      <button className="button button--primary" disabled={!service?.keyConfigured || !validInput || busy} onClick={() => void analyze()}>
        {busy ? "Analyzing clip…" : "Analyze with Spark"}
      </button>
    </div>
    <p className="crowd-note">Analyze sends up to 8 seconds of silent video to Meta using your API credits. The service attempts remote deletion afterward and retains local artifacts. This is an uploaded clip, not a live camera feed.</p>
    <div className="crowd-service" role="status" aria-live="polite">
      <span>{busy ? job?.stage ?? "Uploading video…" : job?.stage ?? (service?.keyConfigured ? "Ready for a clip" : service ? "API key is not configured" : "Observation service unavailable")}</span>
      <button className="button button--ghost" disabled={busy && !error} onClick={() => setConnectionAttempt(value => value + 1)}>Reconnect</button>
    </div>
    {!service && <p className="crowd-note">Run <code>npm run crowd</code> alongside the dashboard to connect.</p>}
    {service && !service.keyConfigured && <p className="crowd-warning">Set MODEL_API_KEY in the observation service environment or .env, then restart the service.</p>}
    {error && <p className="crowd-warning" role="alert">{error}</p>}
    {job?.error && <p className="crowd-warning" role="alert">{job.error}</p>}
    {job?.remoteFileCleanup === "failed" && <p className="crowd-warning" role="alert">Remote file deletion failed. Check Meta Files; local remote-file.json records the file ID.</p>}
    {job?.storageWarning && <p className="crowd-warning" role="alert">{job.storageWarning}</p>}
    <div className="crowd-content">
      <div className="crowd-preview">
        {clipURL || sourceURL ? <video ref={video} src={clipURL ?? sourceURL} controls muted playsInline preload="metadata"
          aria-label={clipURL ? "Analyzed silent clip" : "Selected video preview"}
          onError={() => setVideoNotice("This browser could not play the preview. The service may still be able to process the video.")}
          onTimeUpdate={() => { if (video.current && stopAt.current !== null && video.current.currentTime >= stopAt.current) { video.current.pause(); stopAt.current = null; } }} />
          : <div className="crowd-empty"><span>Audience view</span><p>Choose a video to inspect movement, evidence and visibility.</p></div>}
        {videoNotice && <p className="crowd-note">{videoNotice}</p>}
        {job && <div className="crowd-provenance">
          <strong>Uploaded clip · {job.model}</strong>
          <span>Source window: {job.sourceStartSeconds.toFixed(1)}–{(job.sourceStartSeconds + (job.durationSeconds ?? 8)).toFixed(1)}s</span>
          <span>Requested: <time dateTime={job.requestedAt}>{new Date(job.requestedAt).toLocaleString()}</time></span>
          {job.finishedAt && <span>Analysis finished: <time dateTime={job.finishedAt}>{new Date(job.finishedAt).toLocaleString()}</time></span>}
          <span>Recording time unknown · not synchronized with Mixxx</span>
          {job.modelSeconds != null && <span>Model request: {job.modelSeconds.toFixed(1)}s · human review pending</span>}
          {job.remoteFileCleanup && <span>Remote upload: {humanize(job.remoteFileCleanup)}</span>}
        </div>}
      </div>
      <div className="crowd-results">
        {!observation ? <div className="crowd-empty"><span>{busy ? "Observing the selected window" : "Evidence before interpretation"}</span><p>Regional movement and timestamp evidence appear after analysis. Unknown is a valid result.</p></div>
          : <>
            <p className="crowd-summary">{observation.summary_en}</p>
            <p className="crowd-note">Visibility: {observation.quality.visibility} · Camera: {observation.quality.camera_motion}. Model judgments, not measured enjoyment or musical preference.</p>
            {observation.quality.limitations.length > 0 && <ul className="crowd-limitations">{observation.quality.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
            {observation.regions.length === 0 && <p className="crowd-note">No assessable audience region was identified. This does not mean the venue was empty.</p>}
            {observation.regions.map(region => <article className="crowd-region" key={region.id}>
              <h3>{region.location}</h3>
              <p className="crowd-note">{region.audience_basis} · Visibility: {region.visibility}</p>
              <dl className="crowd-metrics">{metrics.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{humanize(region[key].value)}</dd></div>)}</dl>
              <p className="crowd-note">Visible motion: {region.movement_types.map(humanize).join(", ")}</p>
              <details><summary>Review evidence · clip-relative timestamps</summary>
                <ul className="crowd-evidence">{observation.evidence.filter(item => item.region_id === region.id).map(item => <li key={item.id}>
                  <button className="crowd-time" onClick={() => reviewEvidence(item)} aria-label={`Play evidence ${item.start_seconds} to ${item.end_seconds} seconds`}>{item.start_seconds}–{item.end_seconds}s</button>
                  <span>{item.observation}<small>{metrics.filter(([key]) => region[key].evidence_ids.includes(item.id)).map(([, label]) => label).join(" · ")}</small></span>
                </li>)}</ul>
              </details>
            </article>)}
            {observation.excluded_subjects.length > 0 && <details className="crowd-excluded"><summary>Excluded or uncertain audience membership</summary><ul>{observation.excluded_subjects.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
            <p className="crowd-note">Trends cover this clip only. Regions are model-defined; historical comparisons and music-event attribution are unavailable. Everyday gestures may affect movement labels.</p>
          </>}
        {job && job.status !== "running" && <div className="crowd-export">
          <button className="button button--ghost" onClick={download}>Download observation</button>
          <details><summary>Observation JSON</summary><pre>{JSON.stringify(job, null, 2)}</pre></details>
        </div>}
      </div>
    </div>
  </section>;
}

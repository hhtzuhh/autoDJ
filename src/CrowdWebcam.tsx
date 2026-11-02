import {useEffect, useRef, useState} from "react";
import type {WebcamFlowState} from "./types";

type Decision = {reason: string; model: string; before: string[]; after: string[]; queueChanged: boolean;
  lockedThrough: number; sourceTrack: string; targetTrack: string; technique: string; bassSwap: boolean; bars: number; planId: string; observationId: string; executionStatus?: string};
type Adaptive = {enabled: boolean; status: string; reason: string; decision?: Decision; lastApplied?: Decision;
  observation?: {summary: string; goal: string | null; source: string}};
type CameraJob = {model?: string; modelSeconds?: number; requestedAt?: string; finishedAt?: string; id: string; status: string; stage: string; error?: string; remoteFileCleanup?: string;
  observation?: {summary_en: string; quality: {visibility: string; camera_motion: string}; evidence: {
    id: string; start_seconds: number; end_seconds: number; observation: string}[]};
  cue?: {value: string}};
async function json<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {...options, signal: options?.signal ?? AbortSignal.timeout(15_000)});
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) throw new Error(data?.error ?? "Local service unavailable.");
  return data as T;
}
function post<T>(route: string, data: unknown) {
  return json<T>(`/api/adaptive/${route}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
}
function stopRemote(session: string | null) {
  if (!session) return;
  // Lease expiry is the fallback if unload delivery or the bridge fails.
  navigator.sendBeacon("/api/adaptive/config", new Blob([JSON.stringify({enabled: false, session})], {type: "application/json"}));
}
const delay = (ms: number, signal: AbortSignal) => new Promise<void>((resolve, reject) => {
  if (signal.aborted) {reject(new Error("Stopped")); return;}
  const abort = () => {clearTimeout(timer); reject(new Error("Stopped"));};
  const timer = setTimeout(() => {signal.removeEventListener("abort", abort); resolve();}, ms);
  signal.addEventListener("abort", abort, {once: true});
});
function record(stream: MediaStream, signal: AbortSignal): Promise<Blob> {
  return new Promise((resolve, reject) => {
    const type = ["video/webm;codecs=vp8", "video/webm", "video/mp4"].find(t => MediaRecorder.isTypeSupported(t));
    const recorder = new MediaRecorder(stream, type ? {mimeType: type, videoBitsPerSecond: 700_000} : undefined);
    const chunks: Blob[] = [];
    let timer: ReturnType<typeof setTimeout>;
    const stop = () => {if (recorder.state !== "inactive") recorder.stop();};
    recorder.ondataavailable = e => {if (e.data.size) chunks.push(e.data);};
    recorder.onerror = () => {clearTimeout(timer); signal.removeEventListener("abort", stop); reject(new Error("Camera recording failed."));};
    recorder.onstop = () => {
      clearTimeout(timer); signal.removeEventListener("abort", stop);
      if (signal.aborted) reject(new Error("Stopped"));
      else resolve(new Blob(chunks, {type: recorder.mimeType}));
    };
    signal.addEventListener("abort", stop, {once: true});
    recorder.start(); timer = setTimeout(stop, 6000);
    if (signal.aborted) stop();
  });
}

export function CrowdWebcam({live, tracks, onTelemetry}: {live: boolean; tracks: {id: string; title: string}[]; onTelemetry?: (value: WebcamFlowState) => void}) {
  const [running, setRunning] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stage, setStage] = useState("Camera is off");
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<CameraJob | null>(null);
  const [adaptive, setAdaptive] = useState<Adaptive | null>(null);
  const [changing, setChanging] = useState(false);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [device, setDevice] = useState("");
  const preview = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const controller = useRef<AbortController | null>(null);
  const session = useRef<string | null>(null);
  const enabled = useRef(false);
  const mounted = useRef(true);
  const startingRef = useRef(false);
  const liveRef = useRef(live); liveRef.current = live;

  useEffect(() => {
    onTelemetry?.({running, starting, stage, error, job, adaptive});
  }, [running, starting, stage, error, job, adaptive, onTelemetry]);

  function stop() {
    const wasActive = Boolean(controller.current || stream.current || session.current);
    const ownedSession = enabled.current ? session.current : null;
    enabled.current = false;
    controller.current?.abort(); controller.current = null;
    stream.current?.getTracks().forEach(track => {track.onended = null; track.onmute = null; track.stop();}); stream.current = null;
    session.current = null;
    stopRemote(ownedSession);
    if (mounted.current) {
      setRunning(false);
      if (wasActive) setStage("Camera stopped. An already submitted observation may finish cleanup.");
      setAdaptive(previous => previous ? {...previous, enabled: false, status: "off", reason: "Stopped with camera."} : null);
    }
  }
  useEffect(() => {
    mounted.current = true;
    const hidden = () => {if (document.hidden) stop();};
    const ended = () => stop();
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", ended);
    return () => {mounted.current = false; document.removeEventListener("visibilitychange", hidden); window.removeEventListener("pagehide", ended); stop();};
  }, []);

  useEffect(() => {
    if (!live && enabled.current) {
      enabled.current = false; stopRemote(session.current);
      setAdaptive(previous => previous ? {...previous, enabled: false, status: "off", reason: "Live DJ connection lost."} : null);
    }
  }, [live]);

  useEffect(() => {
    if (!running) return;
    let busy = false;
    const beat = async () => {
      if (!enabled.current || busy || !session.current) return;
      busy = true;
      const heartbeatSession = session.current;
      try {
        const state = await post<Adaptive>("heartbeat", {session: heartbeatSession});
        if (mounted.current && enabled.current && session.current === heartbeatSession) {setAdaptive(state); enabled.current = state.enabled;}
      } catch (cause) {
        if (session.current !== heartbeatSession) return;
        enabled.current = false; stopRemote(heartbeatSession);
        if (mounted.current) {setError(cause instanceof Error ? cause.message : "Adaptive heartbeat failed."); setAdaptive(previous => previous ? {...previous, enabled: false, status: "off"} : null);}
      } finally {busy = false;}
    };
    const timer = setInterval(() => void beat(), 5000);
    return () => clearInterval(timer);
  }, [running]);

  async function captureLoop(media: MediaStream, signal: AbortSignal, id: string) {
    while (!signal.aborted) {
      setStage("Recording 6 seconds · silent webcam");
      const capturedAt = new Date().toISOString();
      const clip = await record(media, signal);
      if (signal.aborted) return;
      setStage("Sending webcam clip to Spark…");
      const query = new URLSearchParams({mode: "webcam", session: id, capturedAt});
      const accepted = await json<{id: string}>(`/api/crowd/analyze?${query}`, {
        method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: clip, signal,
      });
      let result: CameraJob;
      const deadline = Date.now() + 600_000;
      do {
        await delay(1200, signal);
        result = await json<CameraJob>(`/api/crowd/jobs/${accepted.id}`, {signal});
        setStage(result.stage); setJob(result);
        if (Date.now() > deadline) throw new Error("Observation timed out. Stop and inspect the local service.");
      } while (result.status === "running");
      if (result.status !== "completed") throw new Error(result.error ?? "Observation failed. No automatic retry.");
      if (result.remoteFileCleanup !== "deleted") throw new Error("Remote cleanup did not succeed. Capture stopped; inspect the local observation artifacts.");
      if (enabled.current) {
        try {
          const state = await post<Adaptive>("observation", {jobId: accepted.id});
          if (!signal.aborted) {setAdaptive(state); enabled.current = state.enabled;}
        } catch (cause) {
          if (signal.aborted || session.current !== id) return;
          enabled.current = false; stopRemote(id);
          setAdaptive(previous => previous ? {...previous, enabled: false, status: "off"} : null);
          setError(cause instanceof Error ? cause.message : "Adaptive feedback rejected.");
        }
      }
      setStage("Observation complete · next capture in 5 seconds");
      await delay(5000, signal);
    }
  }
  async function start() {
    if (startingRef.current || stream.current) return;
    startingRef.current = true; setStarting(true); setError(null); setJob(null);
    const abort = new AbortController(); controller.current = abort;
    try {
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") throw new Error("Use a browser with webcam recording on localhost.");
      const service = await json<{keyConfigured: boolean; activeJobId: string | null}>("/api/crowd/status");
      if (!service.keyConfigured || service.activeJobId) throw new Error("Configure the observation service and wait for any current analysis to finish.");
      const media = await navigator.mediaDevices.getUserMedia({audio: false,
        video: {width: {ideal: 640}, height: {ideal: 480}, frameRate: {ideal: 15, max: 15}, ...(device ? {deviceId: {exact: device}} : {})}});
      if (abort.signal.aborted || document.hidden || !mounted.current) {media.getTracks().forEach(t => t.stop()); return;}
      stream.current = media;
      media.getVideoTracks().forEach(t => {t.onended = t.onmute = () => {stop(); setError("Camera interrupted. Restart it explicitly.");};});
      if (preview.current) {preview.current.srcObject = media; await preview.current.play();}
      setDevices((await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput"));
      const id = crypto.randomUUID(); session.current = id;
      setRunning(true); setStage("Camera ready");
      void captureLoop(media, abort.signal, id).catch(cause => {
        if (abort.signal.aborted) return;
        stop(); setError(cause instanceof Error ? cause.message : "Camera observation failed.");
      });
    } catch (cause) {
      stop(); if (mounted.current) setError(cause instanceof Error ? cause.message : "Camera permission was denied.");
    } finally {startingRef.current = false; if (mounted.current) setStarting(false);}
  }
  async function toggleAdaptive() {
    const id = session.current;
    if (!id) return;
    setChanging(true); setError(null);
    const turnOn = !enabled.current;
    try {
      const state = await post<Adaptive>("config", {enabled: turnOn, session: id});
      if (session.current !== id || !stream.current || (turnOn && !liveRef.current)) {stopRemote(id); return;}
      enabled.current = state.enabled; setAdaptive(state);
    } catch (cause) {setError(cause instanceof Error ? cause.message : "Could not change adaptive mode.");}
    finally {setChanging(false);}
  }
  const decision = adaptive?.decision;
  const applied = adaptive?.lastApplied;
  const title = (id: string) => tracks.find(t => t.id === id)?.title ?? id.slice(0, 8);
  return <section className="panel crowd-panel" aria-labelledby="webcam-title">
    <div className="panel__header"><div><span className="eyebrow">Meta vision → DJ planning</span><h2 id="webcam-title">Crowd adaptive · Webcam</h2></div>
      <span className="crowd-badge">{adaptive?.enabled ? "Adaptive DJ on" : "Observation mode"}</span></div>
    <p className="crowd-intro">Aim your computer camera at the dancers, including a solo performer. Spark observes short clips; enable adaptive DJ to shape future track order and transitions.</p>
    <div className="crowd-controls">
      <label>Camera<select value={device} disabled={running || starting} onChange={e => setDevice(e.target.value)}>
        <option value="">Default computer camera</option>{devices.map(d => <option key={d.deviceId} value={d.deviceId}>{d.label || "Camera"}</option>)}</select></label>
      <button className="button button--primary" disabled={starting && !running} onClick={() => running ? stop() : void start()}>{starting ? "Opening camera…" : running ? "Stop camera" : "Start webcam observation"}</button>
      <button className="button button--ghost" disabled={!running || changing || (!live && !adaptive?.enabled)} onClick={() => void toggleAdaptive()}>{changing ? "Updating…" : adaptive?.enabled ? "Disable adaptive DJ" : "Enable adaptive DJ"}</button>
    </div>
    <p className="crowd-note">Sends repeated 6-second silent clips to Meta using API credits. Remote deletion is attempted; local recordings and results are retained. Capture stops when this tab is hidden. Adaptive mode requires a live continuous set with Auto arm.</p>
    <div className="crowd-content">
      <div className="crowd-preview"><video ref={preview} muted playsInline aria-label="Live webcam preview" className="crowd-webcam-preview" />
        <p className="crowd-note" role="status">{stage}</p>
        <p className="crowd-note"><strong>Demo gestures:</strong> both arms overhead for 2+ seconds = build; forearms crossed in an X at the chest for 2+ seconds = ease. Conflicting or unclear cues keep the existing direction.</p>
      </div>
      <div className="crowd-results">
        <article className="crowd-region"><h3>1 · Observed</h3><p>{job?.observation?.summary_en ?? "Waiting for a webcam observation."}</p>
          {job?.cue && <p className="crowd-note">Gesture: {job.cue.value} · visibility: {job.observation?.quality.visibility}</p>}
          {job?.observation && <details><summary>Timestamped evidence and raw observation</summary><ul>{job.observation.evidence.map(e => <li key={e.id}>{e.start_seconds}–{e.end_seconds}s: {e.observation}</li>)}</ul>
            <video controls muted playsInline src={`/api/crowd/jobs/${job.id}/clip`} aria-label="Latest analyzed webcam clip" />
            <pre>{JSON.stringify(job, null, 2)}</pre></details>}
        </article>
        <article className="crowd-region"><h3>2 · Decision · {adaptive?.status ?? "off"}</h3><p>{adaptive?.reason ?? "Enable adaptive DJ after starting the live set. Webcam observation alone does not change playback."}</p>
          {decision && <><p>{decision.reason}</p><p className="crowd-note">{decision.model} · {decision.technique.replaceAll("_", " ")} · {decision.bars} bars · bass swap {decision.bassSwap ? "on" : "off"}</p>
            {decision.queueChanged && <p className="crowd-note">Before: {decision.before.slice(decision.lockedThrough + 1, decision.lockedThrough + 4).map(title).join(" → ")}</p>}
            <p className="crowd-note">{decision.queueChanged ? "Proposed next tracks" : "Next tracks retained"}: {decision.after.slice(decision.lockedThrough + 1, decision.lockedThrough + 4).map(title).join(" → ")}</p></>}
        </article>
        <article className="crowd-region"><h3>3 · Execution</h3><p>{applied ? `${applied.executionStatus?.replaceAll("_", " ") ?? "Queue committed; waiting for deck load"} · ${title(applied.sourceTrack)} → ${title(applied.targetTrack)}` : "No crowd-driven change applied."}</p>
          {applied && <details><summary>Decision record</summary><pre>{JSON.stringify(applied, null, 2)}</pre></details>}</article>
        <p className="crowd-note">Natural movement needs two consistent observations. This demo policy follows visible intensity; it does not infer enjoyment. Changes wait for the current transition to finish and must still be fresh. Human takeover always takes priority.</p>
      </div>
    </div>
    {error && <p className="crowd-warning" role="alert">{error}</p>}
  </section>;
}

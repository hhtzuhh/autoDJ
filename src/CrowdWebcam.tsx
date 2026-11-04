import {useEffect, useRef, useState} from "react";
import {WebcamObservationLog} from "./WebcamObservationLog";
import type {CameraJob, CaptureEntry} from "./webcamLog";
import type {WebcamFlowState} from "./types";
import {belongsToRun} from "./crowdChanges";
import type {SetRun} from "./crowdChanges";
import {MusicMemory} from "./MusicMemory";
import {CrowdDecisionLog} from "./CrowdDecisionLog";

type Adaptive = import("./crowdChanges").AdaptiveState;
async function json<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {...options, signal: options?.signal ?? AbortSignal.timeout(15_000)});
  const data = await response.json().catch(() => null);
  if (!response.ok || !data) throw new Error(data?.error ?? "Local service unavailable.");
  return data as T;
}
function post<T>(route: string, data: unknown) {
  return json<T>(`/api/adaptive/${route}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
}
function stopRemote(session: string | null, reason = "camera_stopped") {
  if (!session) return;
  // Lease expiry is the fallback if unload delivery or the bridge fails.
  navigator.sendBeacon("/api/adaptive/config", new Blob([JSON.stringify({enabled: false, session, preserveAccepted: true, reason})], {type: "application/json"}));
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

export function CrowdWebcam({live, tracks, setRun, onTelemetry}: {live: boolean; setRun?: SetRun | null; tracks: {id: string; title: string}[]; onTelemetry?: (value: WebcamFlowState) => void}) {
  const [running, setRunning] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stage, setStage] = useState("Camera is off");
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<CameraJob | null>(null);
  const [captures, setCaptures] = useState<CaptureEntry[]>([]);
  const [adaptive, setAdaptive] = useState<Adaptive | null>(null);
  const [scope, setScope] = useState(setRun?.id ?? '');
  const runRef = useRef(setRun?.id); runRef.current = setRun?.id;
  useEffect(() => {setScope(setRun?.id ?? '');}, [setRun?.id]);
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
    if (!live) return;
    let active = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const state = await json<Adaptive>("/api/adaptive/status", {signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)])});
        if (active) setAdaptive(state);
      } catch { /* Preserve the last known records on a temporary read failure. */ }
      finally { if (active) timer = setTimeout(() => void refresh(), 2000); }
    };
    void refresh();
    return () => {active = false; controller.abort(); clearTimeout(timer);};
  }, [live]);

  useEffect(() => {
    onTelemetry?.({running, starting, stage, error, job, adaptive: adaptive ? {...adaptive,
      decision: adaptive.decision && belongsToRun(adaptive.decision, setRun?.id ?? '') ? adaptive.decision : undefined,
      lastApplied: adaptive.lastApplied && belongsToRun(adaptive.lastApplied, setRun?.id ?? '') ? adaptive.lastApplied : undefined} : null});
  }, [running, starting, stage, error, job, adaptive, onTelemetry, setRun?.id]);

  function stop(reason = "camera_stopped") {
    const wasActive = Boolean(controller.current || stream.current || session.current);
    const ownedSession = enabled.current ? session.current : null;
    enabled.current = false;
    controller.current?.abort(); controller.current = null;
    stream.current?.getTracks().forEach(track => {track.onended = null; track.onmute = null; track.stop();}); stream.current = null;
    session.current = null;
    stopRemote(ownedSession, reason);
    if (mounted.current) {
      setRunning(false);
      if (wasActive) setStage("Camera stopped. Accepted requests continue; Take over cancels automation.");
      setAdaptive(previous => previous ? {...previous, observing: false, observationReason: "Camera stopped. Accepted requests continue."} : null);
    }
  }
  useEffect(() => {
    mounted.current = true;
    const hidden = () => {if (document.hidden) stop("page_hidden");};
    const ended = () => stop("page_closed");
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", ended);
    return () => {mounted.current = false; document.removeEventListener("visibilitychange", hidden); window.removeEventListener("pagehide", ended); stop("page_closed");};
  }, []);

  useEffect(() => {
    if (!live && enabled.current) {
      enabled.current = false; stopRemote(session.current, "connection_lost");
      setAdaptive(previous => previous ? {...previous, observing: false, observationReason: "Live DJ connection lost."} : null);
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
        if (mounted.current && enabled.current && session.current === heartbeatSession) {setAdaptive(state); enabled.current = state.observing ?? state.enabled;}
      } catch (cause) {
        if (session.current !== heartbeatSession) return;
        enabled.current = false; stopRemote(heartbeatSession, "connection_lost");
        if (mounted.current) {setError(cause instanceof Error ? cause.message : "Adaptive heartbeat failed."); setAdaptive(previous => previous ? {...previous, observing: false} : null);}
      } finally {busy = false;}
    };
    const timer = setInterval(() => void beat(), 5000);
    return () => clearInterval(timer);
  }, [running]);

  function updateCapture(id: string, patch: Partial<CaptureEntry>) {
    if (mounted.current) setCaptures(rows => rows.map(row => row.id === id ? {...row, ...patch} : row));
  }
  async function captureLoop(media: MediaStream, signal: AbortSignal, id: string) {
    while (!signal.aborted) {
      const capturedAt = new Date().toISOString();
      const captureId = crypto.randomUUID();
      const setRunId = runRef.current;
      let acceptedId: string | undefined;
      let uploading = false;
      setCaptures(rows => [{id: captureId, setRunId, sessionId: id, capturedAt, phase: "recording"}, ...rows]);
      try {
        setStage("Recording 6 seconds · silent webcam");
        const clip = await record(media, signal);
        if (signal.aborted) throw new Error("Capture stopped before submission.");
        uploading = true;
        updateCapture(captureId, {phase: "uploading"});
        setStage("Sending webcam clip to Spark…");
        const query = new URLSearchParams({mode: "webcam", session: id, capturedAt});
        if (setRunId) query.set("setRunId", setRunId);
        const accepted = await json<{id: string}>(`/api/crowd/analyze?${query}`, {
          method: "POST", headers: {"Content-Type": "application/octet-stream"}, body: clip, signal,
        });
        acceptedId = accepted.id;
        updateCapture(captureId, {phase: "submitted", jobId: accepted.id});
        let result: CameraJob;
        const deadline = Date.now() + 600_000;
        do {
          await delay(1200, signal);
          result = await json<CameraJob>(`/api/crowd/jobs/${accepted.id}`, {signal});
          updateCapture(captureId, {job: result});
          setStage(result.stage); setJob(result);
          if (Date.now() > deadline) throw new Error("Observation timed out. Stop and inspect the local service.");
        } while (result.status === "running");
        if (result.status !== "completed") throw new Error(result.error ?? "Observation failed. No automatic retry.");
        if (result.remoteFileCleanup !== "deleted") throw new Error("Remote cleanup did not succeed. Capture stopped; inspect the local observation artifacts.");
        if (enabled.current) {
          try {
            const state = await post<Adaptive>("observation", {jobId: accepted.id});
            if (!signal.aborted) {setAdaptive(state); enabled.current = state.observing ?? state.enabled;}
          } catch (cause) {
            if (signal.aborted || session.current !== id) return;
            enabled.current = false; stopRemote(id, "observation_error");
            setAdaptive(previous => previous ? {...previous, observing: false} : null);
            setError(cause instanceof Error ? cause.message : "Adaptive feedback rejected.");
          }
        }
      } catch (cause) {
        updateCapture(captureId, {phase: acceptedId ? "submitted" : signal.aborted ? "stopped" : "failed",
          note: acceptedId ? "Camera loop stopped. The log continues checking the submitted observation."
            : signal.aborted ? uploading ? "Upload interrupted; server acceptance is not yet confirmed." : "Stopped before upload. No Meta call was sent."
            : cause instanceof Error ? cause.message : "Capture failed."});
        if (signal.aborted) return;
        throw cause;
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
      media.getVideoTracks().forEach(t => {t.onended = t.onmute = () => {stop("camera_interrupted"); setError("Camera interrupted. Restart it explicitly.");};});
      if (preview.current) {preview.current.srcObject = media; await preview.current.play();}
      setDevices((await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "videoinput"));
      const id = crypto.randomUUID(); session.current = id;
      setRunning(true); setStage("Camera ready");
      void captureLoop(media, abort.signal, id).catch(cause => {
        if (abort.signal.aborted) return;
        stop("observation_error"); setError(cause instanceof Error ? cause.message : "Camera observation failed.");
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
      const state = await post<Adaptive>("config", {enabled: turnOn, session: id, ...(!turnOn ? {preserveAccepted: true, reason: "new_gestures_stopped"} : {})});
      if (session.current !== id || !stream.current || (turnOn && !liveRef.current)) {stopRemote(id, "observation_error"); return;}
      enabled.current = state.observing ?? state.enabled; setAdaptive(state);
    } catch (cause) {setError(cause instanceof Error ? cause.message : "Could not change adaptive mode.");}
    finally {setChanging(false);}
  }
  const runOptions = [...(adaptive?.setRuns ?? [])];
  if (setRun && !runOptions.some(run => run.id === setRun.id)) runOptions.push(setRun);
  const memoryRows = (adaptive?.memory?.history ?? adaptive?.memory?.recent ?? []).filter(row => belongsToRun(row, scope));
  const scopedAdaptive = adaptive ? {...adaptive,
    planningAttempts: adaptive.planningAttempts?.filter(row => belongsToRun(row, scope)),
    decisionHistory: adaptive.decisionHistory?.filter(row => belongsToRun(row, scope)),
    decision: adaptive.decision && belongsToRun(adaptive.decision, scope) ? adaptive.decision : undefined,
    lastApplied: adaptive.lastApplied && belongsToRun(adaptive.lastApplied, scope) ? adaptive.lastApplied : undefined} : null;
  return <section className="panel crowd-panel" aria-labelledby="webcam-title">
    <div className="panel__header"><div><span className="eyebrow">Meta vision → DJ planning</span><h2 id="webcam-title">Crowd adaptive · Webcam</h2></div>
      <span className="crowd-badge">{(adaptive?.observing ?? adaptive?.enabled) ? "New gestures enabled" : "New gestures off"}</span></div>
    <p className="crowd-intro">Spark observes visible audience movement. Enable adaptive DJ to save song-linked observations and use explicit gestures to request a new song order or future in-track actions.</p>
    <div className="crowd-controls">
      <label>Camera<select value={device} disabled={running || starting} onChange={e => setDevice(e.target.value)}>
        <option value="">Default computer camera</option>{devices.map(d => <option key={d.deviceId} value={d.deviceId}>{d.label || "Camera"}</option>)}</select></label>
      <button className="button button--primary" disabled={starting && !running} onClick={() => running ? stop() : void start()}>{starting ? "Opening camera…" : running ? "Stop camera" : "Start webcam observation"}</button>
      <button className="button button--ghost" disabled={!running || changing || (!live && !adaptive?.enabled)} onClick={() => void toggleAdaptive()}>{changing ? "Updating…" : (adaptive?.observing ?? adaptive?.enabled) ? "Stop new gestures" : "Enable adaptive DJ"}</button>
    </div>
    <p className="crowd-note">Sends repeated 6-second silent clips to Meta using API credits. Remote deletion is attempted; local recordings and results are retained. Capture stops when this tab is hidden. Accepted requests continue until applied or expired; Take over cancels automation. Adaptive mode requires a live continuous set with Auto arm.</p>
    <div className="crowd-content">
      <div className="crowd-preview"><video ref={preview} muted playsInline aria-label="Live webcam preview" className="crowd-webcam-preview" />
        <p className="crowd-note" role="status">{stage}</p>
        <p className="crowd-note"><strong>Demo gestures:</strong> hold one upright index finger (number 1) for 2+ seconds to request a different remaining song order. Hold both arms above your head for 2+ seconds to request an in-track effect; cross your forearms in an X for fewer effects. Lower your hands for an observed clip before repeating. Ordinary dancing or stillness is recorded only. Meta may keep actions unchanged if no suitable slot exists.</p>
      </div>
      <div className="crowd-results">
        <label className="set-run-selector">Set run · logs
          <select value={scope} onChange={e => setScope(e.target.value)}>
            {[...runOptions].reverse().map(run => <option key={run.id} value={run.id}>
              Set #{String(run.number).padStart(6, '0')} · {new Date(run.startedAt).toLocaleString('en-GB')}{run.id === setRun?.id ? ' · current' : ''}
            </option>)}
            <option value="">Unassigned · standalone / older logs</option>
            <option value="all">All runs</option>
          </select>
        </label>
        <WebcamObservationLog captures={captures} scope={scope} feedback={memoryRows} />
        <MusicMemory episodes={(adaptive?.memory?.episodes ?? []).filter(row => belongsToRun(row, scope))}
          events={memoryRows} tracks={tracks} warning={adaptive?.memory?.warning} />
        <CrowdDecisionLog adaptive={scopedAdaptive} tracks={tracks} />
        <p className="crowd-note">One request per held gesture. Reordering preserves the current and next prepared handoff, and needs two songs after that protected prefix. The new order applies when the protected next handoff completes. In-track effects wait for their scheduled song beat. Earlier sets remain archived and do not drive this demo. Human takeover always takes priority.</p>
      </div>
    </div>
    {error && <p className="crowd-warning" role="alert">{error}</p>}
  </section>;
}

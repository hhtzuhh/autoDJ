import {useEffect, useRef, useState} from "react";
import type {CopilotState} from "./types";
import type {BuildResult} from "./playlistRequest";

export function TransitionSetupDialog({open, onClose, state, disabled, onAction}: {
  open: boolean; onClose: () => void; state: CopilotState; disabled: boolean;
  onAction: (action: "prepare-transitions" | "activate" | "start") => Promise<BuildResult>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const b = state.dataSource === "live" ? state.setBuilder : undefined;
  const preparing = ["curating", "analyzing", "planning"].includes(b?.status ?? "");
  const locked = disabled || busy || preparing || !state.connected || !b;
  const playing = state.decks.some(d => d.playing);
  const exported = Boolean(b?.activationReady && b.activation?.status === "playlist_ready");
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    else if (!open && dialog.current?.open) dialog.current?.close();
  }, [open]);
  const run = async (action: "prepare-transitions" | "activate" | "start") => {
    if (locked) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      const result = await onAction(action);
      if (!result.ok) setError(result.error ?? "Action rejected");
      else setNotice(result.message ?? "Action accepted");
    } catch (error) {setError(String(error));}
    finally {setBusy(false);}
  };
  const title = (id: string) => b?.queue.find(t => t.id === id)?.title ?? id.slice(0, 8);
  return <dialog ref={dialog} className="playlist-dialog library-dialog" aria-labelledby="transition-setup-title" onCancel={onClose} onClose={onClose}>
    <div className="playlist-dialog__header"><div><span className="eyebrow">From selected songs to Mixxx</span><h2 id="transition-setup-title">Prepare & start your set</h2></div>
      <button type="button" className="playlist-dialog__close" onClick={onClose} aria-label="Close set preparation">×</button></div>
    <p className="flow-empty">{b?.queue.length ?? 0} selected songs · order is preserved. Prepare computes entry/exit candidates; Meta chooses the first executable plan during Start set, before audio starts.</p>
    <ol className="set-setup-steps">
      <li><strong>Prepare adjacent transitions</strong><p>Reuse local analysis and pair caches for each neighboring song. This does not call Meta or start playback.</p>
        <button className="button button--secondary" disabled={locked || (b?.queue.length ?? 0) < 2} onClick={() => void run("prepare-transitions")}>{preparing && b?.buildPhase === "transitions" ? "Preparing candidates…" : "Prepare transitions"}</button></li>
      <li><strong>Export the Mixxx playlist</strong><p>After all pairs are ready, export the selected order. In Mixxx, import/open the generated playlist and select its first song.</p>
        <button className="button button--secondary" disabled={locked || !b?.activationReady || playing} onClick={() => void run("activate")}>Export Mixxx playlist</button>
        {exported && <code className="set-setup-path">{b?.activation?.playlist}</code>}</li>
      <li><strong>Start set</strong><p>Stop both decks first. Start verifies the loaded songs and requires an initial Meta plan, then plays Deck 1. Future plans are prepared ahead of their cues.</p>
        <button className="button button--primary" disabled={locked || !exported || playing} onClick={() => void run("start")}>Start set</button>
        <p>{state.autoArmEnabled ? "Auto arm is on: valid plans can execute automatically. Take over remains available." : "Auto arm is off: approve & arm each transition in Control room, or enable Auto arm for automatic handoffs."}</p></li>
    </ol>
    {b?.transitions.map(edge => <div className="set-setup-edge" key={edge.id}><strong>{title(edge.sourceTrackId)} → {title(edge.targetTrackId)}</strong><span className={`flow-track-status flow-track-status--${edge.status === "analyzed" ? "ready" : edge.status === "blocked" ? "failed" : edge.status}`}>{edge.status} · {edge.candidateCount ?? 0} candidates</span>{edge.reason && <small>{edge.reason === "clean_break_required" ? "Clean break candidates · tempo blending unavailable" : edge.reason}</small>}</div>)}
    {!b?.transitions.length && <p className="flow-empty">Choose a playlist, then prepare its transitions.</p>}
    {(error || b?.error) && <p role="alert" className="playlist-dialog__error">{error ?? b?.error}</p>}
    {notice && <p role="status" className="playlist-dialog__notice">{notice}</p>}
    {disabled && <p className="flow-section__note">Controls unavailable during the active action or while disconnected.</p>}
    <div className="playlist-dialog__footer"><span>Candidate readiness is separate from an executable Meta plan.</span><button className="button button--ghost" onClick={onClose}>Back to pipeline</button></div>
  </dialog>;
}

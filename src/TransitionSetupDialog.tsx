import {useEffect, useRef, useState} from "react";
import type {CopilotState} from "./types";
import type {BuildResult} from "./playlistRequest";
import {PreparedPlaylistImport} from "./PreparedPlaylistImport";

export function TransitionSetupDialog({open, onClose, state, disabled, onAction}: {
  open: boolean; onClose: () => void; state: CopilotState; disabled: boolean;
  onAction: (action: "prepare-transitions" | "activate" | "build-performance" | "start") => Promise<BuildResult>;
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
  const performance = b?.performancePlan;
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    else if (!open && dialog.current?.open) dialog.current?.close();
  }, [open]);
  const run = async (action: "prepare-transitions" | "activate" | "build-performance" | "start") => {
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
    <p className="flow-empty">{b?.queue.length ?? 0} selected songs · order is preserved. Prepare finds candidates for all adjacent pairs. Build plans the first rolling window: up to 4 songs / 3 handoffs. Review it before starting playback; later windows are planned as the set progresses.</p>
    <details><summary>Available DJ actions and transition techniques</summary>
      <p>Playback actions are optional; every song change needs one handoff. Meta chooses timing, common BPM and EQ parameters in one planning call.</p>
      {b?.actionCatalog && Object.entries(b.actionCatalog).map(([name, a]) => <p key={name}><strong>{name.replaceAll("_", " ")}</strong> · {a.purpose} {a.timing} <small>{a.tradeoff}</small></p>)}
      <p>Meta chooses one technique for each transition in its planning window. Availability depends on the cue pair and tempo compatibility. These are control templates; musical quality still needs listening.</p>
      {b?.techniques?.map(t => <p key={t.id}><strong>{t.id.replaceAll("_", " ")}</strong> · {t.description} <small>{t.tradeoff}</small></p>)}
    </details>
    <ol className="set-setup-steps">
      <li><strong>Prepare adjacent transitions</strong><p>Reuse local analysis and pair caches for each neighboring song. This does not call Meta or start playback.</p>
        <button className="button button--secondary" disabled={locked || (b?.queue.length ?? 0) < 2} onClick={() => void run("prepare-transitions")}>{preparing && b?.buildPhase === "transitions" ? "Preparing candidates…" : "Prepare transitions"}</button></li>
      <li><strong>Export or import a prepared playlist</strong><p>For a new set, export after Step 1. If you already have a prepared playlist, import it below to restore the order and transitions, then go straight to Step 3. In Mixxx, open the playlist and select its first song.</p>
        <button className="button button--secondary" disabled={locked || !b?.activationReady || playing} onClick={() => void run("activate")}>Export Mixxx playlist</button>
        {exported && <code className="set-setup-path">{b?.activation?.playlist}</code>}
        <PreparedPlaylistImport disabled={disabled || preparing || !state.connected || !b || playing} busy={busy} onBusy={setBusy} />
      </li>
      <li><strong>Build performance plan</strong><p>First, manually import/open the exported M3U8 in Mixxx, keep its original playlist order, and click its first song in the track table. Web import does not select a playlist in Mixxx. With both decks stopped, Build loads and verifies those songs, then asks Meta to choose cues, actions and parameters for the first window. This does not start playback.</p>
        <button className="button button--secondary" disabled={locked || !exported || playing || performance?.status === "building"} onClick={() => void run("build-performance")}>{performance?.status === "building" ? "Building performance plan…" : "Build performance plan"}</button>
        {performance?.error && <p role="alert" className="playlist-dialog__error">{performance.error}</p>}
        {performance?.transitions?.length ? <div aria-label="Performance plan preview">
          <p><strong>{performance.status === "ready" ? "Ready for review" : "Previous plan · rebuild required"}</strong> · {performance.transitions.length} handoffs · {performance.model}</p>
          <p>{performance.reason}</p>
          {performance.transitions.map((edge, index) => <div key={index} className="set-setup-edge"><div>
            <strong>{title(edge.source_track_id)} → {title(edge.target_track_id)}</strong>
            <p>Exit beat {edge.source_exit_beat_index} → entry beat {edge.target_entry_beat_index} · {edge.transition_bars} bars · {edge.technique.replaceAll("_", " ")}{edge.routine?.common_bpm != null ? ` · common ${edge.routine.common_bpm} BPM` : ""}</p>
            <ul>{edge.routine?.actions.map((action, i) => <li key={i}><code>{action.function}</code> · {action.deck ? `${action.deck} · ` : ""}beat {action.start_beat}{action.duration_beats != null ? ` · ${action.duration_beats} beats` : ""}{action.target_bpm != null ? ` · ${action.target_bpm} BPM` : ""}{action.curve ? ` · ${action.curve}` : ""}{action.fade_out_seconds != null ? ` · fade ${action.fade_out_seconds}s` : ""}</li>)}</ul>
          </div></div>)}
          <small>Action offsets are relative to the handoff cue (beat 0); negative beats happen before it. This is a plan, not a completed performance.</small>
        </div> : null}
      </li>
      <li><strong>Start playback</strong><p>Play the reviewed window without another initial Meta call. Song order, loaded identities and cue positions must still match. Later windows are planned during playback.</p>
        <button className="button button--primary" disabled={locked || !exported || playing || performance?.status !== "ready"} onClick={() => void run("start")}>Start playback</button>
        <p>Start playback enables Auto arm automatically. Valid plans execute at their scheduled cues. Cancel and Take over can still suspend automation.</p></li>
    </ol>
    {b?.transitions.map(edge => <div className="set-setup-edge" key={edge.id}><strong>{title(edge.sourceTrackId)} → {title(edge.targetTrackId)}</strong><span className={`flow-track-status flow-track-status--${edge.status === "analyzed" ? "ready" : edge.status === "blocked" ? "failed" : edge.status}`}>{edge.status} · {edge.candidateCount ?? 0} candidates</span>{edge.reason && <small>{edge.reason === "clean_break_required" ? "Clean break candidates · tempo blending unavailable" : edge.reason}</small>}</div>)}
    {!b?.transitions.length && <p className="flow-empty">Choose a playlist, then prepare its transitions.</p>}
    {(error || b?.error) && <p role="alert" className="playlist-dialog__error">{error ?? b?.error}</p>}
    {notice && <p role="status" className="playlist-dialog__notice">{notice}</p>}
    {disabled && <p className="flow-section__note">Controls unavailable during the active action or while disconnected.</p>}
    <div className="playlist-dialog__footer"><span>Prepare finds cue options. Meta selects cues and a technique per transition.</span><button className="button button--ghost" onClick={onClose}>Back to pipeline</button></div>
  </dialog>;
}

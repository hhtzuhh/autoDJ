import {useEffect, useRef, useState} from "react";
import type {CopilotState} from "./types";
import type {BuildResult} from "./playlistRequest";

export function MusicFeaturesDialog({open, onClose, state, disabled, onAnalyze}: {
  open: boolean; onClose: () => void; state: CopilotState; disabled: boolean;
  onAnalyze: (trackIds: string[]) => Promise<BuildResult>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const all = useRef<HTMLInputElement>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const b = state.dataSource === "live" ? state.setBuilder : undefined;
  const tracks = b?.library ?? [];
  const eligible = tracks.filter(t => t.analysisStatus === "ready" && ["mp3", "wav"].includes(t.audioFormat ?? ""));
  const selected = eligible.filter(t => chosen.includes(t.id)).map(t => t.id);
  const locked = disabled || sending || !b || !state.connected || ["analyzing", "curating", "planning"].includes(b.status);
  const allSelected = eligible.length > 0 && selected.length === eligible.length;
  const ready = tracks.filter(t => t.musicFeatures?.status === "ready").length;
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    else if (!open && dialog.current?.open) dialog.current?.close();
  }, [open]);
  useEffect(() => {if (all.current) all.current.indeterminate = selected.length > 0 && !allSelected;}, [selected.length, allSelected]);
  const submit = async () => {
    if (locked || !selected.length) return;
    setSending(true); setError(null); setNotice(null);
    try {
      const result = await onAnalyze(selected);
      if (!result.ok) setError(result.error ?? "Request rejected");
      else setNotice(`Accepted ${selected.length} songs. Ready descriptions are reused; other songs are sent to Meta one at a time.`);
    } catch (error) {setError(String(error));}
    finally {setSending(false);}
  };
  return <dialog ref={dialog} className="playlist-dialog library-dialog" aria-labelledby="music-dialog-title" onCancel={onClose} onClose={onClose}>
    <div className="playlist-dialog__header"><div><span className="eyebrow">03 · Audio understanding</span><h2 id="music-dialog-title">Muse Spark music features</h2></div>
      <button type="button" className="playlist-dialog__close" aria-label="Close music features" onClick={onClose}>×</button></div>
    <p className="playlist-dialog__library">Muse Spark 1.2 · {ready}/{tracks.length} descriptions ready · {eligible.length} eligible songs</p>
    <p className="flow-empty">Genre, groove, bass, instruments, vocals, harmony, arrangement and mood. Descriptions are model interpretations; BPM, beat timing and RMS remain local measurements.</p>
    <div className="library-analysis-controls"><button type="button" className="button button--primary" disabled={locked || !selected.length} onClick={() => void submit()}>{sending ? "Starting…" : b?.buildPhase === "music_features" && b.status === "analyzing" ? "Spark working…" : `Call Meta Spark (${selected.length})`}</button></div>
    <p className="flow-section__note">Sends the entire original MP3/WAV and the experiment prompt to Meta. Uses API credits. Complete All-In-One + DSP for each selected song first. Existing matching descriptions are reused; Build automatically fills any remaining descriptions before selecting songs.</p>
    <label className="library-select-all"><input ref={all} type="checkbox" checked={allSelected} disabled={locked || !eligible.length} onChange={e => setChosen(e.target.checked ? eligible.map(t => t.id) : [])} /> Select all eligible songs <span>{selected.length} selected</span></label>
    {error && <p role="alert" className="playlist-dialog__error">{error}</p>}
    {notice && <p role="status" className="playlist-dialog__notice">{notice}</p>}
    {disabled && <p className="flow-section__note">Controls unavailable during an active action or while disconnected. Showing last known results.</p>}
    {!tracks.length && <p className="flow-empty">No library songs available.</p>}
    <div className="music-results">{tracks.map((track, index) => {
      const result = track.musicFeatures;
      const canSelect = eligible.some(t => t.id === track.id);
      const status = result?.status ?? "pending";
      return <article key={track.id} className="music-result">
        <div className="music-result__heading"><label className="library-song-choice"><input type="checkbox" aria-label={`Describe ${track.title}`} checked={selected.includes(track.id)} disabled={locked || !canSelect} onChange={e => setChosen(ids => e.target.checked ? [...ids, track.id] : ids.filter(id => id !== track.id))} /><strong>{String(index + 1).padStart(2, "0")} · {track.title}</strong></label>
          <span className={`flow-track-status flow-track-status--${status === "running" ? "analyzing" : status}`}>{status === "pending" ? "Not analyzed" : status}</span></div>
        <p className="flow-section__note">{track.artist}{!canSelect ? track.analysisStatus !== "ready" ? " · Finish local analysis first" : " · MP3/WAV required" : ""}{result?.origin === "cache" ? " · Cached description" : ""}{result?.elapsedSeconds != null ? ` · ${result.elapsedSeconds.toFixed(1)}s` : ""}{result?.usage?.total_tokens != null ? ` · ${result.usage.total_tokens} tokens` : ""}</p>
        {result?.error && <p role="alert" className="playlist-dialog__error">{result.error}</p>}
        {result?.content && <details><summary>View model response · unverified</summary><pre>{result.content}</pre></details>}
        {result?.rawAvailable && <a href={`http://127.0.0.1:8765/api/set-builder/music-features/${track.id}`} target="_blank" rel="noreferrer">Open raw API JSON ↗</a>}
      </article>;
    })}</div>
    <div className="playlist-dialog__footer"><span>Build requires descriptions for every library song before selection.</span><button type="button" className="button button--ghost" onClick={onClose}>Back to pipeline</button></div>
  </dialog>;
}

import {useEffect, useRef, useState} from "react";
import {analysisTrackRows} from "./flowProjection";
import type {CopilotState} from "./types";
import type {BuildResult} from "./playlistRequest";

export function LibraryDialog({open, mode, onClose, state, disabled, disconnected, now, onAnalyze}: {
  open: boolean; mode: "library" | "analysis"; onClose: () => void;
  state: CopilotState; disabled: boolean; disconnected: boolean; now: number;
  onAnalyze: (trackIds: string[]) => Promise<BuildResult>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const selectAll = useRef<HTMLInputElement>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const b = state.dataSource === "live" ? state.setBuilder : undefined;
  const rows = analysisTrackRows(b?.library ?? []);
  const selectedIds = rows.filter(r => chosen.includes(r.track.id)).map(r => r.track.id);
  const allSelected = rows.length > 0 && selectedIds.length === rows.length;
  const ready = rows.filter(r => r.track.analysisStatus === "ready").length;
  const running = rows.filter(r => r.track.analysisStatus === "analyzing").length;
  const failed = rows.filter(r => r.track.analysisStatus === "failed").length;
  const preparing = ["analyzing", "curating", "planning"].includes(b?.status ?? "");
  const locked = disabled || sending || preparing || !b || !state.connected;
  const analysis = mode === "analysis";
  useEffect(() => {
    if (selectAll.current) selectAll.current.indeterminate = selectedIds.length > 0 && !allSelected;
  }, [selectedIds.length, allSelected, analysis]);
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    else if (!open && dialog.current?.open) dialog.current?.close();
  }, [open]);
  const analyze = async () => {
    if (locked || !selectedIds.length) return;
    setSending(true); setError(null); setNotice(null);
    try {
      const result = await onAnalyze(selectedIds);
      if (!result.ok) setError(result.error ?? "Analysis was not accepted");
      else setNotice(`Analysis accepted for ${selectedIds.length} selected songs. Ready results are reused; missing or failed analyses run locally. Meta selection waits for your playlist request.`);
    } catch (error) {setError(error instanceof Error ? error.message : String(error));}
    finally {setSending(false);}
  };
  return <dialog ref={dialog} className="playlist-dialog library-dialog" aria-labelledby="library-dialog-title" onCancel={onClose} onClose={onClose}>
    <div className="playlist-dialog__header"><div><span className="eyebrow">{analysis ? "02 · All-In-One + local DSP" : "01 · Local files"}</span>
      <h2 id="library-dialog-title">{analysis ? "Analyze library" : "All library songs"}</h2></div>
      <button type="button" className="playlist-dialog__close" onClick={onClose} aria-label="Close library dialog">×</button></div>
    <p className="playlist-dialog__library">{rows.length} songs · <code>local-assets/samples/</code></p>
    {analysis && <>
      <div className="library-analysis-summary" aria-live="polite">
        <span><b>{ready}</b> Ready</span><span><b>{rows.length - ready - running - failed}</b> Not ready</span>
        <span><b>{running}</b> Analyzing</span><span><b>{failed}</b> Failed</span>
      </div>
      <div className="library-analysis-controls">
        <button type="button" className="button button--primary" disabled={locked || !selectedIds.length} onClick={() => void analyze()}>{sending ? "Starting…" : preparing ? "Working…" : `Analyze selected (${selectedIds.length})`}</button>
        <label className="library-modal-option"><input type="checkbox" disabled checked={false} readOnly /> Modal accelerate <small>Coming soon · disabled</small></label>
      </div>
      <p className="flow-section__note">Check the songs to analyze. Only selected songs run, one local job at a time; ready results are reused. Build later completes local analysis and Muse Spark descriptions for the entire library before selecting songs.</p>
      <label className="library-select-all"><input ref={selectAll} type="checkbox" checked={allSelected} disabled={locked || !rows.length} onChange={event => setChosen(event.target.checked ? rows.map(r => r.track.id) : [])} /> Select all songs <span>{selectedIds.length} / {rows.length} selected</span></label>
      {error && <p className="playlist-dialog__error" role="alert">{error}</p>}
      {b?.error && <p className="playlist-dialog__error" role="alert">{b.error}</p>}
      {notice && <p className="playlist-dialog__notice" role="status">{notice}</p>}
    </>}
    {disconnected && <p className="playlist-dialog__error" role="status">Disconnected · showing last known library data.</p>}
    {!rows.length ? <p className="flow-empty">No library songs available. Add audio files to local-assets/samples/, then restart the bridge to scan the folder.</p>
      : <div className="flow-track-table-scroll"><table className="flow-track-table">
        <thead><tr><th scope="col"># / Song</th>{analysis ? <><th scope="col">Analysis source</th><th scope="col">Status</th><th scope="col">Execution</th></> : <><th scope="col">Genre · file tag</th><th scope="col">Duration</th></>}</tr></thead>
        <tbody>{rows.map(({track, index, run, source, note}) => <tr key={track.id}>
          <td>{analysis ? <label className="library-song-choice"><input type="checkbox" aria-label={`Analyze ${track.title}`} checked={selectedIds.includes(track.id)} disabled={locked} onChange={event => setChosen(ids => event.target.checked ? [...ids, track.id] : ids.filter(id => id !== track.id))} /><strong>{String(index + 1).padStart(2, "0")} · {track.title}</strong></label> : <strong>{String(index + 1).padStart(2, "0")} · {track.title}</strong>}<small>{track.artist || "Unknown artist"}{b?.queue.some(t => t.id === track.id) ? " · In playlist" : ""}</small></td>
          {analysis ? <>
            <td><strong>{source}</strong><small>{note}</small></td>
            <td><span className={`flow-track-status flow-track-status--${track.analysisStatus}`}>{({ready: "Ready", pending: "Not ready", analyzing: "Analyzing", failed: "Failed"})[track.analysisStatus]}</span>{track.bpm > 0 && <small>{track.bpm.toFixed(1)} BPM</small>}</td>
            <td>{run ? <><strong>{run.runner === "local_cpu" ? "Local CPU" : run.runner}</strong><small>{run.durationSeconds != null ? `${run.durationSeconds.toFixed(1)}s` : run.status === "running" && !disconnected ? `${Math.max(0, (now - Date.parse(run.startedAt)) / 1000).toFixed(0)}s elapsed` : run.status}</small><code>{run.jobId}</code></> : <small>{track.analysisOrigin === "cache" ? "No new job" : "No recorded job"}</small>}</td>
          </> : <><td>{track.genre || "Unknown"}</td><td>{track.durationSeconds > 0 ? `${Math.floor(track.durationSeconds / 60)}:${String(Math.floor(track.durationSeconds % 60)).padStart(2, "0")}` : "Unknown"}</td></>}
        </tr>)}</tbody>
      </table></div>}
    <div className="playlist-dialog__footer"><span>{analysis ? "Analysis only · local CPU" : "All songs in the scanned library"}</span><button type="button" className="button button--ghost" onClick={onClose}>Back to pipeline</button></div>
  </dialog>;
}

import {useEffect, useRef, useState} from "react";
import type {CopilotState} from "./types";
import {requestedTrackCount} from "./playlistRequest";
import type {BuildResult} from "./playlistRequest";

type SavedSet = {id: string; savedAt: string; prompt: string; trackCount: number;
  tracks: {id: string; title: string}[]; available: boolean; reason: string | null};

export function PlaylistDialog({open, onClose, state, disabled, onBuild}: {
  open: boolean; onClose: () => void; state: CopilotState; disabled: boolean;
  onBuild: (prompt: string, count: number) => Promise<BuildResult>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [mode, setMode] = useState<"new" | "saved">("new");
  const [prompt, setPrompt] = useState("");
  const [count, setCount] = useState(3);
  const [sent, setSent] = useState<{prompt: string; count: number; buildId?: string} | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saved, setSaved] = useState<SavedSet[]>([]);
  const [loading, setLoading] = useState(false);
  const [savedError, setSavedError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [chosen, setChosen] = useState("");
  const b = state.dataSource === "live" ? state.setBuilder : undefined;
  const preparing = ["curating", "analyzing", "planning"].includes(b?.status ?? "");
  const locked = disabled || busy || preparing || state.dataSource !== "live" || !state.connected;
  const max = Math.min(50, b?.catalogCount ?? 0);
  const validCount = Number.isInteger(count) && count >= 2 && count <= max;
  const match = sent && b?.prompt === sent.prompt && (!sent.buildId || b.buildId === sent.buildId);
  const selected = saved.find(item => item.id === chosen);

  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    else if (!open && dialog.current?.open) dialog.current?.close();
  }, [open]);
  useEffect(() => {
    if (!open || mode !== "saved" || state.dataSource !== "live") return;
    const controller = new AbortController();
    setLoading(true); setSavedError(null);
    void fetch("http://127.0.0.1:8765/api/set-builder/saved", {signal: controller.signal})
      .then(async response => {const payload = await response.json(); if (!response.ok) throw new Error(payload.error ?? "Could not load saved playlists"); return payload.playlists as SavedSet[];})
      .then(items => {if (!controller.signal.aborted) setSaved(items);})
      .catch(error => {if (!controller.signal.aborted) setSavedError(String(error.message ?? error));})
      .finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => controller.abort();
  }, [open, mode, reload, state.dataSource]);

  const submit = async () => {
    if (locked || !prompt.trim() || !validCount) return;
    const request = {prompt: prompt.trim(), count};
    setSent(request); setBusy(true); setError(null); setNotice(null);
    try {
      const result = await onBuild(request.prompt, request.count);
      if (!result.ok) setError(result.error ?? "Build was not accepted");
      else setSent({...request, buildId: result.buildId});
    } catch (error) {setError(String(error));}
    finally {setBusy(false);}
  };
  const load = async () => {
    if (locked || !selected?.available) return;
    setBusy(true); setError(null); setNotice(null);
    try {
      const response = await fetch("http://127.0.0.1:8765/api/set-builder/load", {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: selected.id}),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error ?? "Could not load playlist");
      setSent(null);
      setNotice(`Loaded ${selected.trackCount} tracks into preparation. Checking adjacent transitions using existing analyses. Export and Start remain separate actions.`);
    } catch (error) {setError(error instanceof Error ? error.message : String(error));}
    finally {setBusy(false);}
  };

  return <dialog ref={dialog} className="playlist-dialog" aria-labelledby="playlist-dialog-title" onCancel={onClose} onClose={onClose}>
    <div className="playlist-dialog__header"><div><span className="eyebrow">Meta playlist assistant</span><h2 id="playlist-dialog-title">Your next set</h2></div>
      <button type="button" className="playlist-dialog__close" onClick={onClose} aria-label="Close playlist dialog">×</button></div>
    <p className="playlist-dialog__library">{b?.catalogCount ?? 0} local tracks · <code>local-assets/samples/</code></p>
    <div className="playlist-dialog__modes" role="group" aria-label="Playlist source">
      <button type="button" aria-pressed={mode === "new"} onClick={() => setMode("new")}>Build a new playlist</button>
      <button type="button" aria-pressed={mode === "saved"} onClick={() => setMode("saved")}>Use a saved playlist</button>
    </div>
    {mode === "new" ? <>
      <div className="playlist-chat" role="log" aria-label="Playlist conversation" aria-live="polite">
        <div className="playlist-message"><strong>Playlist assistant</strong><p>Tell me the style, number of tracks and energy arc you want. I’ll ask Meta to select from your local library, then prepare the audio analysis and transitions.</p></div>
        {sent && <div className="playlist-message playlist-message--user"><strong>You · {sent.count} tracks</strong><p>{sent.prompt}</p></div>}
        {sent && <div className="playlist-message"><strong>{match && b?.model ? b.model : "Preparation status"}</strong>
          <p>{error ? error : busy || !match || b?.status === "curating" ? "Requesting a playlist from Meta…"
            : b?.error ? b.error : b?.status === "ready" ? `Your ${b.queue.length}-track playlist is prepared. Export it to Mixxx when you’re ready.`
            : b?.status === "planning" ? "Refining the order using measured audio features…"
            : `${b?.queue.length ?? 0} tracks selected. ${b?.queue.filter(t => t.analysisStatus === "ready").length ?? 0} analyses ready; preparing adjacent transitions.`}</p>
          {!busy && !error && match && b?.queue.length ? <ol>{b.queue.map(track => <li key={track.id}>{track.title}<span>{track.analysisStatus}</span></li>)}</ol> : null}
          {!busy && !error && match && b?.reason && <p className="playlist-message__reason">{b.reason}</p>}
        </div>}
      </div>
      <form className="playlist-composer" onSubmit={event => {event.preventDefault(); void submit();}}>
        <label htmlFor="playlist-prompt">Describe your playlist</label>
        <textarea id="playlist-prompt" value={prompt} disabled={locked} rows={3}
          placeholder="幫我排 3 首 house，從輕鬆 groove 慢慢增加能量…"
          onChange={event => {setPrompt(event.target.value); const parsed = requestedTrackCount(event.target.value); if (parsed != null) setCount(parsed);}} />
        <div className="playlist-composer__actions"><label>Tracks<input type="number" min={2} max={Math.max(2, max)} value={count} disabled={locked} onChange={event => setCount(Number(event.target.value))} /></label>
          <button type="submit" className="button button--primary" disabled={locked || !validCount || !prompt.trim()}>{busy || preparing ? "Preparing…" : `Send · build ${count} tracks`}</button></div>
        <small>{max < 2 ? "Add at least two songs to local-assets/samples/ and restart the bridge." : !validCount ? `Choose 2–${max} tracks.` : "The Tracks field is the count sent to Meta. Sending uses API credits and starts preparation."}</small>
      </form>
    </> : <div className="playlist-saved">
      <div className="playlist-saved__heading"><p>Completed playlists saved in this project.</p><button type="button" className="button button--ghost" disabled={loading} onClick={() => setReload(v => v + 1)}>Refresh</button></div>
      {loading ? <p role="status">Loading playlists…</p> : savedError ? <p role="alert">{savedError}</p> : !saved.length ? <p className="playlist-saved__empty">No saved playlists yet. Complete a new build to save one here. Old workspace playlists are not imported.</p>
        : <div role="radiogroup" aria-label="Saved playlists">{saved.map(item => <label className={`playlist-saved__item ${chosen === item.id ? "is-selected" : ""}`} key={item.id}>
          <input type="radio" name="saved-playlist" value={item.id} checked={chosen === item.id} disabled={!item.available || locked} onChange={() => setChosen(item.id)} />
          <div><strong>{item.prompt}</strong><small>{item.trackCount} tracks · {new Date(item.savedAt).toLocaleString()}</small><p>{item.tracks.map(t => t.title).join(" → ")}</p>{item.reason && <small>{item.reason}</small>}</div>
        </label>)}</div>}
      <button type="button" className="button button--primary" disabled={locked || loading || !selected?.available} onClick={() => void load()}>Use selected playlist</button>
    </div>}
    {error && <p className="playlist-dialog__error" role="alert">{error}</p>}
    {notice && <p className="playlist-dialog__notice" role="status">{notice}</p>}
    {disabled && <p className="playlist-dialog__library">Preparation controls are temporarily unavailable. Wait for the active action or reconnect to the live bridge.</p>}
    <div className="playlist-dialog__footer"><span>Build and load prepare a set. Playback starts separately.</span><button type="button" className="button button--ghost" onClick={onClose}>Back to pipeline</button></div>
  </dialog>;
}

import {useEffect, useRef, useState} from "react";

type Preview = {filename: string; entryCount: number; canImport: boolean; missingAnalysis: number;
  tracks: {id: string; title: string; analysisStatus: string}[];
  issues: {line: number; entry: string; reason: string}[]};

export function PreparedPlaylistImport({disabled, busy, onBusy, mountedCount, onImported}: {
  disabled: boolean; busy: boolean; onBusy: (busy: boolean) => void;
  mountedCount: number; onImported: () => void;
}) {
  const [input, setInput] = useState<{filename: string; content: string} | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {alive.current = false; onBusy(false);};
  }, [onBusy]);
  const locked = disabled || busy;
  const request = async (action: string, body: {filename: string; content: string}) => {
    const response = await fetch(`http://127.0.0.1:8765/api/set-builder/${action}`, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error ?? "Could not load prepared playlist");
    return result;
  };
  const choose = async (file: File | undefined) => {
    if (!file || locked) return;
    setInput(null); setPreview(null); setError(null); onBusy(true); setWorking(true);
    try {
      if (file.size > 32 * 1024) throw new Error("Choose a UTF-8 M3U/M3U8 file up to 32 KB.");
      const data = {filename: file.name, content: new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer())};
      const result = await request("preview-playlist", data);
      if (!alive.current) return;
      setInput(data); setPreview(result);
    } catch (e) {if (alive.current) setError(e instanceof Error ? e.message : String(e));}
    finally {if (alive.current) {onBusy(false); setWorking(false);}}
  };
  const restore = async () => {
    if (locked || !input || !preview?.canImport || preview.missingAnalysis) return;
    setError(null); onBusy(true); setWorking(true);
    try {
      await request("import-prepared-playlist", input);
      if (!alive.current) return;
      setInput(null); setPreview(null); onImported();
    } catch (e) {if (alive.current) setError(e instanceof Error ? e.message : String(e));}
    finally {if (alive.current) {onBusy(false); setWorking(false);}}
  };
  return <div className="playlist-saved">
    <p>Import replaces the current selection and mounts this file’s song order in the app. Transition candidates are restored or rebuilt locally.</p>
    <label>Choose prepared playlist<input type="file" accept=".m3u,.m3u8" disabled={locked} onChange={event => {const file = event.target.files?.[0]; event.target.value = ""; void choose(file);}} /></label>
    {working && <p role="status">Checking playlist and transition preparation…</p>}
    {preview && <>
      <p><strong>Preview only · not mounted</strong><br />{preview.filename} · {preview.tracks.length}/{preview.entryCount} songs matched</p>
      <ol>{preview.tracks.map(t => <li key={t.id}>{t.title} · {t.analysisStatus === "ready" ? "Analysis ready" : "Needs analysis"}</li>)}</ol>
      {preview.issues.length > 0 && <ul role="alert">{preview.issues.map(i => <li key={i.line}>Line {i.line}: {i.entry} — {i.reason}</li>)}</ul>}
      {preview.missingAnalysis > 0 && <p role="alert">{preview.missingAnalysis} songs need local analysis before this prepared set can be restored. Analyze them first, then choose the file again.</p>}
      {mountedCount > 0 && <p>This will replace the mounted {mountedCount}-song playlist.</p>}
      <button className="button button--secondary" disabled={locked || !preview.canImport || preview.missingAnalysis > 0} onClick={() => void restore()}>Import & mount this playlist</button>
    </>}
    {error && <p role="alert" className="playlist-dialog__error">{error}</p>}
    <details className="panel-details"><summary>File requirements</summary><p>Songs must match local-assets/samples-v3/. Relative paths start there. The M3U stores song order; transition candidates come from local analyses and caches.</p></details>
  </div>;
}

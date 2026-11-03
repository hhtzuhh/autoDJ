import {useState} from "react";

type Preview = {filename: string; entryCount: number; canImport: boolean; missingAnalysis: number;
  tracks: {id: string; title: string; analysisStatus: string}[];
  issues: {line: number; entry: string; reason: string}[]};

export function PreparedPlaylistImport({disabled, busy, onBusy}: {
  disabled: boolean; busy: boolean; onBusy: (busy: boolean) => void;
}) {
  const [input, setInput] = useState<{filename: string; content: string} | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [working, setWorking] = useState(false);
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
    setInput(null); setPreview(null); setError(null); setNotice(null); onBusy(true); setWorking(true);
    try {
      if (file.size > 32 * 1024) throw new Error("Choose a UTF-8 M3U/M3U8 file up to 32 KB.");
      const data = {filename: file.name, content: new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer())};
      const result = await request("preview-playlist", data);
      setInput(data); setPreview(result);
    } catch (e) {setError(e instanceof Error ? e.message : String(e));}
    finally {onBusy(false); setWorking(false);}
  };
  const restore = async () => {
    if (locked || !input || !preview?.canImport || preview.missingAnalysis) return;
    setError(null); setNotice(null); onBusy(true); setWorking(true);
    try {
      const result = await request("import-prepared-playlist", input);
      setNotice(result.message);
    } catch (e) {setError(e instanceof Error ? e.message : String(e));}
    finally {onBusy(false); setWorking(false);}
  };
  return <div className="playlist-saved">
    <p><strong>Or import a previously prepared playlist</strong></p>
    <p>Restore its song order and adjacent transition candidates from this project's local analyses and caches. Valid caches are reused; missing or outdated pairs are recomputed locally. Then continue directly to Step 3. No Meta call or playback here.</p>
    <label>Choose prepared playlist<input type="file" accept=".m3u,.m3u8" disabled={locked} onChange={event => {const file = event.target.files?.[0]; event.target.value = ""; void choose(file);}} /></label>
    {working && <p role="status">Checking playlist and transition preparation…</p>}
    {preview && <>
      <p>{preview.filename} · {preview.tracks.length}/{preview.entryCount} songs matched</p>
      <ol>{preview.tracks.map(t => <li key={t.id}>{t.title} · {t.analysisStatus === "ready" ? "Analysis ready" : "Needs analysis"}</li>)}</ol>
      {preview.issues.length > 0 && <ul role="alert">{preview.issues.map(i => <li key={i.line}>Line {i.line}: {i.entry} — {i.reason}</li>)}</ul>}
      {preview.missingAnalysis > 0 && <p role="alert">{preview.missingAnalysis} songs need local analysis before this prepared set can be restored. Analyze them first, then choose the file again.</p>}
      <button className="button button--secondary" disabled={locked || !preview.canImport || preview.missingAnalysis > 0} onClick={() => void restore()}>Import playlist & restore transitions</button>
    </>}
    {error && <p role="alert" className="playlist-dialog__error">{error}</p>}
    {notice && <p role="status" className="playlist-dialog__notice">{notice}</p>}
    <small>Songs must match local-assets/samples/. Relative paths start there. The M3U contains song order; transition data is loaded from the local cache.</small>
  </div>;
}

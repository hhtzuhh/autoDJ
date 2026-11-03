import {useEffect, useRef, useState} from "react";
import type {CopilotState} from "./types";
import type {BuildResult} from "./playlistRequest";
import {PreparedPlaylistImport} from "./PreparedPlaylistImport";
import {InTrackActions, inTrackLabels} from "./InTrackActions";
import type {InTrackAction} from "./types";
import {preparedPlaylistView} from "./preparedPlaylist";

export function TransitionSetupDialog({open, onClose, state, disabled, onAction}: {
  open: boolean; onClose: () => void; state: CopilotState; disabled: boolean;
  onAction: (action: "prepare-transitions" | "activate" | "build-performance" | "start") => Promise<BuildResult>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const b = state.dataSource === "live" ? state.setBuilder : undefined;
  const mountedPlaylist = preparedPlaylistView(b);
  const [playlistMode, setPlaylistMode] = useState<"selection" | "import">("selection");
  const context = useRef(mountedPlaylist.contextKey);
  context.current = mountedPlaylist.contextKey;
  useEffect(() => {
    setPlaylistMode("selection"); setNotice(null); setError(null);
  }, [mountedPlaylist.contextKey]);
  const preparing = ["curating", "analyzing", "planning"].includes(b?.status ?? "");
  const locked = disabled || busy || preparing || !state.connected || !b;
  const playing = state.decks.some(d => d.playing);
  const exported = mountedPlaylist.mounted;
  const performance = b?.performancePlan;
  useEffect(() => {
    if (open && !dialog.current?.open) dialog.current?.showModal();
    else if (!open && dialog.current?.open) dialog.current?.close();
  }, [open]);
  const run = async (action: "prepare-transitions" | "activate" | "build-performance" | "start") => {
    if (locked) return;
    const startedContext = context.current;
    setBusy(true); setError(null); setNotice(null);
    try {
      const result = await onAction(action);
      if (context.current !== startedContext) return;
      if (!result.ok) setError(result.error ?? "Action rejected");
      else if (action !== "activate") setNotice(result.message ?? "Action accepted");
    } catch (error) {if (context.current === startedContext) setError(String(error));}
    finally {setBusy(false);}
  };
  const title = (id: string) => b?.queue.find(t => t.id === id)?.title ?? id.slice(0, 8);
  return <dialog ref={dialog} className="playlist-dialog library-dialog" aria-labelledby="transition-setup-title" onCancel={onClose} onClose={onClose}>
    <div className="playlist-dialog__header"><div><span className="eyebrow">From selected songs to Mixxx</span><h2 id="transition-setup-title">Prepare & start your set</h2></div>
      <button type="button" className="playlist-dialog__close" onClick={onClose} aria-label="Close set preparation">×</button></div>
    <p className="flow-empty">{b?.queue.length ?? 0} selected songs · order is preserved. Prepare finds candidates for all adjacent pairs. Build plans the first rolling window: up to 4 songs / 3 handoffs. Review it before starting playback; later windows are planned as the set progresses.</p>
    <details className="transition-catalog">
      <summary>Available transition techniques{b?.techniques?.length ? ` (${b.techniques.length})` : ""}</summary>
      <p>Meta chooses a technique and its action parameters for each song change in one planning call. Available choices depend on the cue pair and tempo compatibility.</p>
      <ul className="transition-catalog__techniques">
        {b?.techniques?.map(t => <li key={t.id}><strong>{t.id.replaceAll("_", " ")}</strong><p>{
          t.id === "bass_swap" ? "Crossfade while exchanging the two songs’ low EQ over the selected interval."
            : t.id === "bass_dip" ? "Crossfade with a bass-light moment, then restore the incoming low end."
            : t.description
        }</p></li>)}
      </ul>
      {!b?.techniques?.length && <p className="flow-empty">Technique catalog unavailable. Connect to the live service to load it.</p>}
      <p className="flow-section__note">Transition actions handle the song change, including tempo preparation and recovery. Musical quality still needs listening.</p>
      <details className="panel-details"><summary>Technical details · action functions{b?.actionCatalog ? ` (${Object.keys(b.actionCatalog).length})` : ""}</summary>
        <p>These functions implement the techniques above. Meta returns one action list per transition; EQ actions are optional and tempo ramps are required when the selected common BPM changes a song’s speed.</p>
        <ul className="transition-catalog__mapping">
          <li>Crossfade / late cut → <code>crossfade</code> with a linear / late-cut curve.</li>
          <li>Bass swap / bass dip → <code>crossfade</code> plus <code>bass_swap</code> / <code>bass_dip</code>.</li>
          <li>Clean cut / fade out cut → <code>cut_to_next</code> with a 0 / 0.5-second fade.</li>
          <li>Tempo adjustment → <code>ramp_tempo</code> before or after a beat-synced handoff.</li>
        </ul>
        {b?.actionCatalog && Object.entries(b.actionCatalog).map(([name, a]) => <p key={name}><strong><code>{name}</code></strong> · {a.purpose} {a.timing} <small>{a.tradeoff}</small></p>)}
      </details>
    </details>
    <details className="transition-catalog">
      <summary>Optional in-track actions{b?.inTrackCatalog ? ` (${Object.keys(b.inTrackCatalog).length})` : ""}</summary>
      <p>Shape one song while it plays alone. Meta can choose none, or up to two actions per outgoing song in the same planning call. These finish before transition preparation; the final song becomes eligible when its outgoing transition is planned.</p>
      {b?.inTrackCatalog ? <ul className="transition-catalog__techniques">
        {Object.entries(b.inTrackCatalog).map(([name, a]) => <li key={name}><strong>{inTrackLabels[name as InTrackAction["function"]] ?? name}</strong><p>{a.purpose}</p><p>{a.tradeoff}</p></li>)}
      </ul> : <p className="flow-empty">Restart the updated bridge to load the in-track action catalog.</p>}
      <p className="flow-section__note">In-track timing uses absolute song beats. Actions reduce low EQ or channel volume and restore the executor baseline; they do not loop, seek, or change playback speed.</p>
    </details>
    <ol className="set-setup-steps">
      <li><strong>Prepare adjacent transitions</strong><p>Reuse local analysis and pair caches for each neighboring song. This does not call Meta or start playback.</p>
        <button className="button button--secondary" disabled={locked || (b?.queue.length ?? 0) < 2} onClick={() => void run("prepare-transitions")}>{preparing && b?.buildPhase === "transitions" ? "Preparing candidates…" : "Prepare transitions"}</button></li>
      <li><strong>Mount one playlist</strong><p>The app uses one prepared playlist at a time. Export mounts the current selection; import replaces it with the imported song order.</p>
        <div className={`mounted-playlist ${exported ? "mounted-playlist--ready" : ""}`} aria-label="Currently mounted playlist">
          <strong>{exported ? `Mounted in this app · ${mountedPlaylist.tracks.length} songs` : "No playlist mounted for this selection"}</strong>
          {exported ? <>
            <p className="mounted-playlist__filename">{mountedPlaylist.filename}</p>
            <p>{mountedPlaylist.source}</p>
            <details className="panel-details"><summary>View mounted song order ({mountedPlaylist.tracks.length})</summary>
              <ol>{mountedPlaylist.tracks.map(t => <li key={t.id}>{t.title}</li>)}</ol>
            </details>
            <details className="panel-details"><summary>Playlist file path</summary><code className="set-setup-path">{mountedPlaylist.path}</code></details>
            <p className="mounted-playlist__mixxx"><strong>Mixxx setup</strong><br />Open this playlist in Mixxx and click its first song, “{mountedPlaylist.tracks[0]?.title}”. App mounting does not select a playlist in Mixxx; Step 3 checks the staged songs.</p>
          </> : <p>{b?.queue.length ?? 0} songs currently selected. Prepare their transitions, then export and mount them, or import another playlist below.</p>}
        </div>
        <div className="playlist-source-options" role="group" aria-label="Choose playlist source">
          <button type="button" className={`button ${playlistMode === "selection" ? "button--secondary" : "button--ghost"}`} aria-pressed={playlistMode === "selection"} disabled={busy} onClick={() => setPlaylistMode("selection")}>Use current selection ({b?.queue.length ?? 0})</button>
          <button type="button" className={`button ${playlistMode === "import" ? "button--secondary" : "button--ghost"}`} aria-pressed={playlistMode === "import"} disabled={busy} onClick={() => setPlaylistMode("import")}>Import another playlist</button>
        </div>
        {playlistMode === "selection" ? <>
          <p>{exported ? "The playlist above is mounted. Export again only if you need a new file." : "Export the selected order to an M3U8 file and mount it for Step 3."}</p>
          <button className="button button--secondary" disabled={locked || !b?.activationReady || playing} onClick={() => void run("activate")}>{exported ? "Re-export & mount current selection" : "Export & mount current selection"}</button>
        </> : <PreparedPlaylistImport key={mountedPlaylist.contextKey} disabled={disabled || preparing || !state.connected || !b || playing} busy={busy} onBusy={setBusy} mountedCount={exported ? mountedPlaylist.tracks.length : 0} onImported={() => setPlaylistMode("selection")} />}
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
            <h4>Transition actions · handoff-relative beats</h4>
            <ul>{edge.routine?.actions.map((action, i) => <li key={i}><code>{action.function}</code> · {action.deck ? `${action.deck} · ` : ""}beat {action.start_beat}{action.duration_beats != null ? ` · ${action.duration_beats} beats` : ""}{action.target_bpm != null ? ` · ${action.target_bpm} BPM` : ""}{action.curve ? ` · ${action.curve}` : ""}{action.fade_out_seconds != null ? ` · fade ${action.fade_out_seconds}s` : ""}</li>)}</ul>
            <InTrackActions actions={edge.routine?.in_track_actions} />
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

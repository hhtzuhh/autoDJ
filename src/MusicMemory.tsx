import type {MemoryEpisode} from './memoryEpisodes';
import {inTrackAtCapture, transitionAtCapture} from './memoryEpisodes';
import type {MemoryEvent} from './PlanningMemory';
import {songTitle} from './crowdChanges';

export function MemoryEpisodeCard({episode, tracks = []}: {episode: MemoryEpisode; tracks?: {id: string; title: string}[]}) {
  const title = (id?: string, saved?: string | null) => saved || (id ? songTitle(tracks.find(t => t.id === id)) : 'Unknown song');
  return <details className="memory-episode">
    <summary><span className="memory-episode-summary"><time>{new Date(episode.capturedAt).toLocaleTimeString('en-GB')}</time>
      <strong>{episode.tracks.length ? episode.tracks.map(t => title(t.id, t.title)).join(' + ')
        : episode.playbackState === 'stopped' ? 'Playback stopped' : 'Song at capture unknown'}</strong></span></summary>
    <p className="crowd-note">{episode.durationSeconds.toFixed(1)}s capture{episode.fresh === false && " · historical"}</p>
    {episode.tracks.length > 0 && <p className="crowd-note">Playing decks · {episode.tracks.map(t => `Deck ${t.deck}, beats ${t.firstBeat ?? '?'}–${t.lastBeat ?? '?'}`).join(' · ')}</p>}
    <div className="plan-action-heading">Audience movement</div>
    <p>{episode.crowd.summary ?? 'No usable audience description.'}</p>
    <p className="crowd-note">{episode.crowd.movement_amplitude} amplitude · {episode.crowd.movement_continuity} movement · trend {episode.crowd.within_clip_trend}</p>
    {episode.crowd.signalSource === 'gesture' && <p className="crowd-note">Operator gesture · {episode.crowd.signalGoal ?? 'unknown'} request. This is an instruction, not a preference measurement.</p>}
    {!episode.moments.length && <p className="crowd-note">Transition / in-track context was not recorded for this clip.</p>}
    {episode.moments.map((moment, i) => <div key={`${moment.planId}-${i}`}>
      <div className="plan-action-heading">Transition at capture</div>
      <p>{title(moment.sourceTrack, moment.sourceTitle)} → {title(moment.targetTrack, moment.targetTitle)}<br />
        {moment.technique?.replaceAll('_', ' ') ?? 'Technique unknown'}{moment.commonBpm != null && ` · ${moment.commonBpm} BPM`}<br />
        <strong>{transitionAtCapture(moment)}</strong></p>
      <div className="plan-action-heading">In-track at capture</div><p>{inTrackAtCapture(moment)}</p>
      {moment.inTrackCompletedDuringClip.length > 0 && <p className="crowd-note">{moment.inTrackCompletedDuringClip.length} action completion(s) recorded during this clip.</p>}
    </div>)}
    {episode.association !== 'capture_time_telemetry' && <p className="crowd-warning">Playback coverage is incomplete; this association is uncertain.</p>}
    <details><summary>Evidence & planned action parameters</summary>
      <p className="crowd-note">{episode.setRunId ?? 'Unassigned'} · Observation {episode.jobId?.slice(0,8) ?? 'unknown'} · Visibility {episode.crowd.visibility ?? 'unknown'} · Camera {episode.crowd.cameraMotion ?? 'unknown'}</p>
      <p className="crowd-note">Only executor journal events confirm action execution. A selected plan or an executor waiting for its cue does not.</p>
      <pre>{JSON.stringify(episode, null, 2)}</pre>
    </details>
  </details>;
}

export function MusicMemory({episodes, events, tracks, warning}: {episodes: MemoryEpisode[]; events: MemoryEvent[]; tracks: {id: string; title: string}[]; warning?: string | null}) {
  return <details className="crowd-region music-memory">
    <summary className="memory-section-summary">2 · Memory · music & audience <span>({episodes.length})</span></summary>
    <p className="crowd-note">{episodes.length} listening moments in this view · linked at capture time, not when Meta replied.</p>
    <details><summary>How the DJ uses this memory</summary>
      <p>Ordinary movement is recorded with the playing song. Only an explicit gesture requests a change. That request can reference up to four fresh moments from this set; earlier sets remain archived for review.</p>
      <p>Compare repeated observations before / during / after an action. A gesture is an operator request; movement alone does not prove enjoyment or that an action caused a reaction. Unknown evidence stays unknown.</p>
    </details>
    {warning && <p className="crowd-warning">{warning}</p>}
    {!episodes.length && <p className="crowd-note">No song-linked observations in this view yet. Adaptive DJ records observations with playback context.</p>}
    {episodes.map(episode => <MemoryEpisodeCard key={episode.id} episode={episode} tracks={tracks} />)}
    <details><summary>Technical audit · {events.length} events</summary>
      <p className="crowd-note">Requests, decisions and execution events are retained separately from the listening moments above.</p>
      <ul>{events.slice(-20).map(row => <li key={row.id}>{new Date(row.at * 1000).toLocaleTimeString('en-GB')} · {row.kind} · {row.data.status ?? row.data.summary ?? 'Recorded'}</li>)}</ul>
    </details>
  </details>;
}

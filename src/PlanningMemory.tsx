import {MemoryEpisodeCard} from "./MusicMemory";
import type {MemoryEpisode} from "./memoryEpisodes";
export interface MemoryEvent {
  id: string;
  sessionId?: string;
  setRunId?: string | null;
  kind: string;
  at: number;
  trackIds: string[];
  data: {jobId?: string; summary?: string; status?: string; reason?: string; capturedAt?: string;
    playback?: {association: string}; afterExecutionEventId?: string | null};
}

export interface MemoryContext {
  scope?: string;
  recentEpisodes?: MemoryEpisode[]; relatedEpisodes?: MemoryEpisode[];
  sessionId: string;
  recentObservations: MemoryEvent[];
  recentDecisionsAndExecution: MemoryEvent[];
  relatedHistory: MemoryEvent[];
}

export function PlanningMemory({context}: {context?: MemoryContext | null}) {
  if (!context) return null;
  const rows = [...context.recentObservations, ...context.recentDecisionsAndExecution, ...context.relatedHistory];
  return <details className="panel-details">
    <summary>Memory supplied to this decision · {context.recentObservations.length} observations · {context.relatedHistory.length} historical records</summary>
    <p className="panel-details__note">{context.scope === 'current_set_only'
      ? 'Demo scope: recent song-linked observations are supplied only for explicit gesture requests. Past sets stay archived; ordinary rolling calls receive no audience memory.'
      : 'Context saved with this earlier decision. Past audiences may differ; movement is not enjoyment.'}</p>
    {context.recentEpisodes?.map(episode => <MemoryEpisodeCard key={episode.id} episode={episode} />)}
    {!!context.relatedEpisodes?.length && <details><summary>Related moments from earlier runs</summary>{context.relatedEpisodes.map(episode => <MemoryEpisodeCard key={episode.id} episode={episode} />)}</details>}
    <details><summary>Supporting event records</summary>
    {rows.length === 0 ? <p className="panel-details__note">No relevant memory yet.</p> : <ul>{rows.map(row =>
      <li key={row.id}><strong>{row.kind} · {row.id.slice(0, 8)}</strong> · {new Date(row.at * 1000).toLocaleTimeString("en-GB")}
        <p>{row.data.summary ?? row.data.reason ?? row.data.status ?? "Recorded event"}</p>
        {row.data.playback && <small>{row.data.playback.association.replaceAll("_", " ")}</small>}
      </li>)}</ul>}
    </details>
    <details><summary>Exact memory context</summary><pre>{JSON.stringify(context, null, 2)}</pre></details>
  </details>;
}

import type {CopilotState} from "./types";

export function preparedPlaylistView(builder: CopilotState["setBuilder"]) {
  const activation = builder?.activation;
  const tracks = builder?.queue ?? [];
  const mounted = Boolean(builder?.status === "ready" && builder.activationReady
    && activation?.status === "playlist_ready" && activation.trackCount === tracks.length && tracks.length >= 2
    && (activation.builderRevision == null || activation.builderRevision === builder.queueRevision)
    && (activation.orderedTrackIds == null || JSON.stringify(activation.orderedTrackIds) === JSON.stringify(tracks.map(t => t.id))));
  return {
    mounted,
    tracks,
    filename: mounted ? activation!.playlist.split(/[\\/]/).pop() ?? activation!.playlist : null,
    path: mounted ? activation!.playlist : null,
    source: builder?.selectionSource?.kind === "mixxx_playlist"
      ? `Imported from ${builder.selectionSource.filename}` : "Exported from the current selection",
    // A new selection/order/export invalidates local file previews and notices.
    contextKey: JSON.stringify([builder?.buildId, builder?.queueRevision,
      builder?.orderedTrackIds, activation?.playlist]),
  };
}

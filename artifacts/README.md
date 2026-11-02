# Generated local data

All contents except this README are ignored by Git.

- `time-map/<audio-sha256>/`: normalized input, neural analysis, temporary
  demix/spectrogram data, DSP analysis JSON and analyzer log.
- `live-performance/`: prepared and active queues, pair caches, playlists,
  model-call records, transition plans and crowd decisions.
- `mixxx-runtime/`: MIDI intent log, takeover/cancellation and rebase signals.
- `crowd/`: uploaded/captured footage and vision results.

For an empty-output run, stop all services and run `npm run data:fresh`.
It moves existing generated data into `.local/archives/` inside this repository
and retains songs. It does not copy or restore the outer workspace's results.
Model weights and installed Python environments are dependencies, not song
analysis caches; the development toolchain still lives in the sibling workspace.

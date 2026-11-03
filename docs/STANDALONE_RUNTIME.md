# Standalone runtime adaptation

The November 3 product adaptation promotes the project's MIDI observer, feedback
transport, mapping, lifecycle supervision and transition execution into this
repository. These components previously ran from a private development workspace.
They are existing project work adapted for packaging, not newly invented features.

`backend/live_bridge.py` imports sibling product modules directly. The supervisor
launches `backend/transition.py` with its own Python interpreter. The executor
uses `routine_executor.py` for parameterized actions; neither calls a model.
Observer and executor share the same cancellation, takeover, command-log and
safe-seek rebase files under `artifacts/mixxx-runtime/`.

`runtime_paths.py` defaults to this repository, independently of the working
directory. The bridge sets `DJ_DATA_ROOT`, `DJ_RUNTIME_DIR` and the selected
`DJ_ANALYSIS_ROOT` before importing runtime modules; children inherit them.
`--data-root` also selects the bridge's local `.env`. No private workspace
configuration is searched. The Crowd service reads the repository `.env` or
process environment and retains its own storage setting.

The `essentia_dsp` analyzer uses the bridge interpreter unless explicitly
overridden. All-In-One remains a separate adapter with product-local normalization
and feature extraction, plus a configurable external inference executable.
Switching analysis backends retains the original cache separation and requires
new transition preparation/planning. No model weights, private songs, generated
analyses or credentials are distributed.

The lifecycle queue reader keeps the persisted queue contract. Live recovery
shares the product pair analyzer instead of dynamically importing an experimental
pair module. The unnecessary experimental queue CLI and rolling planner were not
promoted. The supplied Mixxx mapping retains the existing protocol unchanged.

## Verification

The isolated check copied only nonignored product source files into a temporary
directory outside the development workspace, created a fresh Python 3.11 virtual
environment, installed `requirements.txt`, and installed frontend dependencies
with `npm ci`. No private `.env`, audio, analysis caches or sibling code was copied.

- 318 backend tests pass, including observed-position execution, safe seek in both
  directions, takeover, lifecycle, recovery, MIDI feedback and startup from an
  unrelated working directory.
- 39 frontend flow tests and 18 Crowd service tests pass; the production build
  passes. The Crowd persistence test now waits for worker release after terminal
  model status, removing an async test race without changing service behavior.
- `scripts/smoke_dsp.py` is the reproducible real-DSP check: two generated WAV
  signals, isolated analyzer subprocesses, pair generation and executor dry-run.
  It passed with two analyzed tracks and ten candidate pairs in the isolated
  checkout. Its plans are explicitly synthetic, not Meta decisions.

The bridge entrypoint test mocks the MIDI observer and HTTP server. Actual Mixxx
playback and Meta API calls were not repeated for this packaging change; the
existing live service was left running. Full audio acceptance still requires a
playback rehearsal after switching to the product runtime. Optional All-In-One
normalization is unit-tested; fresh model download/inference is not verified here.

This is a locally verified macOS setup, not verification of Linux/Windows or a
published repository. Follow the main README for setup and the mapping README for
Mixxx configuration.

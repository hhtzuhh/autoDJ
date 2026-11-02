# AI DJ Copilot

A human-first AI DJ system for live performance. Meta models choose among
locally measured, time-consistent transition candidates; a deterministic
supervisor validates the decision against real Mixxx state; the human can
change the performance or take over at any time.

This public repository is being built for the Meta Global AI Developer
Hackathon. The current React dashboard is the first product adaptation of a
separately validated local prototype.

## Current status

- Implemented here: responsive React/TypeScript control-room UI, typed state
  contract, queue/Deck/plan/event views, deterministic adjacent-pair analysis,
  and bounded Meta selection for the current A–B–C window.
- Development bridge implemented: the verified private-workspace Mixxx
  observer can now feed real Deck identity, play state, beat, position, sync,
  queue revision, plan provenance, and events into the dashboard. This bridge
  still depends on the sibling experimental workspace and is not yet the
  standalone public observer implementation.
- Set-list adapter implemented: an analyzed, versioned runtime queue resolves
  to hash-verified local audio and an ordered Mixxx playlist. After A→B, the
  bridge can recycle the stopped Deck, verify the loaded C identity, promote
  the preserved B→C decision, and arm it without another model call. This
  automatic three-track path has completed a live Mixxx acceptance run.
- Set-builder development slice implemented: the live UI can send a bounded
  natural-language set request to Meta, show the chosen local tracks, run
  missing per-track analysis in one background worker, and prepare only the
  adjacent pair caches. Dragging a prepared track preserves unchanged edges
  and schedules only new adjacencies. Once measured BPM is available, a
  second bounded Meta pass may reorder the set; local code accepts it only
  when it reduces difficult adjacencies. Large BPM gaps produce explicit
  `clean_break` candidates instead of being discarded. A separate Meta
  selector and native-tempo executor compile them into a target-start →
  fader-cut → source-stop handoff; they never enter the beat-sync crossfade
  compiler.
- Safe set activation is split into two explicit stages. **Export Mixxx playlist**
  writes a hash-verified playlist without mutating the active queue. After the
  operator opens it and selects its first row, **Start set** requires both
  Decks stopped, verifies the first two full audio identities, obtains the
  first Meta plan, atomically promotes the queue, and starts Deck 1. The plan
  remains unarmed until **Approve & arm**. This path has automated coverage but
  has not yet completed a live ten-track Mixxx acceptance run.
- The live bridge exposes separate **Plan with Meta** and **Approve & arm**
  actions. Planning makes one model request, validates the returned candidate
  locally, and never writes to Mixxx. Arming remains a distinct human action
  behind the existing lifecycle preflight and executor.
- Queue transition labels now distinguish analyzed candidates from execution
  readiness. `Standby ready` means a deterministic emergency candidate exists
  but no Meta plan is armed; `Plan ready`, `Armed`, and `Executing` are separate
  states. If a Meta request fails, local code revalidates the standby against
  the latest observed beat and uses it only while sufficient arm runway
  remains. Otherwise the edge becomes `Needs human` instead of silently
  waiting or seeking backward.
- Rolling background planning now uses one joint window of up to three future
  transitions, replenished at promotion and retried after failures. Existing
  valid plans remain available while planning; failures show `degraded`. Queue
  tails use one or two edges. Synthetic ten-track regressions cover promotion,
  timeout, stale results, and takeover.
  A live ten-track run completed all nine transitions, but repeated Meta
  timeouts depleted the future buffer.
  Planning identity now survives arm's execution-ID change; actual replanning
  advances a separate epoch. Candidate search retains timing alternatives as
  well as musical leaders (at most 16³ local combinations, ten sent to Meta).
  The dashboard preserves each decision's request provenance and executable
  cues through promotion, separately from refill request failures. It exposes
  API phase, reasoning usage, retries, buffer depth, and timing risks.
  Low-reasoning API probes completed in 3.89–4.88 seconds; these small samples
  do not guarantee latency. A subsequent live run completed eight handoffs with
  eleven pre-cue seeks; all nine Meta calls succeeded in 3.23–7.43 seconds,
  with no timeout or discard. Stopping playback cancelled the final transition.
  A separate tail test completed the last two handoffs with two pre-cue seeks.
  These runs do not establish uninterrupted nine-handoff acceptance for the
  latest version or automatic recovery after seeking past a cue.
  Restarted bridges invalidate unowned persisted plans and require replanning
  before arm rather than trusting a saved `executing` status.
- After a cancelled or stale transition, **Recover & arm** uses the one
  currently playing deck and prepares only the next track in the idle deck.
  It plans forward from the latest observed beat, then rechecks normal arm
  preconditions. With playback stopped, **Recover from position** prepares a
  plan without seeking or starting the source; resume it before arming.
  Cancelled plans cannot be revived by toggling Auto arm, and the near-end
  continuity guard stays disabled after cancellation until explicit recovery.
- Not included here yet: a standalone public Mixxx observer/executor and
  redistributable audio fixtures.
- The UI labels simulation data explicitly. It does not present fixture events
  as live model or audio-engine behavior.
- Crowd observation: a separate manual-upload panel sends a silent clip to
  Muse Spark 1.3 and shows regional movement, timestamp evidence and visibility
  limits. It is observation-only: no queue, planner or Mixxx control connection.
  Uploaded footage is not labeled as a live camera feed. See
  [setup, limitations and verification](docs/CROWD_OBSERVATION.md).

## Run the frontend

Requires Node.js 20 or newer.

```bash
npm install
npm run dev
```

Open the Vite URL, normally `http://127.0.0.1:5173`.

For optional Crowd observation, install ffmpeg/ffprobe and use Node >=22.9.
Set `MODEL_API_KEY` in your environment or repository `.env`, then run
`npm run crowd` in a second terminal. The dashboard proxies `/api/crowd` to
the loopback-only service on port 8767. The panel works alongside either DJ
simulation or the live bridge. Uploaded Analyze calls and active webcam cycles
make real Meta requests. **Start webcam observation** captures six silent seconds
per cycle; **Enable adaptive DJ** additionally requires `?source=live`, an active
continuous set and Auto arm. It stages bounded future reorders/transition choices
for the next completed handoff. Human takeover stays authoritative.

Computer webcam → Muse observation was verified once. The full adaptive audio
loop is implemented and covered by synthetic tests, but a real dance/Mixxx
rehearsal remains pending. See [behavior, gestures and limits](docs/CROWD_OBSERVATION.md).
The vision service does not require the private experiment workspace or SAM.
Actual DJ execution still uses the development bridge described below.

### Agent flow view

Select **Agent flow** beside **Control room**, or open
`http://127.0.0.1:5173/?source=live&view=flow` with the live bridge running.
The view follows three paths: set preparation, the live Observe → Plan →
Validate → Execute → Verify loop, and optional webcam feedback into future
plans. Select a node to inspect its reported evidence. The request ledger
shows retained model-call status, latency and token usage where available.

Click the **Select & order** node to open the playlist dialog. **Build a new
playlist** accepts a natural-language request; explicit song counts populate
the visible Tracks field, which is authoritative when sent. The conversation
shows the submitted request and actual preparation status. Sending invokes the
same Meta set builder as Control room; opening the dialog makes no model call.
**Use a saved playlist** lists completed builds from this project's ignored
`artifacts/live-performance/saved-sets/`. Loading checks local songs and analysis
identity, reuses analyses, and checks adjacent pairs. It changes preparation only;
Export and Start are separate actions. Original API-call records remain historical.

Lights reflect reported state: cyan is working, green is complete/available,
amber is waiting, red is failed/blocked, and gray is idle or unavailable.
Cached audio analysis is distinguished from computation in the recorded build;
older results without provenance are labeled accordingly. Live execution
requires fresh bridge telemetry and an owned, running executor. The handoff
counter resets on bridge restart and excludes restored plans. Disconnection
removes live execution claims; simulation is explicitly labeled.

Select **Analyze audio** to see every selected track, its analysis source,
status, and recorded job ID/runner/duration. Complete metadata reuse, reuse of
All-In-One's internal neural-output cache with DSP recomputation, and fresh
neural inference are separate cases. A custom/injected analyzer is not labeled
as a neural run without evidence. Old results retain unknown execution history.
Analysis jobs currently run serially on the local CPU; Modal fan-out is not
implemented. The per-track records make that future execution path inspectable.

Viewing this page does not make model calls or advance playback. The agent
observes continuously and requests bounded Meta decisions when needed; it
does not call the model on every UI update. Both in-app views stay mounted,
so switching between them does not stop webcam capture. Hiding the browser
tab still stops capture. Human controls remain available in either view.

Verified with the live idle bridge, node inspection and automated state
projection/telemetry tests. A full playing-set and audience-feedback rehearsal
of the new display is still pending. Run `npm run test:flow` with Node >=22.18
(native TypeScript stripping), plus the backend suite below.

### Development live bridge

From the `submission/` directory, put your private songs in
`local-assets/samples/` **before starting the bridge**. This folder and
`artifacts/` are inside the product checkout, but Git ignores their contents
except the folder README files. Existing outer-workspace analyses and playlists
are not imported. The bridge can start with no analysis, queue or plan.

```bash
npm run data:init
npm run bridge
# In a second terminal:
npm run dev
```

Open Mixxx with the installed MIDI mapping to supply observed state. The bridge
still uses the sibling workspace's Python environments and observer/executor
implementation; this change relocates product data, not those dependencies.
`--workspace-root` identifies that toolchain; `--data-root` overrides the product
data location and defaults to this repository regardless of the working directory.

For a repeatable empty-output run, stop bridge and crowd services, then run
`npm run data:fresh`. Previous outputs (including saved playlists) move to
`.local/archives/` inside this checkout; songs remain. Restart the bridge after
changing songs. The analyzer's intermediate WAV, neural output and DSP data all
use this run's `artifacts/time-map/`; it does not reuse the outer neural cache.
The observer refreshes its catalog as new analyses complete.

Then open:

```text
http://127.0.0.1:5173/?source=live
```

The Queue panel now also exposes the development set builder. Its catalog is
bounded to `local-assets/samples` by default; audio paths and source files are
never returned to the browser. **Build with Meta** makes one strict curation
call for membership and order, then reports track and edge analysis status as
the local worker progresses. Once every edge is ready, select **Export Mixxx
playlist**, open the generated playlist, and select its first row. **Start set**
verifies the first two rows before changing the active queue or playing Deck
1. It plans but does not arm the first transition.

Observed Deck state is never overwritten directly. The Arm action never sends
MIDI directly: it first creates a unique plan instance and delegates identity,
queue revision, transport, target-stop, and latest-arm checks to the existing
lifecycle supervisor. Failed preconditions return HTTP 409 and leave Mixxx untouched.
Takeover is enabled only for a dashboard-owned plan in `executing` state and
uses the existing preserve-playback cancellation signal. During automatic
Deck recycling, Takeover cancels the pending load/arm path and preserves the
currently playing Deck.

Live acceptance on 2026-10-30 observed Mixxx Deck 1 playing Boomerang. The
bridge reported beat 209 at 95.410 seconds; the React view subsequently showed
beat 216 at approximately 98 seconds, performance revision 10, queue revision
3, and the same human seek/play/crossfader events. This verifies the read path.

Write acceptance on 2026-10-30 first confirmed that Arm returned HTTP 409 and
did not touch Mixxx while the target deck was empty. With both analyzed tracks
loaded, the bridge accepted a unique plan instance, precisely cued the target,
ran the two-bar transition, stopped the source, restored sync and tempo state,
and exited successfully. The final observer state showed Boomerang playing on
Deck 1, Bass Addict stopped on Deck 2, crossfader at Deck 1, and plan status
`completed` at performance revision 19.

Takeover acceptance on 2026-10-30 triggered the dashboard action during an
active fade. The API immediately returned `cancelled` with reason
`human_dashboard_takeover`; the executor consumed the plan-specific preserve
signal and exited successfully. Seven seconds later both Decks were still
playing, their leader/follower sync roles were unchanged, and the crossfader
remained at its handoff position. No stop or cleanup command followed.

### Meta configuration

Copy `.env.example` to `.env` and set `MODEL_API_KEY`. The model, compatible
API base URL, and timeout are configured with `META_MODEL`, `META_API_BASE`,
and `META_TIMEOUT_SECONDS`. `META_REASONING_EFFORT` defaults to `low` and
`META_MAX_TOKENS` to `2048` (reasoning plus visible output). These controls use
the documented [Meta Chat Completions reasoning API](https://dev.meta.ai/docs/reasoning).
Truncated responses are rejected before compilation. Request metrics never
include credentials or internal reasoning text. The timeout remains 30 seconds;
short refill windows still show a risk instead of claiming a guaranteed buffer.

Set building uses a separate preparation budget: `META_CURATOR_MAX_TOKENS=4096`
and `META_CURATOR_TIMEOUT_SECONDS=60`. Only a response explicitly truncated by
the output limit gets one larger-budget retry (double, capped at 8192). Partial
output is always rejected; timeout, HTTP and schema errors are not retried.
This applies to initial curation and post-analysis reordering, not live transition
planning. Safe attempt/usage metrics are retained in private set-builder state.
If the post-analysis pass still fails, **Retry planning only** reuses completed
track/pair analysis instead of rebuilding the set. A failed plan cannot display
Ready to activate. The Build button shows the numeric track count actually sent;
that field takes precedence over a count embedded in the description.

On 2026-11-01, a real failed ten-track post-analysis pass was resumed without
re-running analysis or starting Mixxx playback. It completed on the first
4096-token attempt in 7.97 seconds, using 2079 completion tokens (1555 reasoning
and 524 visible). The prepared set returned to ready; the active queue revision
and stopped Decks were unchanged. Retry bounding, rejection of truncated output,
analysis reuse and readiness/stale-result guards are covered by offline tests.

The planner sends at most ten locally feasible candidates in exactly one Meta
Model API call. The response may select a candidate and request the supported
bass-swap template, but it cannot introduce track IDs, beat positions, or
transition lengths. Unknown candidates, malformed output, expired exits, and
incomplete A–B–C windows fail closed before the arm boundary.

A real `muse-spark-1.3` acceptance call on 2026-10-30 selected one of ten
Boots → Bass → Boomerang candidates and passed strict local validation. The
live endpoint was also tested against the then-current Mixxx state: it returned
HTTP 409 because the current queue position did not have both a next and a
following track. That rejection caused no Mixxx write and is the expected
rolling-window behavior.

Later live acceptance on 2026-10-30 armed and completed a Meta-selected
Boots → Bass transition from the dashboard. The same single Meta call preserved
a Bass → Boomerang following draft, and the UI displayed analyzed cue, mix
window, and incoming-entry markers on both Deck waveforms. The first transition
completed successfully, but its 32-beat executor tail plus the manual Deck swap
caused the preserved second exit to expire before promotion. The system rejected
the stale draft without seeking backward. After an explicit human pause and
rewind, the deterministic live replanner selected Bass beat 348 → Boomerang
beat 228; the second transition completed with executor return code 0 and
Boomerang continuing on Deck 1. This validates fail-closed recovery, not an
uninterrupted three-track run. Automatic Deck loading or a larger end-to-end
swap runway remained required in that acceptance run. The subsequent set-list
loader reserves 128 beats of B dwell and reduces the executor cleanup tail to
eight beats.

The uninterrupted three-track acceptance on 2026-10-30 used the dedicated
`Boots → Bass Addict → Boomerang` Mixxx playlist. One `muse-spark-1.3` call
selected both legs. A→B completed with executor return code 0; the bridge then
recycled Deck 1, loaded Boomerang, verified its full analysis identity,
promoted the preserved B→C draft without another model call, and armed the
second executor. B→C also returned 0. Final performance revision 33 showed
Boomerang continuing on Deck 1, Bass stopped on Deck 2, and lifecycle status
`completed`. No human Deck load occurred after the first arm.

For a production build:

```bash
npm run build
npm run preview
```

### Transition candidate pipeline

The public repository contains the deterministic adjacent-pair scorer and
versioned cache used before any model selection. It consumes preprocessed
`track-analysis-v1.json` metadata; it does not require or publish the source
audio. Candidate generation is explicitly marked `deterministic_heuristic_not_ai`.

Run its synthetic acceptance suite with:

```bash
python -m unittest discover -s backend -p 'test_*.py' -v
```

The tests verify that a ten-track queue creates nine directional pair analyses,
reordering builds only new adjacent edges, caches are reused, incompatible
tempo produces clean-break candidates without entering the sync planner, and
queues longer than ten tracks remain supported. A mock asynchronous acceptance
test also verifies that a request with initially unanalyzed tracks reaches
analysis-ready within a five-second deadline without invoking All-In-One.
also verify bounded, temporally feasible A–B–C construction, strict Meta output
validation, exactly one model call, compilation into the executor plan shape,
and exact playlist generation from hash-verified local audio.

## Demo interaction

1. The initial state shows B playing and a validated B→C fixture plan.
2. Select **Extend B past cue**. The old exit expires while B keeps playing.
3. The deterministic recovery path creates a future plan from the new beat.
4. Select **Approve & arm** to show the local lifecycle boundary.
5. Select **Take over** at any time. Future automation is cancelled while
   current playback and mixer state are preserved.

Without `?source=live`, this is a UI simulation. Live mode uses the same typed
contract and connects Arm and Takeover to the local lifecycle supervisor.

## Safety and product boundaries

- The model never sends beat-by-beat Mixxx commands.
- Local code owns identity, timing, queue revision, and lifecycle validation.
- Invalid, late, or unavailable plans do not interrupt the current song.
- Meta failure falls back only to a previously analyzed candidate that still
  passes a fresh live-beat runway check; an expired fallback becomes an
  explicit human-action state.
- Human transport, seek, loop, and crossfader actions take priority.
- Pre-transition Deck volume, Low EQ, and filter shaping preserve the timed
  cue; the same channel controls at or after transition start become takeover.
- Private development tracks, credentials, recordings, model caches, and
  generated artifacts are not part of this repository.

## Hackathon disclosure

The control architecture, music-analysis pipeline, constrained planning,
Mixxx observer/executor integration, takeover behavior, and this product UI
were developed during the hackathon. The public product integration is still
in progress; claims will be updated only after runnable verification.

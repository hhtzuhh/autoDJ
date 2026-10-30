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
- The live bridge exposes separate **Plan with Meta** and **Approve & arm**
  actions. Planning makes one model request, validates the returned candidate
  locally, and never writes to Mixxx. Arming remains a distinct human action
  behind the existing lifecycle preflight and executor.
- Not included here yet: a standalone public Mixxx observer/executor and
  redistributable audio fixtures.
- The UI labels simulation data explicitly. It does not present fixture events
  as live model or audio-engine behavior.

## Run the frontend

Requires Node.js 20 or newer.

```bash
npm install
npm run dev
```

Open the Vite URL, normally `http://127.0.0.1:5173`.

### Development live bridge

From the private hackathon workspace, with the Mixxx MIDI mapping installed:

```bash
experiments/mixxx-control/.venv/bin/python \
  submission/backend/live_bridge.py
```

Then open:

```text
http://127.0.0.1:5173/?source=live
```

Observed Deck state is never overwritten directly. The Arm action never sends
MIDI directly: it first creates a unique plan instance and delegates identity,
queue revision, transport, target-stop, and latest-arm checks to the existing
lifecycle supervisor. Failed preconditions return HTTP 409 and leave Mixxx untouched.
Takeover is enabled only for a dashboard-owned plan in `executing` state and
uses the existing preserve-playback cancellation signal.

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
and `META_TIMEOUT_SECONDS`; no model name or credential is embedded in the
planner.

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
rolling-window behavior; a live Meta-selected plan has not yet been armed from
this dashboard.

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
tempo is blocked, and queues longer than ten tracks remain supported. They
also verify bounded, temporally feasible A–B–C construction, strict Meta output
validation, exactly one model call, and compilation into the executor plan
shape.

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
- Human transport, seek, loop, or mixer actions take priority.
- Private development tracks, credentials, recordings, model caches, and
  generated artifacts are not part of this repository.

## Hackathon disclosure

The control architecture, music-analysis pipeline, constrained planning,
Mixxx observer/executor integration, takeover behavior, and this product UI
were developed during the hackathon. The public product integration is still
in progress; claims will be updated only after runnable verification.

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
  contract, queue/Deck/plan/event views, and an interactive simulation of cue
  expiry, uninterrupted replanning, arming, and human takeover.
- Development bridge implemented: the verified private-workspace Mixxx
  observer can now feed real Deck identity, play state, beat, position, sync,
  queue revision, plan provenance, and events into the dashboard. This bridge
  still depends on the sibling experimental workspace and is not yet the
  standalone public observer implementation.
- Not connected here yet: live Meta Model API request, write controls into the
  local audio executor, and redistributable audio fixtures.
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

Live Deck state is read-only. The Arm action never sends MIDI directly: it
first creates a unique plan instance and delegates identity, queue revision,
transport, target-stop, and latest-arm checks to the existing lifecycle
supervisor. Failed preconditions return HTTP 409 and leave Mixxx untouched.
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

For a production build:

```bash
npm run build
npm run preview
```

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

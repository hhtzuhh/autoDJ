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
- Not connected here yet: live Mixxx observer, Meta Model API call, local audio
  executor, and redistributable audio fixtures.
- The UI labels simulation data explicitly. It does not present fixture events
  as live model or audio-engine behavior.

## Run the frontend

Requires Node.js 20 or newer.

```bash
npm install
npm run dev
```

Open the Vite URL, normally `http://127.0.0.1:5173`.

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

This is currently a UI simulation. Live adapters will produce the same typed
contract without changing the visual layer.

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


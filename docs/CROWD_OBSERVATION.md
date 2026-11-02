# Crowd observation

The dashboard has two explicitly separate inputs: manual uploaded-footage
observation and opt-in computer webcam feedback. Both use Muse Spark 1.3;
SAM is not called. Webcam observations can influence future track order and
transition plans when the operator enables **Adaptive DJ** on a live continuous
set. Manual uploaded footage remains observation-only and cannot control the DJ.
The existing local supervisor and executor remain responsible for audio control.

## Run

Requires Node >=22.9 plus ffmpeg and ffprobe on PATH. No additional npm packages
are required beyond the dashboard dependencies.

1. Set `MODEL_API_KEY` in your shell environment or repository `.env` (see
   `.env.example`). The key stays on the service; it is never sent to the browser.
2. From the repository root, run `npm run crowd` in one terminal.
3. Run `npm run dev` in another terminal and open `http://127.0.0.1:5173`.
4. In **Crowd observation**, select a local video up to 40 MB, choose its start
   time in seconds, and click **Analyze with Spark**. This explicitly uploads
   a prepared clip to Meta and uses API credits.

The default dashboard's DJ controls are simulated. Crowd observation still
makes a real Meta request when Analyze is clicked. The same panel is available
with `?source=live`; it does not depend on the Mixxx bridge being available.

The loopback-only service listens on port 8767. Vite dev (5173) and preview
(4173) proxy `/api/crowd`; a production host would need the same proxy and a
separately managed Node service. A static deployment alone cannot run analysis.
Vite also proxies `/api/adaptive` to the existing live bridge on port 8765.
Only these local dashboard origins are accepted. There is no public hosting or
authentication layer; do not expose the service to the internet.

## Webcam and adaptive DJ

1. Use **Start webcam observation** and allow the computer camera. Audio capture
   is disabled. Each cycle records six seconds, uploads once, waits for Spark
   and remote cleanup, then waits five seconds before the next recording.
   Only one observation job runs at a time; stop manual uploads before capture.
2. Observation works in the default DJ simulation too. To affect real music,
   run the existing development live bridge, open `?source=live`, activate/start
   a continuous set, and enable **Auto arm** using the existing DJ controls.
3. Click **Enable adaptive DJ**. There is no per-decision approval afterward;
   human takeover still cancels future automation. This button is disabled in
   DJ simulation or when the live connection is lost.
4. Keep the camera stable with dancers' upper bodies and arms visible. One
   participant is supported. No camera stream or automatic restart survives
   a reload, hidden tab, disconnection, or capture error.
5. **Stop camera** or **Disable adaptive DJ** stops new crowd-driven changes.
   A plan already promoted/armed remains under the normal DJ controls; use
   **Take over** to cancel audio automation.

The webcam profile uses one fixed `audience` region. It keeps visible movement
separate from explicit demo gestures: both arms overhead continuously for at
least two seconds means `build`; forearms crossed in an X at the chest for at
least two seconds means `ease`. Brief, obscured, conflicting, or uncertain poses
are not commands. This is a user-enabled convention, not inferred intention.

Natural movement follows a documented demo heuristic: two consecutive good,
stable-camera observations within 90 seconds must both show some/most moving
with moderate/large sustained motion (`build`), or none/few moving with
still/subtle absent/intermittent motion (`ease`). Intermediate states hold the
existing direction. This does not establish enjoyment, boredom, musical
preference, dance style, or beat synchronization. Gesture accuracy and useful
musical response have not been calibrated. Incidental poses can be mistaken for
commands; the evidence remains inspectable.

The bridge fetches the completed job from the fixed local vision endpoint. It
requires a current camera session, increasing capture timestamps, unseen job ID,
and capture age no greater than 120 seconds. A five-second browser heartbeat
renews an 18-second lease. Stopping Auto arm, human invalidation/takeover, missing
heartbeat, or a changed planning context prevents pending work from applying.
Unknown/conflicting gestures or unreliable visibility invalidate staged/in-flight
decisions. A natural reversal needs two consistent clips; a brief neutral window
does not revoke a held-gesture decision before its 120-second expiry.
There is a 60-second cooldown after application and after planning failures.

For locked A→B, local code builds at most three orders: retain C/D/E, move D
before C, or move E before C. Played tracks, A and B stay fixed; later tracks
stay in place. Up to nine locally feasible transition-window candidates are
sent to Meta in one request with the visible evidence and movement-following
policy. Meta selects one supplied ID, including keeping the current order.
Available techniques are existing clean breaks or beat-synced crossfades with
validated bar lengths and optional bass swap on the first sync transition.
No arbitrary MIDI, effects, tempo changes, new tracks or instant skips are
accepted from model output.

The choice is staged while A→B remains unchanged. Only at the completed-transition
boundary, before the idle deck is recycled, does the bridge recheck freshness,
control ownership, current beat, queue revision and exact track mapping. Queue
and manifest writes use atomic file replacement with rollback on ordinary
failure; process death between two replacements leaves a revision mismatch
that existing startup checks reject. Sampler slot identities stay fixed.
A restarted bridge needs normal human recovery/activation, especially after
reordering; it does not reconstruct an active camera session.

The dashboard separates observation, staged decision, queue commit, verified
plan promotion, arming and observed execution/completion. A committed queue is
not proof that audio executed. Failed/late model responses preserve existing
playback and plans. Selection and execution records are private JSONL in
`artifacts/live-performance/crowd-decisions.jsonl` under the submission repository.

## Data flow and retention

`selected file → local preparation → Meta Files → Muse Spark → validation → UI`

- At most 8 seconds from the selected start, silent H.264, 640px wide, 15fps.
  At least 2 seconds must remain. Supported input containers are MP4/MOV,
  WebM/Matroska and AVI, subject to installed decoders.
- Source metadata and audio are stripped; Meta receives the generic filename
  `crowd-clip.mp4`. Original filenames are used only in the local file picker.
- One upload and one Muse request per manual click or webcam cycle, no SAM
  request and no automatic retry of a failed observation. One job runs at a time. Polling reads job state, not the model.
- Upload deletion is attempted after success or failure. The job remains running
  until this attempt finishes. A cleanup failure appears separately from the
  observation result. If the process is interrupted, inspect `remote-file.json`
  and the Meta Files dashboard; graceful cleanup cannot be guaranteed on a kill.
- Default storage is ignored `artifacts/crowd/<job-id>/` inside submission. Override with
  `CROWD_ARTIFACTS_DIR` to keep artifacts elsewhere. The service retains the local
  input copy, normalized clip, exact prompt, raw API response, result, and remote
  file cleanup record; these are not automatically deleted or published.
- Download exports the accepted observation and run metadata. Raw API responses
  remain local. A malformed model response is retained but not shown as a valid
  observation. Timestamps link to playback of the exact normalized clip.
- The manual-upload panel can recover its last job across page reloads while the service
  stays running. Restarting the service clears its in-memory job list; saved
  artifacts remain on disk. This is not a persistent media library.

## Interpretation and boundaries

The UI distinguishes the selected source window, request time, analysis-finish
time and unknown recording time. Uploaded footage is not a current measurement
of the live audience and is not synchronized to Mixxx playback.

For each model-defined audience region, the output separates movement spread,
amplitude, continuity, within-clip trend, flow, visibility and timestamp evidence.
Values are qualitative model judgments. Unknown and empty regions are valid;
an empty region list does not establish an empty venue. No enjoyment, emotion,
musical preference or calibrated engagement score is measured.

The strict contract rejects invalid timestamps, missing/cross-region evidence,
unexpected fields, unsupported enums and a trend without two non-overlapping
intervals. Camera movement or insufficient visibility requires unknown values
for affected metrics. This validates structure, not factual accuracy or complete
compliance with instructions inside natural-language evidence.

Known limitations from the private experiments include incidental hair/eyewear
adjustments being described as declining movement, incomplete visibility caveats,
and inconsistent regional boundaries across calls. Manual-upload regions and IDs cannot be
compared across clips. The webcam policy instead requires one stable whole-frame
region and compares coarse labels, without persistent person tracking. Historical trend,
beat synchronization and music-event attribution are unavailable.

## Verification — 2026-11-01

- `npm run build`: passes TypeScript checks and production build.
- `npm run test:crowd`: 14/14 offline tests pass. Includes evidence validation,
  isolation from DJ routes, concurrent-upload rejection, origin/input checks,
  cleanup on model rejection, visible cleanup failure, and actual ffmpeg
  normalization of a generated synthetic video with audio. No private fixture
  or API key is needed for the tests.
- Python backend regression suite: 78/78 pass, including 14 new synthetic
  adaptive regressions for bounded reordering, staging without playback changes,
  freshness/replay/session rejection, unknown/camera-motion holds, takeover during
  inference, completed-boundary commit and promotion, bass-swap propagation,
  expired cues/leases, and rollback after a simulated disk failure.
- Chrome desktop: selected a private operator-provided file through the picker,
  clicked Analyze, observed preparation/model/completion states, and displayed
  a real Muse result. Run `e9ee5ef7-3a99-446e-b5ab-9b539f44e785` completed in
  approximately 23.5 seconds, including a 16.3-second model request. Remote file
  deletion succeeded. These are one-run timings, not a latency guarantee.
- Expanded evidence and clicked the 5–8s interval: the normalized eight-second
  video sought to 5s, played, and was paused at 8s. Download observation produced
  a completed JSON download. DJ fixture revisions remained P18/Q3 throughout.
- Chrome computer webcam: allowed camera for this visit, captured six silent
  seconds, received a valid `webcam-v1` Muse result, then explicitly stopped
  capture. Run `572d8092-51a1-4169-ace1-91a76e01282f`: 18.5s model request,
  approximately 25.3s from upload receipt through successful remote deletion.
  It reported no held control gesture. This is one run, not an accuracy benchmark.
- No real Mixxx playback was started or changed during webcam validation.
  The full webcam → Meta planning → live queue/transition execution loop remains
  unverified with real dancers. Gesture recognition, whether natural movement
  produces useful musical decisions, mobile input, long-running sessions and
  simultaneous live playback still need an operator rehearsal.

Media, raw private observations and screenshots are not included in this public
repository. Code and synthetic validation fixtures are newly developed during
the hackathon. The adaptive implementation is a demo prototype; synthetic control tests and
one real camera observation do not establish a successful live adaptive set.

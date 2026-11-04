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
   In the private workspace, the service also accepts the existing sibling
   `experiments/meta-dj-agent/.env` when the key is otherwise unset.
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
5. **Stop camera** or **Stop new gestures** stops new camera requests. Already
   accepted queued, planning or staged requests continue until applied or their
   original evidence expires. Hiding/closing the browser tab has the same behavior.
   Use **Take over** to cancel pending requests and audio automation.

The webcam profile uses one fixed `audience` region. It keeps visible movement
separate from explicit demo gestures: both arms overhead continuously for at
least two seconds means `build`; forearms crossed in an X at the chest for at
least two seconds means `ease`. Brief, obscured, conflicting, or uncertain poses
are not commands. This is a user-enabled convention, not inferred intention.

Ordinary movement and stillness are recorded with capture-time song context;
they never trigger adaptive planning. Hold number 1 for at least two seconds to
request remaining-song reorder. Arms overhead requests more in-track effect;
crossed forearms requests less. The latter two preserve song order. Each held
pose triggers once, and a clear observed release re-arms it. Unknown does not
count as release. Meta may leave in-track actions unchanged when no suitable
slot exists. A handoff-only change does not fulfill an in-track request and is
not applied. Incidental poses can still be mistaken for commands; inspect the
vision evidence. No enjoyment, preference or causal response is inferred.

### Observation log

**Observed · Capture log** shows every capture, independently of Adaptive DJ or
whether playback is running. Recording creates a local row immediately; service
acceptance, the actual Muse request timestamp, response timestamp, model duration,
accepted observation and errors update that same row. Responses remain visible
when a new capture starts. Clips/evidence, raw saved Meta responses and structured
job status each have a closed-by-default disclosure. Reading history or raw
responses never submits another model request.

The service restores completed/failed `result.json` files from the existing UUID
artifact folders after restart. Older jobs without precise model request timestamps
are labeled explicitly; timestamps are not reconstructed from guesses. The log
continues checking accepted jobs after the camera stops, so a late response is
still visible. Local recordings stopped before service acceptance exist only in
the current page's log. The six-second capture is followed by inference/cleanup
and a five-second pause; it is not a fixed six-second polling interval.

Starting another set or restarting observation does not clear these job records.
Each camera start has a `webcamSession`; each submitted clip has a unique job ID.
New webcam jobs freeze the shared set-run ID at capture start. Adaptive
memory associates capture-time deck/plan telemetry when enabled; observation-only
jobs do not automatically identify the playing set or its action plans.

November 3 log verification: 16 vision-service and 27 frontend tests pass, plus
the production build. Tests cover in-flight calls, raw response retrieval,
history restoration including failures, deduplication, stopped uploads and stale
poll responses. Chrome displayed ten existing webcam records and expanded a saved
raw response. Continuous fresh camera capture with this UI remains an operator check.

The bridge fetches the completed job from the fixed local vision endpoint. It
requires a current camera session, increasing capture timestamps, unseen job ID,
and capture age no greater than 120 seconds. A five-second browser heartbeat
renews an 18-second observation lease. Missing heartbeat stops new observations,
while already accepted requests retain their original 120-second freshness limit.
Stopping Auto arm, human invalidation/takeover, or an unsafe changed planning
context prevents pending work from applying.
Unknown/conflicting gestures or unreliable visibility invalidate staged/in-flight
decisions. Ordinary movement does not revoke an accepted gesture proposal.
Evidence expires after 120 seconds. New explicit gestures can retry after a
clear release; repeated held poses cannot trigger retries. Cancellation, replacement,
or expiry records a terminal discarded reason for any staged proposal.

For locked A→B, the planner considers moving one remaining song next while
preserving every other song's relative order. A number 1 request considers only changed orders; in-track requests keep the order. Up to ten next-song options span the remaining set (all
remaining songs for the usual seven-song demo; evenly spaced choices for longer
sets). Local validation produces at most ten candidates, distributed across
feasible orders, for one Meta request. Meta selects only a supplied ID. No new
tracks or instant skips are added. It plans up to four songs / three handoffs,
with the same parameterized transition and optional in-track catalogs used by
ordinary rolling calls. Empty in-track actions are valid.

Ordinary replenishment and webcam-triggered replanning use the same scheduler.
An observation can queue a request while another call runs; it cannot start a
competing request. The old valid buffer stays available until an adaptive plan
passes validation and commits. A staged adaptive plan reserves the next boundary.
The current entire routine (including in-track actions, ramps and handoff) stays
locked. Future windows are plans, not instructions already sent to Mixxx.

The choice is staged while A→B remains unchanged. Only at the completed-transition
boundary, before the idle deck is recycled, does the bridge recheck freshness,
control ownership, current beat, queue revision and exact track mapping. Queue
and manifest writes use atomic file replacement with rollback on ordinary
failure; process death between two replacements leaves a revision mismatch
that existing startup checks reject. Sampler slot identities stay fixed.
A restarted bridge needs normal human recovery/activation, especially after
reordering; it does not reconstruct an active camera session.

Executor completion alone does not confirm the transport feedback has arrived.
Rotation now waits up to three seconds for the expected incoming deck to be
playing and the expected outgoing deck to be explicitly stopped. Unknown states
are not confirmations. Cancellation interrupts the wait, changed identities or
planning context reject it, and the observer remains free to receive MIDI.
If rotation fails before commit, the pending adaptive decision is marked
`application_failed`, its reason is retained in memory and shown in the webcam
panel, and it is not represented as an applied queue change.

The dashboard separates observation, staged decision, queue commit, verified
plan promotion, arming and observed execution/completion. A committed queue is
not proof that audio executed. Failed/late model responses preserve existing
playback and plans. Selection and execution records are private JSONL in
`artifacts/live-performance/crowd-decisions.jsonl` under the submission repository.

## Session and cross-session memory — November 3

The **Memory · music & audience** panel groups observations into capture-time
episodes: playing song(s) and beat range, visible audience movement, the selected
transition and its execution status, and selected versus confirmed in-track
actions. Technical requests/decisions remain in a collapsed audit. The projection
uses saved playback samples and matching set/plan executor journals, never events
after the clip. `executing` alone can mean waiting for a cue and does not confirm
a handoff or action. Missing telemetry remains unknown; playing decks are not a
measurement of audible master output.

Only explicit gesture planning receives up to four fresh (120-second), current-set
`recentEpisodes`. Ordinary rolling replenishment receives no audience episodes;
previous sets remain local archives for review, not live preferences. Memory
cards are read-only and do not independently trigger planning or playback.

Memory is a local append-only episodic journal at
`artifacts/live-performance/memory/events-v1.jsonl`. It contains structured
observations, selected proposals, supervisor lifecycle outcomes and failures.
It is ignored by Git. No raw video is embedded in the memory journal; separate
vision artifacts retain recordings under the policy below.

The bridge samples actual deck identity, beat, play state and available controls
approximately once per second. A returned six-second observation is associated
with those samples at **capture time**, not response arrival. Missing coverage is
explicitly `incomplete_telemetry`; the bridge does not substitute the current song.
An observation can reference a prior execution event as a follow-up. This is a
temporal association, not a causal outcome score. Supervisor lifecycle records
do not prove every individual planned action ran; selected actions stay labeled
as selected. Unknown observations and failed/cancelled plans are retained.

Reasons must be English. Exact supplied memory is retained with the plan through
promotion. Queue applied, action selected, action started, and action completed
remain distinct. In-track confirmation in AI decisions comes only from matching
set/plan executor journal events for the first source song of that decision.

Start playback assigns a set-run ID. Restarting retains the journal; earlier sets
remain archived and are not supplied to live planning. Startup reads at most the
last 2 MB, and keeps at most 512 events in RAM; older journal data remains on
disk but is outside this bounded retrieval horizon. This is an inspectable episodic
archive, not a learned preference model, embedding database or automatic next-day
playlist builder. Disk failure reports a RAM-only warning without stopping music.

The UI shows **Observed → Memory → AI decision → Execution & follow-up**. Queue
and AI-plan details expose the memory supplied to their specific call. During
continuous playback, Tonight's set uses the live queue rather than the original
prepared order. `Pending`, queue applied, armed and completed are distinct states.

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
- The manual-upload panel can recover its last job across page reloads. Completed
  and failed jobs are restored from saved results after service restart, including
  their clips and raw replies. Interrupted jobs without a completed result file
  are not automatically resumed; inspect their local remote-file cleanup record.

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
region and compares coarse labels, without persistent person tracking. Recent
observations and playback telemetry can now be recalled together; beat
synchronization, calibrated audience preferences and causal attribution remain
unavailable.

## Verification — 2026-11-03 memory integration

- Offline regressions cover capture-time association across song changes,
  cross-restart retrieval, cancelled/unknown records, disk failure and partial
  journal writes, serialized planning, distant-song options, locked-prefix
  preservation, boundary commit and nonempty in-track action promotion.
- A real Muse Spark 1.3 request using existing analyzed song candidates and
  explicitly **synthetic** memory passed action validation in 15.23 seconds.
  The English reason referenced the observation and cancelled historical records,
  explicitly not treating them as successful outcomes. It selected a crossfade
  with tempo ramps and `in_track_actions: []`. This was an isolated planning test;
  it did not alter the live queue, start the camera or play audio. Request,
  response and validation are ignored local `artifacts/live-performance/memory-validation/`.
- Dashboard accessibility inspection showed the four-stage panel and live queue.
  The restarted bridge exposes memory status; the vision service loads the
  existing local key. A new real webcam + dancing + Mixxx execution rehearsal
  remains necessary. No claim of improved musical quality is established.

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

### Visible webcam plan changes

AI plan shows the latest webcam decision with observation ID/time, evidence,
reason, application state and the previous/selected next handoff. Its comparison
expands remaining song orders and per-edge transition parameters and in-track
actions. The bridge freezes the prior ready future window (or retained buffer)
before inference; absent or older snapshots are explicitly unavailable.
A proposal is not a queue commit. Completion refers only to the first handoff,
not the whole planning window.

Desktop Control room places Webcam, AI plan and Tonight's set in one row.
When the same songs actually change order, keyed rows animate to their new
positions. Ordinary polling and loading another set do not animate. Applied
webcam orders show previous/new positions. Reduced-motion users get the updated
order without movement; small screens stack the three panels.

### Explicit number 1 reorder gesture

With live playback and Adaptive DJ enabled, hold one upright index finger with
other fingers folded for at least two seconds. Spark may return `cue: reorder`
with timed evidence. This is an explicit operator convention, not a mood signal.
It is an explicit operator request; ordinary movement is record-only.
Only one request is accepted per held pose; a subsequent clear non-reorder clip
must release the latch. Unknown/conflicting visibility does not release it.

The planner excludes the unchanged order and protects both the current handoff
and the next prepared handoff. During A→B, B→C is preserved and only songs after
C can move. At least two songs after that prefix and a feasible different order
are required. If the next handoff is not prepared, the request waits while the
ordinary planner prepares it. Natural movement cannot replace a pending explicit
reorder request; unclear evidence or takeover can still invalidate it. Calls use
the shared scheduler and commit only when the protected later handoff completes.
Normal A→B promotion does not invalidate the response: queue identity, protected
B→C cue/action/deck bindings and control generation are rechecked. Changed timing,
a passed protected boundary, takeover or expired evidence cannot apply the old
response. Camera shutdown only stops new observations; accepted requests remain
subject to the same queue, cue, ownership and freshness checks. A still-fresh reorder may be replanned after advancement;
the original capture time is retained, so retries do not extend the 120-second
evidence lifetime. In-track gesture planning keeps its existing timing behavior.
Failure preserves the queue and explains why. Synthetic tests cover timed cue
validation, cooldown bypass, repeated poses, insufficient songs, candidate
exclusion and unchanged active playback. Real number-1 recognition still needs
a webcam rehearsal; the schema cannot independently verify fingers in pixels.
The November 4 later-handoff change has synthetic coverage for responses before
and after promotion, delayed commit, cue changes, expiry and cancellation. A real
audible reordered handoff still needs verification with this version.

Webcam decisions and their execution outcomes now share one chronological row
per plan. The UI shows song titles only for orders; reasons and parameters are
collapsed. The latest 20 webcam decisions available in bounded local memory
are restored across restarts, with audit updates folded into the matching row.
An old staged decision is not labeled pending without current ownership, and
an old execution-start event without a final result does not claim completion.

### Gesture-only demo verification — November 3

The simplified behavior passed 218 backend tests, 38 frontend tests, 18 vision
service tests, and the production build. New synthetic regressions cover ordinary
movement without planning, held-pose deduplication/release for all three gestures,
fixed song order for in-track requests, no-op preservation, staged cancellation,
four fresh current-set observations only, and executor-confirmed action events
isolated by set/plan ID. The restarted bridge and Chrome show the new instructions;
both decks remain stopped and the camera is off. This change has not yet had a
new real Meta planning call or live gesture-to-audible-action rehearsal. Existing
recorded outcomes are retained and are not relabeled as new-policy successes.

### Request visibility and stopping observation — November 4

The performance queue shows receipt/planning as soon as the bridge accepts a
number-1 request. Once Meta selects a changed order, a separate **Proposed order ·
not yet applied** list shows the protected prefix and pending song positions.
The actual queue remains unchanged until the protected handoff commits. Only
a matching committed order is labeled **New order applied**; this does not claim
that the next audible handoff has already completed. Vision capture/inference
latency still occurs before the bridge can accept a gesture.

Stopping observation, hiding the browser tab or losing its heartbeat preserves
accepted queued/in-flight/staged work without extending evidence freshness.
Restarting a camera preserves pending work, while stale camera sessions cannot
stop the new owner. Takeover, stale evidence, queue/cue changes and explicit
adaptive cancellation retain their normal rejection behavior.

Synthetic verification: 328 backend tests and 40 frontend tests pass, including
stopping observation at each request stage, lease expiry, camera restart, stale
owners, takeover, evidence expiry and changed queues. A new real webcam-to-audible
reorder rehearsal remains necessary for this version.

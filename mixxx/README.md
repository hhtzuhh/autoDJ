# Mixxx mapping setup

Tested with Mixxx 2.5.6 on macOS. Both mapping files are required; they implement
protocol 2 for precise position feedback, sampler staging and tempo control.

1. Open macOS **Audio MIDI Setup → Window → Show MIDI Studio → IAC Driver**.
   Enable **Device is online** and create a bus named `Bus 1`.
2. In Mixxx, open **Preferences → Controllers → IAC Driver Bus 1 → Open User
   Mapping Folder**. Copy `AI-DJ-Probe.midi.xml` and `AI-DJ-Probe.scripts.js` from
   this folder into the user mapping folder.
3. Reopen Preferences, select **AI DJ programmatic control probe**, enable it
   and apply. Reload the mapping whenever either file changes.
4. From the repository root, run `.venv/bin/python backend/probe.py ports`.
   If the port differs, set `MIXXX_MIDI_PORT` to its exact name in `.env`.
5. Start `npm run bridge`. Import the dashboard's exported `.m3u8` playlist into
   Mixxx, select its first row, and leave both decks stopped before building the
   performance plan. The bridge stages tracks into sampler slots and verifies
   loaded identities from observed metadata before playback.

The observer identifies tracks by sample count, sample rate and file BPM against
the selected analysis catalog. Ambiguous or unknown identities block planning;
the mapping cannot transmit arbitrary file paths.

The read-only CLI observer can be run with `.venv/bin/python backend/probe.py
observer` (export `MIXXX_MIDI_PORT` in the shell for direct CLI use). Run one
bridge at a time: concurrent automation processes must not control the same bus.
Human takeover and cancel preserve current source playback and stop later
automation writes. A seek is safe only while waiting before the first automatic
action and while both its old and new positions precede that action.

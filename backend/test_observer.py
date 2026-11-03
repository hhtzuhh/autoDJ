import unittest
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mido

import observer


def record(
    sha: str = "a" * 64,
    *,
    duration: float = 120.0,
    sample_rate: int = 44_100,
    bpm: float = 130.0,
) -> observer.TrackRecord:
    return observer.TrackRecord(
        audio_sha256=sha,
        analysis_cache_key=f"cache-{sha[:4]}",
        source_filename=f"{sha[:4]}.wav",
        duration_seconds=duration,
        sample_rate=sample_rate,
        file_bpm=bpm,
        beats_seconds=tuple(index * 60 / bpm for index in range(260)),
    )


def split(value: int, parts: int) -> list[int]:
    return [(value >> (7 * index)) & 0x7F for index in range(parts)]


class TrackCatalogTest(unittest.TestCase):
    def test_unique_numeric_fingerprint_resolves(self) -> None:
        target = record()
        other = record("b" * 64, duration=150.0, bpm=135.0)
        catalog = observer.TrackCatalog([target, other])

        match, candidates = catalog.resolve(
            track_samples=target.expected_engine_samples,
            sample_rate=44_100,
            file_bpm=130.0,
        )

        self.assertEqual(match, target)
        self.assertEqual(candidates, [target])

    def test_ambiguous_fingerprint_never_guesses(self) -> None:
        first = record()
        second = record("b" * 64)
        match, candidates = observer.TrackCatalog([first, second]).resolve(
            track_samples=first.expected_engine_samples,
            sample_rate=44_100,
            file_bpm=130.0,
        )
        self.assertIsNone(match)
        self.assertEqual(len(candidates), 2)

    def test_unique_exact_samples_win_during_transient_stale_bpm(self) -> None:
        low_key = record("a" * 64, duration=183.3044, bpm=146.0)
        previous_track = record("b" * 64, duration=182.91255, bpm=100.0)
        catalog = observer.TrackCatalog([low_key, previous_track])

        match, candidates = catalog.resolve(
            track_samples=low_key.expected_engine_samples - 280,
            sample_rate=44_100,
            file_bpm=98.667,
        )

        self.assertEqual(match, low_key)
        self.assertEqual(candidates, [low_key])

    def test_repeated_unresolved_fingerprint_does_not_create_revisions(self) -> None:
        reducer = observer.ObserverReducer(observer.TrackCatalog([record()]))
        first = reducer.apply_fingerprint(
            1,
            track_samples=15_162_624,
            sample_rate=44_100,
            file_bpm=140.0,
            now=0.0,
        )
        repeated = reducer.apply_fingerprint(
            1,
            track_samples=15_162_624,
            sample_rate=44_100,
            file_bpm=140.0,
            now=1.0,
        )

        self.assertEqual(first[0].event, "track_identity_snapshot")
        self.assertEqual(first[0].details["identity_status"], "unresolved")
        self.assertEqual(repeated, [])
        self.assertEqual(reducer.state.revision, 0)


class ObserverReducerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.track = record()
        self.reducer = observer.ObserverReducer(observer.TrackCatalog([self.track]))
        self.reducer.apply_track_loaded(1, True, now=0.0)
        events = self.reducer.apply_fingerprint(
            1,
            track_samples=self.track.expected_engine_samples,
            sample_rate=self.track.sample_rate,
            file_bpm=self.track.file_bpm,
            now=0.0,
        )
        self.assertEqual(events[0].event, "track_identity_snapshot")
        self.assertEqual(events[0].revision, 0)

    def test_normal_playback_does_not_increment_revision(self) -> None:
        self.reducer.apply_control("deck:1", "play", True, now=0.0)
        self.reducer.apply_control("deck:1", "position", 0.10, now=0.0)

        events = self.reducer.apply_control("deck:1", "position", 0.11, now=1.2)

        self.assertEqual(events, [])
        self.assertEqual(self.reducer.state.revision, 0)

    def test_seek_emits_position_jump(self) -> None:
        self.reducer.apply_control("deck:1", "play", True, now=0.0)
        self.reducer.apply_control("deck:1", "position", 0.10, now=0.0)

        events = self.reducer.apply_control("deck:1", "position", 0.70, now=1.0)

        self.assertEqual(events[0].event, "position_jump")
        self.assertEqual(events[0].origin, "human_or_external")
        self.assertEqual(events[0].revision, 1)

    def test_expected_mixer_change_is_automation(self) -> None:
        self.reducer.apply_control("master", "crossfader", 0.5, now=0.0)
        self.reducer.expect("master", "crossfader", 0.75, now=1.0)

        events = self.reducer.apply_control("master", "crossfader", 0.75, now=1.1)

        self.assertEqual(events[0].origin, "automation")
        self.assertEqual(events[0].event, "mixer_gesture_started")

    def test_crossfader_gesture_increments_revision_once(self) -> None:
        self.reducer.apply_control("master", "crossfader", 0.5, now=0.0)

        first = self.reducer.apply_control("master", "crossfader", 0.45, now=1.0)
        middle = self.reducer.apply_control("master", "crossfader", 0.30, now=1.1)
        end = self.reducer.apply_control("master", "crossfader", 0.10, now=1.2)

        self.assertEqual(len(first), 1)
        self.assertEqual(middle, [])
        self.assertEqual(end, [])
        self.assertEqual(self.reducer.state.revision, 1)

    def test_human_crossfader_override_during_automation_fade_emits(self) -> None:
        self.reducer.apply_control("master", "crossfader", 0.5, now=0.0)
        self.reducer.expect("master", "crossfader", 0.0, now=1.0)
        automation = self.reducer.apply_control(
            "master", "crossfader", 0.0, now=1.1
        )

        override = self.reducer.apply_control(
            "master", "crossfader", 0.7, now=1.2
        )

        self.assertEqual(automation[0].origin, "automation")
        self.assertEqual(override[0].event, "mixer_changed")
        self.assertEqual(override[0].origin, "human_or_external")

    def test_sync_transaction_matches_transient_and_final_modes(self) -> None:
        self.reducer.apply_control("deck:2", "sync_mode", 1, now=0.0)
        self.reducer.expect("deck:2", "sync_mode", 2, now=1.0)
        self.reducer.expect("deck:2", "sync_mode", 0, now=1.0)

        transient = self.reducer.apply_control(
            "deck:2", "sync_mode", 2, now=1.1
        )
        final = self.reducer.apply_control("deck:2", "sync_mode", 0, now=1.2)

        self.assertEqual(transient[0].origin, "automation")
        self.assertEqual(final[0].origin, "automation")

    def test_eq_command_matches_arbitrary_transient_and_settled_value(self) -> None:
        self.reducer.apply_control("deck:1", "eq_low", 0.5, now=0.0)
        self.reducer.expect("deck:1", "eq_low", None, now=1.0, ttl=0.25)
        self.reducer.expect("deck:1", "eq_low", 0.45, now=1.0, ttl=2.0)

        transient = self.reducer.apply_control(
            "deck:1", "eq_low", 0.89, now=1.05
        )
        settled = self.reducer.apply_control(
            "deck:1", "eq_low", 0.45, now=1.9
        )

        self.assertEqual(transient[0].origin, "automation")
        self.assertEqual(settled[0].origin, "automation")

    def test_pause_time_does_not_become_false_seek(self) -> None:
        self.reducer.apply_control("deck:1", "play", True, now=0.0)
        self.reducer.apply_control("deck:1", "position", 0.10, now=0.0)
        self.reducer.apply_control("deck:1", "play", False, now=2.0)
        self.reducer.apply_control("deck:1", "play", True, now=20.0)

        events = self.reducer.apply_control("deck:1", "position", 0.11, now=21.0)

        self.assertEqual(events, [])

    def test_seek_while_paused_emits_position_jump(self) -> None:
        self.reducer.apply_control("deck:1", "play", False, now=0.0)
        self.reducer.apply_control("deck:1", "position", 0.10, now=0.0)

        events = self.reducer.apply_control("deck:1", "position", 0.60, now=2.0)

        self.assertEqual(events[0].event, "position_jump")
        self.assertEqual(events[0].origin, "human_or_external")

    def test_unmatched_loop_change_is_human_or_external(self) -> None:
        self.reducer.apply_control("deck:1", "loop_enabled", False, now=0.0)

        events = self.reducer.apply_control(
            "deck:1", "loop_enabled", True, now=2.0
        )

        self.assertEqual(events[0].origin, "human_or_external")
        self.assertEqual(events[0].event, "loop_changed")

    def test_channel_shaping_controls_have_distinct_safe_event(self) -> None:
        for control in ("volume", "eq_low", "filter"):
            with self.subTest(control=control):
                reducer = observer.ObserverReducer(
                    observer.TrackCatalog([self.track])
                )
                reducer.apply_control("deck:1", control, 0.5, now=0.0)

                events = reducer.apply_control(
                    "deck:1", control, 0.7, now=1.0
                )

                self.assertEqual(events[0].event, "channel_control_changed")
                self.assertEqual(events[0].origin, "human_or_external")
                self.assertEqual(events[0].details["control"], control)


class MidiObserverDecoderTest(unittest.TestCase):
    def test_channel_shaping_ccs_decode(self) -> None:
        decoder = observer.MidiObserverDecoder()
        for cc, name in (
            (observer.EQ_LOW_CC, "eq_low"),
            (observer.FILTER_CC, "filter"),
        ):
            with self.subTest(control=name):
                payloads = decoder.feed(
                    mido.Message(
                        "control_change", channel=1, control=cc, value=64
                    )
                )
                self.assertEqual(payloads[0]["target"], "deck:2")
                self.assertEqual(payloads[0]["control"], name)
                self.assertAlmostEqual(payloads[0]["value"], 64 / 127)

    def test_complete_fingerprint_is_reassembled(self) -> None:
        decoder = observer.MidiObserverDecoder()
        values = (
            (observer.TRACK_SAMPLES_PART_CC, split(10_584_000, 5)),
            (observer.SAMPLE_RATE_PART_CC, split(44_100, 3)),
            (observer.FILE_BPM_PART_CC, split(130_000, 3)),
        )
        for controls, parts in values:
            for control, value in zip(controls, parts, strict=True):
                self.assertEqual(
                    decoder.feed(
                        mido.Message(
                            "control_change", channel=0, control=control, value=value
                        )
                    ),
                    [],
                )

        payloads = decoder.feed(
            mido.Message(
                "control_change",
                channel=0,
                control=observer.TRACK_FINGERPRINT_COMPLETE_CC,
                value=1,
            )
        )

        self.assertEqual(payloads[0]["track_samples"], 10_584_000)
        self.assertEqual(payloads[0]["sample_rate"], 44_100)
        self.assertEqual(payloads[0]["file_bpm"], 130.0)

    def test_precise_position_is_reassembled(self) -> None:
        decoder = observer.MidiObserverDecoder()
        for control, value in zip(
            observer.PRECISE_POSITION_PART_CC,
            split(2_646_000, 5),
            strict=True,
        ):
            decoder.feed(
                mido.Message(
                    "control_change", channel=0, control=control, value=value
                )
            )

        payloads = decoder.feed(
            mido.Message(
                "control_change",
                channel=0,
                control=observer.PRECISE_POSITION_STATUS_CC,
                value=1,
            )
        )

        self.assertEqual(payloads[0]["kind"], "precise_position")
        self.assertEqual(payloads[0]["engine_samples"], 2_646_000)


class ObserverUpdatePublisherTest(unittest.TestCase):
    def test_snapshot_burst_does_not_expire_tempo_intent_behind_slow_ui(self):
        # Model the failing pipeline: a position reader sends full snapshots;
        # each dashboard projection costs 4 ms. Reducing 800 unchanged fields
        # must not spend 3.2 s projecting UI before reading the tempo callback.
        now = [100.0]
        calls = []
        reducer = observer.ObserverReducer(observer.TrackCatalog([]))
        reducer.apply_control('deck:2', 'rate', 0.0, now=now[0])
        reducer.expect('deck:2', 'rate', None, now=now[0], ttl=.25)
        def project(state, events):
            calls.append(events)
            now[0] += .004
        publisher = observer.ObserverUpdatePublisher(project)
        with patch('observer.time.monotonic', side_effect=lambda: now[0]):
            for _ in range(800):
                now[0] += .0001
                publisher.offer(reducer.state, (), now[0])
            events = tuple(reducer.apply_control('deck:2', 'rate', -.5275590551181102, now=now[0]))
            publisher.offer(reducer.state, events, now[0])
        self.assertEqual(events[0].origin, 'automation')
        self.assertLess(len(calls), 5)
        self.assertEqual(calls[-1], events)
        # A genuinely uncommanded change is still human, and is not throttled.
        human = tuple(reducer.apply_control('deck:2', 'rate', -.1, now=now[0]))
        with patch('observer.time.monotonic', side_effect=lambda: now[0]):
            publisher.offer(reducer.state, human, now[0])
        self.assertEqual(human[0].origin, 'human_or_external')
        self.assertEqual(calls[-1], human)

    def test_pending_latest_telemetry_flushes_when_stream_goes_idle(self):
        callback = Mock()
        publisher = observer.ObserverUpdatePublisher(callback)
        with patch('observer.time.monotonic', return_value=1):
            publisher.offer('initial', (), 1)
        publisher.offer('intermediate', (), 1.01)
        publisher.offer('latest', (), 1.02)
        publisher.flush(1.04)
        callback.assert_called_once_with('initial', ())
        with patch('observer.time.monotonic', return_value=1.06):
            publisher.flush(1.06)
        self.assertEqual(callback.call_count, 2)
        callback.assert_called_with('latest', ())


class CommandLogReaderTest(unittest.TestCase):
    def test_consumer_delay_does_not_expire_feedback_received_within_command_ttl(self):
        port = Mock()
        port.__enter__ = Mock(return_value=port)
        port.__exit__ = Mock(return_value=False)
        port.poll.side_effect = [
            (mido.Message('control_change', channel=0, control=25, value=64), 10.0),
            (mido.Message('control_change', channel=0, control=25, value=55), 10.1),
            (mido.Message('control_change', channel=0, control=25, value=100), 11.4),
            StopIteration(),
        ]
        reader = Mock()
        reader.read.side_effect = [[observer.ExpectedChange('master', 'crossfader', 55/127, 10.8, issued_at=10.05)]] + [[]] * 10
        events = []
        with patch.object(observer, 'TimestampedMidiInput', return_value=port), patch.object(observer, 'CommandLogReader', return_value=reader), patch.object(observer.TrackCatalog, 'from_analysis_root', return_value=observer.TrackCatalog([])), patch.object(observer.mido, 'open_output'), patch.object(observer, 'request_snapshot'), patch.object(observer.time, 'monotonic', return_value=11.5), patch('builtins.print'):
            with self.assertRaises(StopIteration):
                observer.run_observer(port_name='synthetic', analysis_root=Path('.'), command_log=Path('synthetic'),
                    on_update=lambda state, changes: events.extend(changes))
        self.assertEqual([e.origin for e in events], ['automation', 'human_or_external'])
        self.assertEqual(events[0].observed_at, 10.1)

    def test_future_command_cannot_claim_earlier_manual_feedback(self):
        reducer = observer.ObserverReducer(observer.TrackCatalog([]))
        reducer.apply_control('master', 'crossfader', .5, now=10)
        reducer.expect('master', 'crossfader', .7, now=11)
        events = reducer.apply_control('master', 'crossfader', .7, now=10.5)
        self.assertEqual(events[0].origin, 'human_or_external')

    def test_tails_only_new_automation_intents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "commands.jsonl"
            path.write_text('{"old":true}\n', encoding="utf-8")
            reader = observer.CommandLogReader(path)
            payload = {
                "target": "master",
                "control": "crossfader",
                "value": 0.75,
                "expires_at": 20.0,
                "tolerance": 0.02,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")

            changes = reader.read()

            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0].target, "master")
            self.assertEqual(changes[0].control, "crossfader")
            self.assertEqual(changes[0].value, 0.75)


if __name__ == "__main__":
    unittest.main()

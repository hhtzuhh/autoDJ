import unittest
from unittest import mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe
import observer


class PreciseSeekProtocolTest(unittest.TestCase):
    def test_routine_ratio_encoding_is_precise_bounded_and_attributed(self):
        for ratio in (.8, 130 / 140, 1, 130 / 120, 1.2):
            output = mock.Mock()
            with mock.patch.object(probe, "record_expected_change") as record:
                probe.send_tempo_ratio(output, 2, ratio)
            messages = [call.args[0] for call in output.send.call_args_list]
            self.assertEqual(messages[0].value, 35)
            self.assertTrue(all(m.control == 30 and m.channel == 0 for m in messages))
            parts = [m.value - 64 for m in messages[1:]]
            self.assertAlmostEqual((parts[0] + parts[1]*63 + parts[2]*3969)/100000, ratio, places=4)
            record.assert_called_once_with("deck:2", "rate", None, ttl=.25)
        for ratio in (.7, 1.3, float('nan')):
            with self.assertRaises(ValueError): probe.send_tempo_ratio(mock.Mock(), 1, ratio)

    def test_midi_integer_round_trip(self) -> None:
        for value in (0, 1, 127, 128, 8_820_000, 128**5 - 1):
            self.assertEqual(probe.join_midi_integer(probe.split_midi_integer(value)), value)

    def test_seconds_use_stereo_engine_samples(self) -> None:
        self.assertEqual(probe.seconds_to_engine_samples(30.0, 44_100), 2_646_000)

    def test_invalid_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            probe.split_midi_integer(-1)
        with self.assertRaises(ValueError):
            probe.seconds_to_engine_samples(-0.1, 44_100)
        with self.assertRaises(ValueError):
            probe.seconds_to_engine_samples(1.0, 0)

    def test_precise_seek_attributes_its_transport_side_effect(self) -> None:
        class Port:
            def __init__(self, messages=None) -> None:
                self.messages = list(messages or [])

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def send(self, message) -> None:
                return None

            def poll(self):
                return self.messages.pop(0) if self.messages else None

        samples = probe.seconds_to_engine_samples(1.0, 44_100)
        feedback = [
            probe.mido.Message(
                "control_change", channel=0, control=control, value=value
            )
            for control, value in zip(
                probe.PRECISE_POSITION_PART_CC,
                probe.split_midi_integer(samples),
                strict=True,
            )
        ]
        feedback.append(
            probe.mido.Message(
                "control_change",
                channel=0,
                control=probe.PRECISE_POSITION_STATUS_CC,
                value=1,
            )
        )
        with (
            mock.patch.object(probe, "feedback_input", return_value=Port(feedback)),
            mock.patch.object(probe.mido, "open_output", return_value=Port()),
            mock.patch.object(probe, "record_expected_change") as record,
        ):
            probe.precise_seek(1, 1.0, 44_100, False)

        self.assertEqual(record.call_count, 4)
        self.assertEqual(
            [call.args for call in record.call_args_list[1:]],
            [
                ("deck:1", "sync_mode", None),
                ("deck:2", "sync_mode", None),
                ("deck:1", "play", False),
            ],
        )
        self.assertEqual(
            record.call_args_list[-1].args,
            ("deck:1", "play", False),
        )

    def test_sync_message_targets_requested_deck(self) -> None:
        class Output:
            def __init__(self) -> None:
                self.messages = []

            def send(self, message) -> None:
                self.messages.append(message)

        output = Output()
        with mock.patch.object(probe, "record_expected_change") as record:
            probe.send_sync_value(output, 2, "enabled", True)
        message = output.messages[0]
        self.assertEqual(message.channel, 0)
        self.assertEqual(message.control, probe.SYNC_COMMAND_CC)
        self.assertEqual(message.value, 22)
        self.assertEqual(record.call_count, 2)
        self.assertEqual(record.call_args_list[1].args, ("deck:2", "rate", None))

    def test_sync_disable_attributes_transient_leader_then_off(self) -> None:
        class Output:
            def send(self, message) -> None:
                return None

        with mock.patch.object(probe, "record_expected_change") as record:
            probe.send_sync_value(Output(), 2, "enabled", False)

        self.assertEqual(record.call_count, 3)
        self.assertEqual(
            [call.args for call in record.call_args_list],
            [
                ("deck:2", "sync_mode", 2),
                ("deck:2", "sync_mode", 0),
                ("deck:2", "rate", None),
            ],
        )

    def test_stopping_playback_attributes_sync_role_side_effects(self) -> None:
        class Output:
            def send(self, message) -> None:
                return None

        with mock.patch.object(probe, "record_expected_change") as record:
            probe.send_value(Output(), "1", "play", 0.0)

        self.assertEqual(
            [call.args for call in record.call_args_list],
            [
                ("deck:1", "sync_mode", None),
                ("deck:2", "sync_mode", None),
                ("deck:1", "play", 0.0),
            ],
        )

    def test_crossfader_endpoint_attributes_role_swap_before_stop(self) -> None:
        # Replay the real failure: fade endpoint, both sync callbacks, then
        # stop. No stop expectation exists yet when these callbacks arrive.
        for endpoint, incoming, outgoing in ((0.0, 1, 2), (1.0, 2, 1)):
            with self.subTest(endpoint=endpoint):
                reducer = observer.ObserverReducer(observer.TrackCatalog([]))
                reducer.apply_control(f'deck:{incoming}', 'sync_mode', 1, now=0)
                reducer.apply_control(f'deck:{outgoing}', 'sync_mode', 2, now=0)
                events = []
                def record(target, control, value, **kwargs):
                    reducer.expect(target, control, value, now=1.0, **kwargs)
                class Output:
                    def send(self, message):
                        events.extend(reducer.apply_control(f'deck:{outgoing}', 'sync_mode', 1, now=1.013))
                        events.extend(reducer.apply_control(f'deck:{incoming}', 'sync_mode', 2, now=1.015))
                with mock.patch.object(probe, 'record_expected_change', side_effect=record):
                    probe.send_value(Output(), 'master', 'crossfader', endpoint)
                self.assertEqual([event.origin for event in events], ['automation', 'automation'])
                human = reducer.apply_control(f'deck:{incoming}', 'sync_mode', 0, now=1.02)
                self.assertEqual(human[0].origin, 'human_or_external')

    def test_crossfader_expectations_do_not_hide_sync_off_or_late_changes(self) -> None:
        for value, now in ((0, 1.01), (1, 2.0)):
            reducer = observer.ObserverReducer(observer.TrackCatalog([]))
            reducer.apply_control('deck:2', 'sync_mode', 2, now=0)
            def record(target, control, expected, **kwargs):
                reducer.expect(target, control, expected, now=1.0, **kwargs)
            with mock.patch.object(probe, 'record_expected_change', side_effect=record):
                probe.send_value(mock.Mock(), 'master', 'crossfader', 0.0)
            event = reducer.apply_control('deck:2', 'sync_mode', value, now=now)[0]
            self.assertEqual(event.origin, 'human_or_external')

    def test_mid_fade_does_not_blanket_ignore_sync_changes(self) -> None:
        with mock.patch.object(probe, 'record_expected_change') as record:
            probe.send_value(mock.Mock(), 'master', 'crossfader', .5)
        self.assertEqual([call.args for call in record.call_args_list],
                         [('master', 'crossfader', .5)])

    def test_bass_message_uses_reliable_command_channel(self) -> None:
        class Output:
            def __init__(self) -> None:
                self.messages = []

            def send(self, message) -> None:
                self.messages.append(message)

        output = Output()
        with mock.patch.object(probe, "record_expected_change") as record:
            probe.send_bass_value(output, 2, 0.0)
        self.assertEqual(
            [call.args for call in record.call_args_list],
            [("deck:2", "eq_low", None), ("deck:2", "eq_low", 0.0)],
        )
        self.assertEqual(record.call_args_list[0].kwargs, {"ttl": 0.25})
        self.assertEqual(record.call_args_list[1].kwargs, {"ttl": 2.0})
        target, value = output.messages
        self.assertEqual(target.channel, 0)
        self.assertEqual(target.control, probe.BASS_COMMAND_CC)
        self.assertEqual(target.value, 41)
        self.assertEqual(value.control, probe.BASS_COMMAND_CC)
        self.assertEqual(value.value, probe.BASS_VALUE_OFFSET)

        output = Output()
        probe.send_bass_value(output, 1, 0.5)
        self.assertEqual(output.messages[0].value, 40)
        self.assertEqual(output.messages[1].value, 95)

    def test_sampler_registry_commands_encode_exact_slot_and_deck(self) -> None:
        class Output:
            def __init__(self) -> None:
                self.messages = []

            def send(self, message) -> None:
                self.messages.append(message)

        output = Output()
        with mock.patch.object(probe, "record_expected_change") as record:
            probe.send_stage_sampler_value(output, 7)
            probe.send_load_sampler_value(output, 2, 7)

        self.assertEqual(
            [(message.control, message.value) for message in output.messages],
            [(probe.RECORDING_COMMAND_CC, 30), (probe.RECORDING_COMMAND_CC, 58)],
        )
        self.assertEqual(record.call_count, 4)
        with self.assertRaises(ValueError):
            probe.send_stage_sampler_value(output, 11)
        with self.assertRaises(ValueError):
            probe.send_load_sampler_value(output, 3, 1)

    def test_deck_status_decodes_precise_bpms(self) -> None:
        messages = [
            probe.mido.Message(
                "control_change",
                control=probe.DECK_STATUS_SYNC_MODE_CC,
                value=1,
            )
        ]
        for control, value in zip(
            probe.DECK_STATUS_BPM_PART_CC,
            probe.split_midi_integer(126_048, 3),
            strict=True,
        ):
            messages.append(
                probe.mido.Message("control_change", control=control, value=value)
            )
        for control, value in zip(
            probe.DECK_STATUS_FILE_BPM_PART_CC,
            probe.split_midi_integer(129_931, 3),
            strict=True,
        ):
            messages.append(
                probe.mido.Message("control_change", control=control, value=value)
            )
        messages.append(
            probe.mido.Message(
                "control_change",
                control=probe.DECK_STATUS_COMPLETE_CC,
                value=1,
            )
        )

        status = probe.decode_deck_status(messages)

        self.assertEqual(status["sync_mode"], 1)
        self.assertEqual(status["bpm"], 126.048)
        self.assertEqual(status["file_bpm"], 129.931)

    def test_deck_status_reuses_existing_output_port(self) -> None:
        class Port:
            def __init__(self, messages=None) -> None:
                self.messages = list(messages or [])
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                return None

            def send(self, message) -> None:
                self.sent.append(message)

            def poll(self):
                return self.messages.pop(0) if self.messages else None

        messages = [
            probe.mido.Message(
                "control_change",
                channel=0,
                control=probe.DECK_STATUS_SYNC_MODE_CC,
                value=0,
            )
        ]
        for controls, value in (
            (probe.DECK_STATUS_BPM_PART_CC, 126_000),
            (probe.DECK_STATUS_FILE_BPM_PART_CC, 126_000),
        ):
            messages.extend(
                probe.mido.Message(
                    "control_change", channel=0, control=control, value=part
                )
                for control, part in zip(
                    controls, probe.split_midi_integer(value, 3), strict=True
                )
            )
        messages.append(
            probe.mido.Message(
                "control_change",
                channel=0,
                control=probe.DECK_STATUS_COMPLETE_CC,
                value=1,
            )
        )
        output = Port()
        with (
            mock.patch.object(probe, "feedback_input", return_value=Port(messages)),
            mock.patch.object(probe.mido, "open_output") as open_output,
        ):
            status = probe.deck_status(1, output=output)

        open_output.assert_not_called()
        self.assertEqual(status["bpm"], 126.0)
        self.assertEqual(output.sent[-1].control, probe.DECK_STATUS_COMMAND_CC)


if __name__ == "__main__":
    unittest.main()

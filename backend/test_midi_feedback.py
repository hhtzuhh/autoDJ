import unittest
from queue import Queue, Empty
import threading
from unittest.mock import Mock, patch

import midi_feedback as feedback


class MidiFeedbackTest(unittest.TestCase):
    def test_receiver_keeps_arrival_time_while_consumer_is_delayed(self):
        incoming = Queue()
        closed = threading.Event()
        class Input:
            def __enter__(self): return self
            def __exit__(self, *_): closed.set()
            def poll(self):
                try: return incoming.get_nowait()
                except Empty: return None
        clock = [100.0]
        with patch.object(feedback, 'PollingMidiInput', return_value=Input()), patch.object(feedback.time, 'monotonic', side_effect=lambda: clock[0]):
            with feedback.TimestampedMidiInput('synthetic') as port:
                incoming.put('crossfader')
                item = port.messages.get(timeout=1)
                port.messages.put(item)
                clock[0] = 101.36  # dashboard/consumer resumes after the intent's TTL
                self.assertEqual(port.poll(), ('crossfader', 100.0))
                incoming.put('human pause')
                self.assertEqual(port.messages.get(timeout=1), ('human pause', 101.36))
        self.assertTrue(closed.is_set())
        self.assertFalse(port.thread.is_alive())

    def device(self):
        device = Mock()
        device.get_ports.return_value = ["test"]
        device.get_message.return_value = None
        return device

    def test_native_polling_never_registers_python_callback_and_closes_once(self):
        device = self.device()
        device.get_message.side_effect = [([0xB1, 44, 1], 0), None]
        with patch.object(feedback.rtmidi, "MidiIn", return_value=device):
            with feedback.PollingMidiInput("test") as port:
                message = port.poll()
                self.assertEqual((message.channel,message.control,message.value),(1,44,1))
                self.assertIsNone(port.poll())
            port.close()
        device.set_callback.assert_not_called()
        device.close_port.assert_called_once()
        device.delete.assert_called_once()

    def test_many_requests_reuse_one_input_and_discard_stale_replies(self):
        device = self.device()
        with patch.object(feedback.rtmidi,"MidiIn",return_value=device) as create:
            with feedback.feedback_session("test") as session:
                for _ in range(100):
                    device.get_message.side_effect = [([0xB0,44,1],0),None,([0xB1,44,1],0)]
                    with feedback.feedback_input("test") as port:
                        self.assertIs(port,session)
                        self.assertEqual(port.poll().channel,1)
                    device.close_port.assert_not_called()
            create.assert_called_once()
        device.close_port.assert_called_once()

    def test_exception_clears_session_and_releases_input(self):
        device = self.device()
        with patch.object(feedback.rtmidi,"MidiIn",return_value=device):
            with self.assertRaisesRegex(RuntimeError,"cancel"):
                with feedback.feedback_session("test"):
                    raise RuntimeError("cancel")
            self.assertIsNone(feedback._session.get())
        device.close_port.assert_called_once()

    def test_missing_port_releases_native_client(self):
        device = self.device()
        with patch.object(feedback.rtmidi,"MidiIn",return_value=device):
            with self.assertRaises(OSError):
                feedback.PollingMidiInput("missing")
        device.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()

"""Callback-free MIDI feedback, with one input per performance routine.

On macOS a python-rtmidi callback can wait for the GIL while close_port holds
it waiting for CoreMIDI delivery to finish. Native queued polling needs no
Python callback, including at shutdown.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from queue import Queue, Empty, Full
import threading
import time

import mido
import rtmidi


class PollingMidiInput:
    def __init__(self, name):
        self.name = name
        self._rt = rtmidi.MidiIn(queue_size_limit=8192)
        try:
            names = self._rt.get_ports()
            if name not in names:
                raise OSError(f"unknown MIDI input port {name!r}")
            self._rt.open_port(names.index(name))
            self._rt.ignore_types(sysex=False, timing=False, active_sense=True)
        except BaseException:
            self.close()
            raise

    def poll(self):
        if self._rt is None:
            return None
        packet = self._rt.get_message()
        return mido.Message.from_bytes(packet[0]) if packet is not None else None

    def iter_pending(self):
        while (message := self.poll()) is not None:
            yield message

    def close(self):
        if self._rt is not None:
            rt, self._rt = self._rt, None
            try:
                rt.close_port()
            finally:
                rt.delete()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class TimestampedMidiInput:
    """Receive independently of dashboard work; timestamp before consumer delay.

    The reader owns its native port, including close. No native Python callback
    or guessed grace period is used for automation attribution.
    """
    def __init__(self, name):
        self.name = name
        self.messages = Queue(maxsize=8192)
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._receive, name='idj-midi-receiver', daemon=True)

    def _receive(self):
        try:
            with PollingMidiInput(self.name) as port:
                self.ready.set()
                while not self.stopped.is_set():
                    message = port.poll()
                    if message is None:
                        self.stopped.wait(.001)
                        continue
                    self.messages.put_nowait((message, time.monotonic()))
        except Full:
            self.error = RuntimeError('Observer MIDI queue overflow; feedback is no longer reliable.')
        except Exception as error:
            self.error = error
        finally:
            self.ready.set()

    def poll(self):
        if self.error:
            raise self.error
        try:
            return self.messages.get_nowait()
        except Empty:
            return None

    def __enter__(self):
        self.thread.start()
        self.ready.wait()
        if self.error:
            self.close()
            raise self.error
        return self

    def close(self):
        self.stopped.set()
        self.thread.join()

    def __exit__(self, *_):
        self.close()


_session = ContextVar("midi_feedback_session", default=None)


@contextmanager
def feedback_session(name):
    """Keep native feedback open across seek, status and position requests."""
    with PollingMidiInput(name) as port:
        token = _session.set(port)
        try:
            yield port
        finally:
            _session.reset(token)


@contextmanager
def feedback_input(name):
    port = _session.get()
    if port is not None and port.name == name:
        # Every caller sends a new request after entry. Discard prior replies.
        for _ in port.iter_pending():
            pass
        yield port
    else:
        with PollingMidiInput(name) as owned:
            yield owned

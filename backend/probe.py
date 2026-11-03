#!/usr/bin/env python3
"""Small CLI for exercising the AI DJ Mixxx MIDI mapping."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

import mido
from midi_feedback import feedback_input, feedback_session


DEFAULT_PORT = "IAC Driver Bus 1"
from runtime_paths import RUNTIME_ROOT, ANALYSIS_ROOT as DEFAULT_ANALYSIS_ROOT
CANCEL_FILE = RUNTIME_ROOT / "cancel"
COMMAND_LOG = RUNTIME_ROOT / "commands.jsonl"

INPUT_CC = {
    "play": 20,
    "position": 21,
    "rate": 22,
    "volume": 23,
}
MASTER_CC = {"crossfader": 24}
LIBRARY_CC = {"next": 30, "previous": 29}
LOAD_CC = {1: 31, 2: 32}
OUTPUT_CC = {
    16: "play",
    17: "position",
    18: "rate",
    19: "volume",
    25: "crossfader",
    26: "track_loaded",
}
PRECISE_SEEK_PART_CC = (33, 34, 35, 36, 37)
PRECISE_SEEK_COMMIT_CC = 38
PRECISE_POSITION_PART_CC = (39, 40, 41, 42, 43)
PRECISE_POSITION_STATUS_CC = 44
RECORDING_COMMAND_CC = 30
RECORDING_STATUS_CC = 46
SYNC_COMMAND_CC = 30
BASS_COMMAND_CC = 30
BASS_VALUE_OFFSET = 64
BASS_VALUE_STEPS = 62
DECK_STATUS_COMMAND_CC = 30
DECK_STATUS_SYNC_MODE_CC = 51
DECK_STATUS_BPM_PART_CC = (52, 53, 54)
DECK_STATUS_FILE_BPM_PART_CC = (55, 56, 57)
DECK_STATUS_COMPLETE_CC = 58
MAX_QUEUE_SAMPLER_SLOTS = 10
PRECISE_SEEK_STATUSES = {
    1: "ok",
    2: "incomplete_position",
    3: "no_track_loaded",
    4: "position_out_of_range",
}


def port_name() -> str:
    return os.environ.get("MIXXX_MIDI_PORT", DEFAULT_PORT)


def encode(name: str, value: float) -> int:
    if name == "rate":
        if not -1.0 <= value <= 1.0:
            raise ValueError("rate must be between -1.0 and 1.0")
        value = (value + 1.0) / 2.0
    elif not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")
    return round(value * 127)


def decode(name: str, midi_value: int) -> float:
    normalized = midi_value / 127
    if name == "rate":
        return 0.0 if midi_value == 64 else normalized * 2 - 1
    return normalized


def split_midi_integer(value: int, parts: int = 5) -> tuple[int, ...]:
    if value < 0 or value >= 128**parts:
        raise ValueError(f"integer must be between 0 and {128**parts - 1}")
    return tuple((value >> (7 * index)) & 0x7F for index in range(parts))


def join_midi_integer(parts: list[int] | tuple[int, ...]) -> int:
    if any(part < 0 or part > 127 for part in parts):
        raise ValueError("MIDI integer parts must be between 0 and 127")
    return sum(part << (7 * index) for index, part in enumerate(parts))


def seconds_to_engine_samples(seconds: float, sample_rate: int) -> int:
    if seconds < 0:
        raise ValueError("seconds must be non-negative")
    if sample_rate <= 0:
        raise ValueError("sample rate must be positive")
    return round(seconds * sample_rate * 2)


def show_ports() -> None:
    print("MIDI inputs:")
    for name in mido.get_input_names():
        print(f"  {name}")
    print("MIDI outputs:")
    for name in mido.get_output_names():
        print(f"  {name}")


def send_set(target: str, control: str, value: float) -> None:
    with mido.open_output(port_name()) as output:
        send_value(output, target, control, value)
    print(
        f"sent target={target} control={control} value={value} "
        f"port={port_name()!r}"
    )


def send_library(direction: str) -> None:
    with mido.open_output(port_name()) as output:
        send_library_value(output, direction)
    print(f"selected {direction} library track")


def send_library_value(output: mido.ports.BaseOutput, direction: str) -> None:
    output.send(
        mido.Message(
            "control_change", channel=0, control=LIBRARY_CC[direction], value=127
        )
    )


def send_load(deck: int) -> None:
    if deck not in LOAD_CC:
        raise ValueError("deck must be 1 or 2")
    with mido.open_output(port_name()) as output:
        send_load_value(output, deck)
    print(f"requested selected track load into deck {deck}")


def send_stage_sampler(sampler_slot: int) -> None:
    with mido.open_output(port_name()) as output:
        send_stage_sampler_value(output, sampler_slot)
    print(f"staged selected track into sampler slot {sampler_slot}")


def send_load_sampler(deck: int, sampler_slot: int) -> None:
    with mido.open_output(port_name()) as output:
        send_load_sampler_value(output, deck, sampler_slot)
    print(f"requested sampler slot {sampler_slot} load into deck {deck}")


def send_load_value(output: mido.ports.BaseOutput, deck: int) -> None:
    record_expected_change(f"deck:{deck}", "track_loaded", False, ttl=3.0)
    record_expected_change(f"deck:{deck}", "track_loaded", True, ttl=3.0)
    # Identity is reported once while the new numeric fingerprint is still
    # unresolved and again after the observer resolves it against the catalog.
    # Attribute both reports to this single automated load command.
    record_expected_change(f"deck:{deck}", "track_identity", None, ttl=3.0)
    record_expected_change(f"deck:{deck}", "track_identity", None, ttl=3.0)
    output.send(
        mido.Message(
            "control_change", channel=deck - 1, control=LOAD_CC[deck], value=127
        )
    )


def send_stage_sampler_value(output: mido.ports.BaseOutput, sampler_slot: int) -> None:
    if not 1 <= sampler_slot <= MAX_QUEUE_SAMPLER_SLOTS:
        raise ValueError(
            f"sampler slot must be between 1 and {MAX_QUEUE_SAMPLER_SLOTS}"
        )
    output.send(
        mido.Message(
            "control_change",
            channel=0,
            control=RECORDING_COMMAND_CC,
            value=23 + sampler_slot,
        )
    )


def send_load_sampler_value(
    output: mido.ports.BaseOutput,
    deck: int,
    sampler_slot: int,
) -> None:
    if deck not in (1, 2):
        raise ValueError("deck must be 1 or 2")
    if not 1 <= sampler_slot <= MAX_QUEUE_SAMPLER_SLOTS:
        raise ValueError(
            f"sampler slot must be between 1 and {MAX_QUEUE_SAMPLER_SLOTS}"
        )
    record_expected_change(f"deck:{deck}", "track_loaded", False, ttl=3.0)
    record_expected_change(f"deck:{deck}", "track_loaded", True, ttl=3.0)
    record_expected_change(f"deck:{deck}", "track_identity", None, ttl=3.0)
    record_expected_change(f"deck:{deck}", "track_identity", None, ttl=3.0)
    command_base = 41 if deck == 1 else 51
    output.send(
        mido.Message(
            "control_change",
            channel=0,
            control=RECORDING_COMMAND_CC,
            value=command_base + sampler_slot,
        )
    )


def send_sync_value(
    output: mido.ports.BaseOutput, deck: int, control: str, enabled: bool
) -> None:
    if deck not in (1, 2):
        raise ValueError("deck must be 1 or 2")
    if control not in ("leader", "enabled"):
        raise ValueError("sync control must be leader or enabled")
    command_values = {
        (1, "leader", True): 10,
        (1, "leader", False): 11,
        (2, "leader", True): 12,
        (2, "leader", False): 13,
        (1, "enabled", True): 20,
        (1, "enabled", False): 21,
        (2, "enabled", True): 22,
        (2, "enabled", False): 23,
    }
    expected_modes = {
        (1, "leader", True): 2,
        (1, "leader", False): 0,
        (2, "leader", True): 2,
        (2, "leader", False): 0,
        (1, "enabled", True): 1,
        (1, "enabled", False): 0,
        (2, "enabled", True): 1,
        (2, "enabled", False): 0,
    }
    if not enabled:
        # Mixxx can briefly promote a follower (mode 1) to leader (mode 2)
        # before reporting sync off (mode 0). Attribute both reports to this
        # single command; reducers match by value, not insertion order.
        record_expected_change(f"deck:{deck}", "sync_mode", 2)
    record_expected_change(
        f"deck:{deck}", "sync_mode", expected_modes[(deck, control, enabled)]
    )
    # Mixxx can change the follower's rate as an indirect consequence of sync.
    # The exact value depends on both loaded tracks, so accept the next rate
    # report as part of this automation command.
    record_expected_change(f"deck:{deck}", "rate", None)
    output.send(
        mido.Message(
            "control_change",
            channel=0,
            control=SYNC_COMMAND_CC,
            value=command_values[(deck, control, enabled)],
        )
    )


def require_routine_protocol(output, timeout: float = 1.0) -> None:
    with feedback_input(port_name()) as incoming:
        output.send(mido.Message("control_change", channel=0, control=30, value=36))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in incoming.iter_pending():
                if message.type == "control_change" and message.channel == 0 and message.control == 75 and message.value == 2:
                    return
            time.sleep(.005)
    raise RuntimeError("Reload AI-DJ-Probe mapping: routine protocol v2 is required")


def send_tempo_ratio(output, deck: int, ratio: float) -> None:
    if deck not in (1, 2) or not math.isfinite(ratio) or not .8 <= ratio <= 1.2:
        raise ValueError("tempo ratio must be 0.8..1.2 on deck 1 or 2")
    encoded = round(ratio * 100000)
    parts = (encoded % 63, encoded // 63 % 63, encoded // (63 * 63))
    record_expected_change(f"deck:{deck}", "rate", None, ttl=.25)
    output.send(mido.Message("control_change", channel=0, control=30, value=deck + 33))
    for part in parts:
        output.send(mido.Message("control_change", channel=0, control=30, value=64 + part))


def read_position(deck: int, sample_rate: int, output, timeout: float = .5) -> float:
    """Read actual source-file seconds, independent of playback speed."""
    if deck not in (1, 2) or sample_rate <= 0:
        raise ValueError("invalid position request")
    with feedback_input(port_name()) as incoming:
        output.send(mido.Message("control_change", channel=0, control=30, value=deck + 5))
        parts = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in incoming.iter_pending():
                if message.type != "control_change" or message.channel != deck - 1:
                    continue
                if message.control in PRECISE_POSITION_PART_CC:
                    parts[message.control] = message.value
                elif message.control == PRECISE_POSITION_STATUS_CC and message.value == 1 and len(parts) == 5:
                    return join_midi_integer([parts[c] for c in PRECISE_POSITION_PART_CC]) / (2 * sample_rate)
            time.sleep(.005)
    raise RuntimeError("Mixxx position feedback timed out")


def send_bass_value(
    output: mido.ports.BaseOutput, deck: int, value: float
) -> None:
    if deck not in (1, 2):
        raise ValueError("deck must be 1 or 2")
    if not 0.0 <= value <= 1.0:
        raise ValueError("bass must be between 0.0 and 1.0")
    # Mixxx's EQ parameter callback can briefly report an intermediate value
    # before settling on the requested parameter value. Attribute that short
    # transient and the precise settled value to the command so lifecycle
    # supervision does not mistake a bass ramp for human EQ takeover.
    record_expected_change(f"deck:{deck}", "eq_low", None, ttl=0.25)
    record_expected_change(f"deck:{deck}", "eq_low", value, ttl=2.0)
    output.send(
        mido.Message(
            "control_change",
            channel=0,
            control=BASS_COMMAND_CC,
            value=39 + deck,
        )
    )
    output.send(
        mido.Message(
            "control_change",
            channel=0,
            control=BASS_COMMAND_CC,
            value=BASS_VALUE_OFFSET + round(value * BASS_VALUE_STEPS),
        )
    )


def decode_deck_status(messages: list[mido.Message]) -> dict[str, float | int]:
    sync_mode = None
    bpm_parts: dict[int, int] = {}
    file_bpm_parts: dict[int, int] = {}
    complete = False
    for message in messages:
        if message.type != "control_change":
            continue
        if message.control == DECK_STATUS_SYNC_MODE_CC:
            sync_mode = message.value
        elif message.control in DECK_STATUS_BPM_PART_CC:
            bpm_parts[message.control] = message.value
        elif message.control in DECK_STATUS_FILE_BPM_PART_CC:
            file_bpm_parts[message.control] = message.value
        elif message.control == DECK_STATUS_COMPLETE_CC and message.value == 1:
            complete = True
    if not complete or sync_mode is None:
        raise RuntimeError("Mixxx deck status response was incomplete")
    if len(bpm_parts) != 3 or len(file_bpm_parts) != 3:
        raise RuntimeError("Mixxx deck status response omitted BPM data")
    return {
        "sync_mode": sync_mode,
        "bpm": join_midi_integer(
            [bpm_parts[control] for control in DECK_STATUS_BPM_PART_CC]
        )
        / 1000,
        "file_bpm": join_midi_integer(
            [file_bpm_parts[control] for control in DECK_STATUS_FILE_BPM_PART_CC]
        )
        / 1000,
    }


def _request_deck_status(deck: int, output) -> dict[str, float | int]:
    """Request one deck status while reusing an already-open MIDI output."""
    with feedback_input(port_name()) as input_port:
        output.send(
            mido.Message(
                "control_change",
                channel=0,
                control=DECK_STATUS_COMMAND_CC,
                value=deck + 3,
            )
        )
        messages = []
        response_channel = deck - 1
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            message = input_port.poll()
            if message is None:
                time.sleep(0.005)
                continue
            if message.type != "control_change" or message.channel != response_channel:
                continue
            messages.append(message)
            if (
                message.control == DECK_STATUS_COMPLETE_CC
                and message.value == 1
            ):
                status = decode_deck_status(messages)
                print(
                    f"deck status deck={deck} sync_mode={status['sync_mode']} "
                    f"bpm={status['bpm']:.3f} file_bpm={status['file_bpm']:.3f}"
                )
                return status
    raise RuntimeError("timed out waiting for Mixxx deck status")


def deck_status(deck: int, *, output=None) -> dict[str, float | int]:
    if deck not in (1, 2):
        raise ValueError("deck must be 1 or 2")
    if output is not None:
        return _request_deck_status(deck, output)
    with mido.open_output(port_name()) as owned_output:
        return _request_deck_status(deck, owned_output)


def precise_seek(
    deck: int, seconds: float, sample_rate: int, play: bool
) -> dict[str, float | int]:
    if deck not in (1, 2):
        raise ValueError("deck must be 1 or 2")
    engine_samples = seconds_to_engine_samples(seconds, sample_rate)
    chunks = split_midi_integer(engine_samples)
    channel = deck - 1

    with feedback_input(port_name()) as input_port, mido.open_output(
        port_name()
    ) as output:
        record_expected_change(
            f"deck:{deck}",
            "position_seconds",
            seconds,
            ttl=2.0,
            tolerance=2.5,
        )
        # The commit command atomically sets both position and transport.
        if not play:
            record_expected_change(f"deck:{deck}", "sync_mode", None, ttl=2.0)
            other_deck = 2 if deck == 1 else 1
            record_expected_change(
                f"deck:{other_deck}", "sync_mode", None, ttl=2.0
            )
        record_expected_change(f"deck:{deck}", "play", play, ttl=2.0)
        for control, value in zip(PRECISE_SEEK_PART_CC, chunks, strict=True):
            output.send(
                mido.Message(
                    "control_change", channel=channel, control=control, value=value
                )
            )
        commit_sent_monotonic = time.monotonic()
        output.send(
            mido.Message(
                "control_change",
                channel=channel,
                control=PRECISE_SEEK_COMMIT_CC,
                value=2 if play else 1,
            )
        )

        feedback_parts: dict[int, int] = {}
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            message = input_port.poll()
            if message is None:
                time.sleep(0.005)
                continue
            if message.type != "control_change" or message.channel != channel:
                continue
            if message.control in PRECISE_POSITION_PART_CC:
                feedback_parts[message.control] = message.value
                continue
            if message.control != PRECISE_POSITION_STATUS_CC:
                continue

            status = PRECISE_SEEK_STATUSES.get(
                message.value, f"unknown_status_{message.value}"
            )
            if message.value != 1:
                raise RuntimeError(f"Mixxx rejected precise seek: {status}")
            if len(feedback_parts) != len(PRECISE_POSITION_PART_CC):
                raise RuntimeError("Mixxx precise seek response omitted position data")
            actual_samples = join_midi_integer(
                [feedback_parts[control] for control in PRECISE_POSITION_PART_CC]
            )
            actual_seconds = actual_samples / (sample_rate * 2)
            error_ms = (actual_seconds - seconds) * 1000
            print(
                f"precise seek ok deck={deck} requested={seconds:.6f}s "
                f"actual={actual_seconds:.6f}s error={error_ms:+.3f}ms "
                f"samples={actual_samples} play={int(play)}"
            )
            return {
                "actual_samples": actual_samples,
                "actual_seconds": actual_seconds,
                "commit_sent_monotonic": commit_sent_monotonic,
                "response_received_monotonic": time.monotonic(),
            }

    raise RuntimeError("timed out waiting for Mixxx precise seek response")


def recording(command: str) -> dict[str, float | int]:
    command_values = {"start": 1, "stop": 2, "status": 3}
    desired_status = {"start": 2, "stop": 0}.get(command)

    with feedback_input(port_name()) as input_port, mido.open_output(
        port_name()
    ) as output:
        output.send(
            mido.Message(
                "control_change",
                channel=0,
                control=RECORDING_COMMAND_CC,
                value=command_values[command],
            )
        )
        deadline = time.monotonic() + 5.0
        latest_status = None
        while time.monotonic() < deadline:
            message = input_port.poll()
            if message is None:
                time.sleep(0.01)
                continue
            if (
                message.type != "control_change"
                or message.channel != 0
                or message.control != RECORDING_STATUS_CC
            ):
                continue
            latest_status = message.value
            if command == "status" or latest_status == desired_status:
                names = {0: "stopped", 1: "initializing", 2: "active"}
                print(
                    f"recording status={latest_status} "
                    f"state={names.get(latest_status, 'unknown')}"
                )
                return {
                    "status": latest_status,
                    "response_received_monotonic": time.monotonic(),
                }

    raise RuntimeError(
        f"timed out waiting for Mixxx recording state; latest={latest_status}"
    )


def send_value(output: mido.ports.BaseOutput, target: str, control: str, value: float) -> None:
    if target == "master":
        if control not in MASTER_CC:
            raise ValueError("master supports only crossfader")
        channel = 0
        cc = MASTER_CC[control]
    else:
        deck = int(target)
        if deck not in (1, 2):
            raise ValueError("deck must be 1 or 2")
        if control == "bass":
            send_bass_value(output, deck, value)
            return
        if control not in INPUT_CC:
            raise ValueError(f"deck control must be one of: {', '.join(INPUT_CC)}")
        channel = deck - 1
        cc = INPUT_CC[control]

    midi_value = encode(control, value)
    observer_target = "master" if target == "master" else f"deck:{int(target)}"
    if target == "master" and control == "crossfader" and midi_value in (0, 127):
        # Muting the outgoing deck can move Mixxx's soft sync leader BEFORE
        # the executor sends stop. Register the exact role swap before MIDI,
        # not just as a later stop side effect. One-shot, short-lived matches
        # leave manual sync-off and unrelated role changes observable.
        audible_deck = 1 if midi_value == 0 else 2
        muted_deck = 2 if audible_deck == 1 else 1
        record_expected_change(f"deck:{muted_deck}", "sync_mode", 1)
        record_expected_change(f"deck:{audible_deck}", "sync_mode", 2)
    if control == "play" and not bool(value):
        # Stopping the sync leader can atomically promote the follower and
        # demote the stopped deck. Each deck reports at most one such side
        # effect before the explicit transport report.
        record_expected_change("deck:1", "sync_mode", None)
        record_expected_change("deck:2", "sync_mode", None)
    record_expected_change(observer_target, control, value)
    output.send(
        mido.Message(
            "control_change", channel=channel, control=cc, value=midi_value
        )
    )


def record_expected_change(
    target: str,
    control: str,
    value: float | bool | int | None,
    *,
    ttl: float = 0.75,
    tolerance: float = 0.02,
) -> None:
    """Append an automation intent for a concurrently running observer."""
    now = time.monotonic()
    payload = {
        "target": target,
        "control": control,
        "value": value,
        "issued_at": now,
        "expires_at": now + ttl,
        "tolerance": tolerance,
    }
    COMMAND_LOG.parent.mkdir(parents=True, exist_ok=True)
    with COMMAND_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")


class TransitionCancelled(Exception):
    """Raised when another process requests transition cancellation."""


def request_cancel() -> None:
    CANCEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    CANCEL_FILE.touch()
    with mido.open_output(port_name()) as output:
        send_value(output, "1", "play", 0.0)
        send_value(output, "2", "play", 0.0)
        send_value(output, "master", "crossfader", 0.5)
    print("cancel requested; both decks stopped and crossfader centered")


def wait_or_cancel(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        if CANCEL_FILE.exists():
            raise TransitionCancelled
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def transition(duration: float, lead_in: float, tail: float, steps: int) -> None:
    if duration <= 0 or lead_in < 0 or tail < 0 or steps < 2:
        raise ValueError("duration must be positive, lead/tail non-negative, steps >= 2")

    CANCEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    CANCEL_FILE.unlink(missing_ok=True)
    interval = duration / (steps - 1)
    print(
        f"transition: A lead-in={lead_in}s, crossfade={duration}s, "
        f"B tail={tail}s, steps={steps}",
        flush=True,
    )
    with mido.open_output(port_name()) as output:
        try:
            send_value(output, "1", "volume", 0.75)
            send_value(output, "2", "volume", 0.75)
            send_value(output, "master", "crossfader", 0.0)
            send_value(output, "2", "play", 0.0)
            send_value(output, "1", "play", 1.0)
            wait_or_cancel(lead_in)

            send_value(output, "2", "play", 1.0)
            for index in range(steps):
                if CANCEL_FILE.exists():
                    raise TransitionCancelled
                send_value(output, "master", "crossfader", index / (steps - 1))
                if index + 1 < steps:
                    wait_or_cancel(interval)

            send_value(output, "1", "play", 0.0)
            wait_or_cancel(tail)
        except (TransitionCancelled, KeyboardInterrupt):
            print("transition cancelled", flush=True)
        finally:
            send_value(output, "1", "play", 0.0)
            send_value(output, "2", "play", 0.0)
            send_value(output, "master", "crossfader", 0.5)
            CANCEL_FILE.unlink(missing_ok=True)
    print("transition finished safely; both decks stopped and crossfader centered")


def rotation(fade: float, lead_in: float, load_wait: float, tail: float, steps: int) -> None:
    if fade <= 0 or min(lead_in, load_wait, tail) < 0 or steps < 2:
        raise ValueError("fade must be positive, waits non-negative, steps >= 2")

    CANCEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    CANCEL_FILE.unlink(missing_ok=True)
    interval = fade / (steps - 1)
    print("rotation: A@deck1 -> B@deck2 -> load C@deck1 -> C@deck1", flush=True)

    with mido.open_output(port_name()) as output:
        try:
            for deck in ("1", "2"):
                send_value(output, deck, "play", 0.0)
                send_value(output, deck, "volume", 0.75)
            send_value(output, "master", "crossfader", 0.0)
            send_value(output, "1", "play", 1.0)
            wait_or_cancel(lead_in)

            send_value(output, "2", "play", 1.0)
            for index in range(steps):
                send_value(output, "master", "crossfader", index / (steps - 1))
                if index + 1 < steps:
                    wait_or_cancel(interval)
            send_value(output, "1", "play", 0.0)

            send_library_value(output, "next")
            wait_or_cancel(0.25)
            send_load_value(output, 1)
            wait_or_cancel(load_wait)

            send_value(output, "1", "volume", 0.75)
            send_value(output, "1", "play", 1.0)
            for index in range(steps):
                send_value(
                    output,
                    "master",
                    "crossfader",
                    1.0 - index / (steps - 1),
                )
                if index + 1 < steps:
                    wait_or_cancel(interval)
            send_value(output, "2", "play", 0.0)
            wait_or_cancel(tail)
        except (TransitionCancelled, KeyboardInterrupt):
            print("rotation cancelled", flush=True)
        finally:
            send_value(output, "1", "play", 0.0)
            send_value(output, "2", "play", 0.0)
            send_value(output, "master", "crossfader", 0.5)
            CANCEL_FILE.unlink(missing_ok=True)
    print("rotation finished safely; both decks stopped and crossfader centered")


def monitor() -> None:
    print(f"monitoring {port_name()!r}; press Ctrl-C to stop", flush=True)
    with mido.open_input(port_name()) as input_port:
        for message in input_port:
            if message.type != "control_change" or message.control not in OUTPUT_CC:
                continue
            control = OUTPUT_CC[message.control]
            value = decode(control, message.value)
            target = "master" if control == "crossfader" else f"deck={message.channel + 1}"
            print(f"{target} control={control} value={value:.4f} midi={message.value}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("ports", help="list available MIDI ports")
    subparsers.add_parser("monitor", help="print state feedback from Mixxx")
    observer_parser = subparsers.add_parser(
        "observer", help="emit filtered, revisioned Mixxx performance events"
    )
    observer_parser.add_argument(
        "--analysis-root", type=Path, default=DEFAULT_ANALYSIS_ROOT
    )
    observer_parser.add_argument(
        "--duration",
        type=float,
        help="stop after this many seconds; default runs until Ctrl-C",
    )
    observer_parser.add_argument(
        "--reconcile-interval",
        type=float,
        default=1.0,
        help="seconds between full-state snapshots (default: 1.0)",
    )
    subparsers.add_parser("cancel", help="cancel a transition and stop both decks")

    deck_status_parser = subparsers.add_parser(
        "deck-status", help="read sync mode, current BPM, and file BPM"
    )
    deck_status_parser.add_argument("deck", type=int, choices=(1, 2))

    library_parser = subparsers.add_parser("library", help="change library selection")
    library_parser.add_argument("direction", choices=sorted(LIBRARY_CC))

    load_parser = subparsers.add_parser("load", help="load selected library track")
    load_parser.add_argument("deck", type=int, choices=sorted(LOAD_CC))

    stage_sampler_parser = subparsers.add_parser(
        "stage-sampler", help="stage the selected library track in a fixed queue slot"
    )
    stage_sampler_parser.add_argument(
        "slot", type=int, choices=range(1, MAX_QUEUE_SAMPLER_SLOTS + 1)
    )

    load_sampler_parser = subparsers.add_parser(
        "load-sampler", help="load an exact staged queue slot into a deck"
    )
    load_sampler_parser.add_argument("deck", type=int, choices=(1, 2))
    load_sampler_parser.add_argument(
        "slot", type=int, choices=range(1, MAX_QUEUE_SAMPLER_SLOTS + 1)
    )

    seek_parser = subparsers.add_parser(
        "seek", help="seek to a track time using a multi-message sample position"
    )
    seek_parser.add_argument("deck", type=int, choices=(1, 2))
    seek_parser.add_argument("seconds", type=float)
    seek_parser.add_argument("--sample-rate", type=int, default=44100)
    seek_parser.add_argument("--play", action="store_true")

    record_parser = subparsers.add_parser(
        "record", help="start, stop, or query Mixxx master recording"
    )
    record_parser.add_argument("action", choices=("start", "stop", "status"))

    transition_parser = subparsers.add_parser(
        "transition", help="run a fixed A-to-B crossfade smoke test"
    )
    transition_parser.add_argument("--duration", type=float, default=4.0)
    transition_parser.add_argument("--lead-in", type=float, default=2.0)
    transition_parser.add_argument("--tail", type=float, default=2.0)
    transition_parser.add_argument("--steps", type=int, default=17)

    rotation_parser = subparsers.add_parser(
        "rotation", help="run A-to-B, load C, then B-to-C end-to-end test"
    )
    rotation_parser.add_argument("--fade", type=float, default=4.0)
    rotation_parser.add_argument("--lead-in", type=float, default=2.0)
    rotation_parser.add_argument("--load-wait", type=float, default=2.0)
    rotation_parser.add_argument("--tail", type=float, default=2.0)
    rotation_parser.add_argument("--steps", type=int, default=17)

    set_parser = subparsers.add_parser("set", help="set a Mixxx control")
    set_parser.add_argument("target", help="1, 2, or master")
    set_parser.add_argument("control")
    set_parser.add_argument("value", type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "ports":
            show_ports()
        elif args.command == "monitor":
            monitor()
        elif args.command == "observer":
            from observer import run_observer

            run_observer(
                port_name=port_name(),
                analysis_root=args.analysis_root,
                command_log=COMMAND_LOG,
                duration=args.duration,
                reconciliation_interval=args.reconcile_interval,
            )
        elif args.command == "cancel":
            request_cancel()
        elif args.command == "deck-status":
            deck_status(args.deck)
        elif args.command == "library":
            send_library(args.direction)
        elif args.command == "load":
            send_load(args.deck)
        elif args.command == "stage-sampler":
            send_stage_sampler(args.slot)
        elif args.command == "load-sampler":
            send_load_sampler(args.deck, args.slot)
        elif args.command == "seek":
            precise_seek(args.deck, args.seconds, args.sample_rate, args.play)
        elif args.command == "record":
            recording(args.action)
        elif args.command == "transition":
            transition(args.duration, args.lead_in, args.tail, args.steps)
        elif args.command == "rotation":
            rotation(args.fade, args.lead_in, args.load_wait, args.tail, args.steps)
        else:
            send_set(args.target, args.control, args.value)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

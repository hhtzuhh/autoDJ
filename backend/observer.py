#!/usr/bin/env python3
"""Deterministic, read-only Mixxx performance observer."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import time
from typing import Any, Callable

import mido
from midi_feedback import TimestampedMidiInput


SNAPSHOT_COMMAND_CC = 30
SNAPSHOT_COMMAND_VALUE = {1: 6, 2: 7}
PLAY_CC = 16
POSITION_CC = 17
RATE_CC = 18
VOLUME_CC = 19
CROSSFADER_CC = 25
TRACK_LOADED_CC = 26
LOOP_ENABLED_CC = 59
TRACK_SAMPLES_PART_CC = (60, 61, 62, 63, 64)
SAMPLE_RATE_PART_CC = (65, 66, 67)
FILE_BPM_PART_CC = (68, 69, 70)
TRACK_FINGERPRINT_COMPLETE_CC = 71
SYNC_MODE_CC = 72
EQ_LOW_CC = 73
FILTER_CC = 74
PRECISE_POSITION_PART_CC = (39, 40, 41, 42, 43)
PRECISE_POSITION_STATUS_CC = 44


def join_midi_integer(parts: list[int] | tuple[int, ...]) -> int:
    if any(part < 0 or part > 127 for part in parts):
        raise ValueError("MIDI integer parts must be between 0 and 127")
    return sum(part << (7 * index) for index, part in enumerate(parts))


@dataclass(frozen=True)
class TrackRecord:
    audio_sha256: str
    analysis_cache_key: str
    source_filename: str
    duration_seconds: float
    sample_rate: int
    file_bpm: float
    beats_seconds: tuple[float, ...]

    @property
    def expected_engine_samples(self) -> int:
        return round(self.duration_seconds * self.sample_rate * 2)


class TrackCatalog:
    """Resolve numeric Mixxx fingerprints without pretending they are hashes."""

    def __init__(self, records: list[TrackRecord]) -> None:
        self.records = records

    @classmethod
    def from_analysis_root(cls, root: Path) -> "TrackCatalog":
        records = []
        for path in sorted(root.glob("*/track-analysis-v1.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            audio = payload["audio"]
            rhythm = payload["rhythm"]
            records.append(
                TrackRecord(
                    audio_sha256=payload["audio_sha256"],
                    analysis_cache_key=payload["analysis_cache_key"],
                    source_filename=payload["source_filename"],
                    duration_seconds=float(payload["duration_seconds"]),
                    sample_rate=int(audio["sample_rate"]),
                    file_bpm=float(rhythm["bpm"]),
                    beats_seconds=tuple(float(value) for value in rhythm["beats_seconds"]),
                )
            )
        return cls(records)

    def resolve(
        self,
        *,
        track_samples: int,
        sample_rate: int,
        file_bpm: float,
        sample_tolerance_seconds: float = 0.5,
        exact_sample_tolerance_seconds: float = 0.05,
        bpm_tolerance: float = 1.5,
    ) -> tuple[TrackRecord | None, list[TrackRecord]]:
        # Mixxx updates these controls independently while loading a track.  For a
        # short interval track_samples can already describe the new track while
        # file_bpm still belongs to the previous one.  A unique, near-exact sample
        # length is therefore stronger evidence than that transient BPM value.
        exact_sample_tolerance = round(
            exact_sample_tolerance_seconds * sample_rate * 2
        )
        exact_matches = [
            record
            for record in self.records
            if record.sample_rate == sample_rate
            and abs(record.expected_engine_samples - track_samples)
            <= exact_sample_tolerance
        ]
        if exact_matches:
            return (
                exact_matches[0] if len(exact_matches) == 1 else None,
                exact_matches,
            )

        sample_tolerance = round(sample_tolerance_seconds * sample_rate * 2)
        matches = [
            record
            for record in self.records
            if record.sample_rate == sample_rate
            and abs(record.expected_engine_samples - track_samples) <= sample_tolerance
            and abs(record.file_bpm - file_bpm) <= bpm_tolerance
        ]
        return (matches[0] if len(matches) == 1 else None), matches


@dataclass
class ExpectedChange:
    target: str
    control: str
    value: float | bool | int | None
    expires_at: float
    tolerance: float = 0.02
    issued_at: float = 0.0


class CommandLogReader:
    """Tail automation intents written by other local control processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = path.stat().st_size if path.exists() else 0

    def read(self) -> list[ExpectedChange]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:
            self.offset = 0
        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self.offset)
            lines = handle.readlines()
            self.offset = handle.tell()
        changes = []
        for line in lines:
            payload = json.loads(line)
            changes.append(
                ExpectedChange(
                    target=payload["target"],
                    control=payload["control"],
                    value=payload.get("value"),
                    expires_at=float(payload["expires_at"]),
                    tolerance=float(payload.get("tolerance", 0.02)),
                    issued_at=float(payload.get("issued_at", 0.0)),
                )
            )
        return changes


@dataclass
class DeckState:
    loaded: bool | None = None
    track_samples: int | None = None
    sample_rate: int | None = None
    file_bpm: float | None = None
    track_id: str | None = None
    analysis_cache_key: str | None = None
    source_filename: str | None = None
    identity_status: str = "unresolved"
    play: bool | None = None
    normalized_position: float | None = None
    position_seconds: float | None = None
    beat_index: int | None = None
    rate: float | None = None
    volume: float | None = None
    eq_low: float | None = None
    filter: float | None = None
    loop_enabled: bool | None = None
    sync_mode: int | None = None
    last_position_at: float | None = None


@dataclass
class PerformanceState:
    revision: int = 0
    decks: dict[int, DeckState] = field(
        default_factory=lambda: {1: DeckState(), 2: DeckState()}
    )
    crossfader: float | None = None
    active_plan_id: str | None = None


@dataclass(frozen=True)
class ObserverEvent:
    event: str
    revision: int
    origin: str
    observed_at: float
    details: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class ObserverReducer:
    """Reduce raw state changes into semantic, revisioned performance events."""

    def __init__(self, catalog: TrackCatalog) -> None:
        self.catalog = catalog
        self.state = PerformanceState()
        self.expected: list[ExpectedChange] = []
        self.active_gestures: dict[str, tuple[float, str]] = {}

    def expect(
        self,
        target: str,
        control: str,
        value: float | bool | int | None,
        *,
        now: float,
        ttl: float = 0.75,
        tolerance: float = 0.02,
    ) -> None:
        self.expected.append(
            ExpectedChange(target, control, value, now + ttl, tolerance, now)
        )

    def _origin(
        self,
        target: str,
        control: str,
        value: float | bool | int | None,
        now: float,
        *,
        first_observation: bool,
    ) -> str:
        self.expected = [item for item in self.expected if item.expires_at >= now]
        for index, item in enumerate(self.expected):
            if item.target != target or item.control != control or item.issued_at > now:
                continue
            if item.value is None or value is None:
                self.expected.pop(index)
                return "automation"
            if isinstance(value, bool) or isinstance(item.value, bool):
                matched = bool(value) == bool(item.value)
            else:
                matched = math.isclose(
                    float(value), float(item.value), abs_tol=item.tolerance
                )
            if matched:
                self.expected.pop(index)
                return "automation"
        return "system" if first_observation else "human_or_external"

    def _emit(
        self,
        event: str,
        origin: str,
        now: float,
        details: dict[str, Any],
        *,
        revisioned: bool = True,
    ) -> ObserverEvent:
        if revisioned:
            self.state.revision += 1
        return ObserverEvent(event, self.state.revision, origin, now, details)

    def apply_control(
        self,
        target: str,
        control: str,
        value: float | bool | int,
        *,
        now: float,
    ) -> list[ObserverEvent]:
        if target == "master":
            if control != "crossfader":
                return []
            first = self.state.crossfader is None
            previous = self.state.crossfader
            self.state.crossfader = float(value)
            if first:
                return []
            if math.isclose(float(previous), float(value), abs_tol=0.01):
                return []
            origin = self._origin(target, control, value, now, first_observation=False)
            gesture_key = f"{target}:{control}"
            last_change = self.active_gestures.get(gesture_key)
            self.active_gestures[gesture_key] = (now, origin)
            if last_change is not None and now - last_change[0] <= 0.5:
                if (
                    last_change[1] == "automation"
                    and origin == "human_or_external"
                ):
                    return [
                        self._emit(
                            "mixer_changed",
                            origin,
                            now,
                            {
                                "control": control,
                                "previous": previous,
                                "value": value,
                            },
                        )
                    ]
                return []
            return [
                self._emit(
                    "mixer_gesture_started",
                    origin,
                    now,
                    {"control": control, "previous": previous, "value": value},
                )
            ]

        deck_index = int(target.removeprefix("deck:"))
        deck = self.state.decks[deck_index]
        if control == "position":
            return self._apply_position(deck_index, deck, float(value), now)

        attribute = {
            "play": "play",
            "rate": "rate",
            "volume": "volume",
            "eq_low": "eq_low",
            "filter": "filter",
            "loop_enabled": "loop_enabled",
            "sync_mode": "sync_mode",
        }.get(control)
        if attribute is None:
            return []
        previous = getattr(deck, attribute)
        first = previous is None
        cast_value: Any = value
        if control in {"play", "loop_enabled"}:
            cast_value = bool(value)
        elif control == "sync_mode":
            cast_value = int(value)
        else:
            cast_value = float(value)
        setattr(deck, attribute, cast_value)
        if control == "play":
            deck.last_position_at = now
        if first or previous == cast_value:
            return []
        origin = self._origin(target, control, cast_value, now, first_observation=False)
        event = {
            "play": "transport_changed",
            "rate": "tempo_control_changed",
            "volume": "channel_control_changed",
            "eq_low": "channel_control_changed",
            "filter": "channel_control_changed",
            "loop_enabled": "loop_changed",
            "sync_mode": "sync_changed",
        }[control]
        return [
            self._emit(
                event,
                origin,
                now,
                {
                    "deck": deck_index,
                    "control": control,
                    "previous": previous,
                    "value": cast_value,
                },
            )
        ]

    def _apply_position(
        self, deck_index: int, deck: DeckState, normalized: float, now: float
    ) -> list[ObserverEvent]:
        previous_seconds = deck.position_seconds
        previous_at = deck.last_position_at
        deck.normalized_position = normalized
        duration = None
        record = next(
            (item for item in self.catalog.records if item.audio_sha256 == deck.track_id),
            None,
        )
        if record:
            duration = record.duration_seconds
        elif deck.track_samples and deck.sample_rate:
            duration = deck.track_samples / (deck.sample_rate * 2)
        if duration is None:
            deck.last_position_at = now
            return []
        seconds = normalized * duration
        deck.position_seconds = seconds
        deck.last_position_at = now
        if record:
            deck.beat_index = max(0, bisect_right(record.beats_seconds, seconds) - 1)
        if previous_seconds is None or previous_at is None:
            return []
        expected_seconds = (
            previous_seconds + (now - previous_at) if deck.play else previous_seconds
        )
        jump_threshold = max(2.5, duration / 50)
        if abs(seconds - expected_seconds) <= jump_threshold:
            return []
        origin = self._origin(
            f"deck:{deck_index}",
            "position_seconds",
            seconds,
            now,
            first_observation=False,
        )
        return [
            self._emit(
                "position_jump",
                origin,
                now,
                {
                    "deck": deck_index,
                    "previous_seconds": previous_seconds,
                    "expected_seconds": expected_seconds,
                    "position_seconds": seconds,
                    "beat_index": deck.beat_index,
                },
            )
        ]

    def apply_track_loaded(
        self, deck_index: int, loaded: bool, *, now: float
    ) -> list[ObserverEvent]:
        deck = self.state.decks[deck_index]
        previous = deck.loaded
        deck.loaded = loaded
        first = previous is None
        if loaded == previous:
            return []
        if not loaded:
            self.state.decks[deck_index] = DeckState(loaded=False)
        origin = self._origin(
            f"deck:{deck_index}", "track_loaded", loaded, now, first_observation=first
        )
        if first:
            return []
        return [
            self._emit(
                "track_load_state_changed",
                origin,
                now,
                {"deck": deck_index, "loaded": loaded},
            )
        ]

    def apply_fingerprint(
        self,
        deck_index: int,
        *,
        track_samples: int,
        sample_rate: int,
        file_bpm: float,
        now: float,
    ) -> list[ObserverEvent]:
        deck = self.state.decks[deck_index]
        first = deck.track_samples is None
        previous_identity = (
            deck.track_samples,
            deck.sample_rate,
            deck.file_bpm,
            deck.identity_status,
            deck.track_id,
        )
        record, matches = self.catalog.resolve(
            track_samples=track_samples,
            sample_rate=sample_rate,
            file_bpm=file_bpm,
        )
        deck.track_samples = track_samples
        deck.sample_rate = sample_rate
        deck.file_bpm = file_bpm
        if record:
            deck.track_id = record.audio_sha256
            deck.analysis_cache_key = record.analysis_cache_key
            deck.source_filename = record.source_filename
            deck.identity_status = "resolved_numeric_fingerprint"
        else:
            deck.track_id = None
            deck.analysis_cache_key = None
            deck.source_filename = None
            deck.identity_status = "ambiguous" if matches else "unresolved"
        current_identity = (
            deck.track_samples,
            deck.sample_rate,
            deck.file_bpm,
            deck.identity_status,
            deck.track_id,
        )
        details = {
            "deck": deck_index,
            "track_samples": track_samples,
            "sample_rate": sample_rate,
            "file_bpm": file_bpm,
            "identity_status": deck.identity_status,
            "track_id": deck.track_id,
            "source_filename": deck.source_filename,
            "candidate_count": len(matches),
        }
        if previous_identity == current_identity:
            return []
        origin = self._origin(
            f"deck:{deck_index}", "track_identity", None, now, first_observation=first
        )
        if first:
            return [self._emit("track_identity_snapshot", origin, now, details, revisioned=False)]
        return [self._emit("track_identity_changed", origin, now, details)]


class MidiObserverDecoder:
    def __init__(self) -> None:
        self.fingerprint_parts: dict[int, dict[int, int]] = {1: {}, 2: {}}
        self.position_parts: dict[int, dict[int, int]] = {1: {}, 2: {}}

    def feed(self, message: mido.Message) -> list[dict[str, Any]]:
        if message.type != "control_change":
            return []
        deck_index = message.channel + 1
        control = message.control
        if control in PRECISE_POSITION_PART_CC:
            self.position_parts[deck_index][control] = message.value
            return []
        if control == PRECISE_POSITION_STATUS_CC and message.value == 1:
            parts = self.position_parts[deck_index]
            if not all(item in parts for item in PRECISE_POSITION_PART_CC):
                return []
            payload = {
                "kind": "precise_position",
                "deck": deck_index,
                "engine_samples": join_midi_integer(
                    [parts[item] for item in PRECISE_POSITION_PART_CC]
                ),
            }
            self.position_parts[deck_index] = {}
            return [payload]
        if control in TRACK_SAMPLES_PART_CC + SAMPLE_RATE_PART_CC + FILE_BPM_PART_CC:
            self.fingerprint_parts[deck_index][control] = message.value
            return []
        if control == TRACK_FINGERPRINT_COMPLETE_CC:
            parts = self.fingerprint_parts[deck_index]
            required = TRACK_SAMPLES_PART_CC + SAMPLE_RATE_PART_CC + FILE_BPM_PART_CC
            if not all(item in parts for item in required):
                return []
            payload = {
                "kind": "fingerprint",
                "deck": deck_index,
                "track_samples": join_midi_integer(
                    [parts[item] for item in TRACK_SAMPLES_PART_CC]
                ),
                "sample_rate": join_midi_integer(
                    [parts[item] for item in SAMPLE_RATE_PART_CC]
                ),
                "file_bpm": join_midi_integer(
                    [parts[item] for item in FILE_BPM_PART_CC]
                )
                / 1000,
            }
            self.fingerprint_parts[deck_index] = {}
            return [payload]
        if control == CROSSFADER_CC:
            return [
                {
                    "kind": "control",
                    "target": "master",
                    "control": "crossfader",
                    "value": message.value / 127,
                }
            ]
        mapping = {
            PLAY_CC: ("play", message.value > 0),
            POSITION_CC: ("position", message.value / 127),
            RATE_CC: (
                "rate",
                0.0 if message.value == 64 else message.value / 127 * 2 - 1,
            ),
            VOLUME_CC: ("volume", message.value / 127),
            EQ_LOW_CC: ("eq_low", message.value / 127),
            FILTER_CC: ("filter", message.value / 127),
            LOOP_ENABLED_CC: ("loop_enabled", message.value > 0),
            SYNC_MODE_CC: ("sync_mode", message.value),
        }
        if control == TRACK_LOADED_CC:
            return [
                {"kind": "track_loaded", "deck": deck_index, "loaded": message.value > 0}
            ]
        if control not in mapping or deck_index not in (1, 2):
            return []
        name, value = mapping[control]
        return [
            {
                "kind": "control",
                "target": f"deck:{deck_index}",
                "control": name,
                "value": value,
            }
        ]


def request_snapshot(output: mido.ports.BaseOutput) -> None:
    for deck, value in SNAPSHOT_COMMAND_VALUE.items():
        output.send(
            mido.Message(
                "control_change",
                channel=0,
                control=SNAPSHOT_COMMAND_CC,
                value=value,
            )
        )


def apply_decoded(
    reducer: ObserverReducer, payload: dict[str, Any], *, now: float
) -> list[ObserverEvent]:
    if payload["kind"] == "control":
        return reducer.apply_control(
            payload["target"], payload["control"], payload["value"], now=now
        )
    if payload["kind"] == "track_loaded":
        return reducer.apply_track_loaded(payload["deck"], payload["loaded"], now=now)
    if payload["kind"] == "fingerprint":
        return reducer.apply_fingerprint(
            payload["deck"],
            track_samples=payload["track_samples"],
            sample_rate=payload["sample_rate"],
            file_bpm=payload["file_bpm"],
            now=now,
        )
    if payload["kind"] == "precise_position":
        deck = reducer.state.decks[payload["deck"]]
        if not deck.track_samples:
            return []
        return reducer.apply_control(
            f"deck:{payload['deck']}",
            "position",
            payload["engine_samples"] / deck.track_samples,
            now=now,
        )
    return []


class ObserverUpdatePublisher:
    """Coalesce telemetry-only callbacks while delivering every control event immediately.

    A position query returns a full Mixxx snapshot. Rebuilding the dashboard for
    every decoded field can make the MIDI consumer slower than the producer,
    expiring automation intents before their feedback is read.
    """
    def __init__(self, callback, interval: float = 0.05):
        self.callback = callback
        self.interval = interval
        self.next_at = 0.0
        self.pending = None

    def offer(self, state, events, now):
        if self.callback is None:
            return
        self.pending = state
        if events or now >= self.next_at:
            self.pending = None
            self.callback(state, events)
            self.next_at = time.monotonic() + self.interval

    def flush(self, now):
        if self.pending is not None and now >= self.next_at:
            self.offer(self.pending, (), now)


def run_observer(
    *,
    port_name: str,
    analysis_root: Path,
    command_log: Path | None = None,
    duration: float | None = None,
    reconciliation_interval: float = 1.0,
    refresh_catalog: bool = False,
    on_update: Callable[[PerformanceState, tuple[ObserverEvent, ...]], None]
    | None = None,
) -> None:
    if reconciliation_interval <= 0:
        raise ValueError("reconciliation_interval must be positive")
    catalog = TrackCatalog.from_analysis_root(analysis_root)
    reducer = ObserverReducer(catalog)
    decoder = MidiObserverDecoder()
    command_reader = CommandLogReader(command_log) if command_log else None
    publisher = ObserverUpdatePublisher(on_update)
    started = time.monotonic()
    print(
        json.dumps(
            {
                "event": "observer_started",
                "port": port_name,
                "catalog_tracks": len(catalog.records),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    with TimestampedMidiInput(port_name) as input_port, mido.open_output(port_name) as output:
        request_snapshot(output)
        next_reconciliation = started + reconciliation_interval
        while duration is None or time.monotonic() - started < duration:
            current_time = time.monotonic()
            if current_time >= next_reconciliation:
                if refresh_catalog:
                    # A cold-start builder creates analyses after this observer
                    # starts. Refresh before requesting fresh track fingerprints.
                    reducer.catalog = TrackCatalog.from_analysis_root(analysis_root)
                request_snapshot(output)
                next_reconciliation = current_time + reconciliation_interval
            if command_reader:
                reducer.expected.extend(command_reader.read())
            received = input_port.poll()
            if received is None:
                publisher.flush(time.monotonic())
                time.sleep(0.005)
                continue
            # Producers append their intent before sending MIDI. Read again
            # after receiving a message to close the cross-process race where
            # the intent arrived just after the pre-poll tail read.
            if command_reader:
                reducer.expected.extend(command_reader.read())
            message, received_at = received
            now = time.monotonic()
            for payload in decoder.feed(message):
                events = tuple(apply_decoded(reducer, payload, now=received_at))
                for event in events:
                    print(json.dumps({**asdict(event), "processed_at": now,
                        "feedback_delay_seconds": round(max(0.0, now - received_at), 6)},
                        ensure_ascii=False, sort_keys=True), flush=True)
                publisher.offer(reducer.state, events, now)

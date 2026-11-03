#!/usr/bin/env python3
"""Deterministic plan lifecycle and human-takeover invalidation rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Protocol

from queue_service import QueueSnapshot


LIFECYCLE_SCHEMA_VERSION = 1
from runtime_paths import RUNTIME_ROOT
DEFAULT_TAKEOVER_PATH = RUNTIME_ROOT / "takeover.json"
PRE_EXECUTION_STATES = {"draft", "ready", "armed"}
TERMINAL_STATES = {"completed", "invalidated", "cancelled"}
INVALIDATING_HUMAN_EVENTS = {
    "position_jump": "human_seek",
    "loop_changed": "human_loop",
    "track_load_state_changed": "human_track_load",
    "track_identity_changed": "human_track_change",
    "mixer_gesture_started": "human_mixer_takeover",
    "mixer_changed": "human_mixer_takeover",
    "channel_control_changed": "human_channel_takeover",
    "transport_changed": "human_transport_change",
    "tempo_control_changed": "human_tempo_change",
    "sync_changed": "human_sync_change",
}
SAFE_BEFORE_TRANSITION_CONTROLS = {"volume", "eq_low", "filter"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DeckPerformance:
    deck: int
    track_id: str | None
    analysis_cache_key: str | None
    beat_index: int | None
    play: bool | None
    position_seconds: float | None = None
    position_observed_monotonic: float | None = None


@dataclass(frozen=True)
class PerformanceSnapshot:
    revision: int
    decks: dict[int, DeckPerformance]

    @classmethod
    def from_observer_state(cls, state: Any) -> "PerformanceSnapshot":
        return cls(
            revision=int(state.revision),
            decks={
                int(deck_id): DeckPerformance(
                    deck=int(deck_id),
                    track_id=deck.track_id,
                    analysis_cache_key=deck.analysis_cache_key,
                    beat_index=deck.beat_index,
                    play=deck.play,
                    position_seconds=deck.position_seconds,
                    position_observed_monotonic=deck.last_position_at,
                )
                for deck_id, deck in state.decks.items()
            },
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PerformanceSnapshot":
        performance = payload.get("performance", payload)
        return cls(
            revision=int(performance["revision"]),
            decks={
                int(deck_id): DeckPerformance(
                    deck=int(deck_id),
                    track_id=deck.get("track_id"),
                    analysis_cache_key=deck.get("analysis_cache_key"),
                    beat_index=(
                        int(deck["beat_index"])
                        if deck.get("beat_index") is not None
                        else None
                    ),
                    play=deck.get("play"),
                    position_seconds=(
                        float(deck["position_seconds"])
                        if deck.get("position_seconds") is not None
                        else None
                    ),
                    position_observed_monotonic=(
                        float(deck["position_observed_monotonic"])
                        if deck.get("position_observed_monotonic") is not None
                        else None
                    ),
                )
                for deck_id, deck in performance["decks"].items()
            },
        )


@dataclass(frozen=True)
class PlanTrackBinding:
    role: str
    deck: int
    track_id: str
    analysis_cache_key: str


@dataclass
class LifecycleEntry:
    from_status: str | None
    to_status: str
    reason: str
    at: str = field(default_factory=utc_now)


@dataclass
class PlanEnvelope:
    plan_id: str
    source_window_id: str
    status: str
    bound_performance_revision: int
    last_performance_revision: int
    bound_queue_revision: int
    latest_arm_beat_index: int
    transition_start_beat_index: int
    tracks: tuple[PlanTrackBinding, ...]
    invalidation_reason: str | None = None
    preserve_playback: bool = True
    schema_version: int = LIFECYCLE_SCHEMA_VERSION
    history: list[LifecycleEntry] = field(default_factory=list)

    @classmethod
    def from_rolling_selection(
        cls,
        selection: dict[str, Any],
        performance: PerformanceSnapshot,
    ) -> "PlanEnvelope":
        plan = selection["next_executable_plan"]
        if int(selection["queue_revision"]) < 0:
            raise ValueError("queue revision cannot be negative")
        deck_a = plan["deck_a"]
        deck_b = plan["deck_b"]
        latest_arm = int(deck_a["play_start_beat_index"])
        transition_start = int(plan.get("automation_start_beat_index", deck_a["transition_start_beat_index"]))
        if latest_arm > transition_start:
            raise ValueError("latest arm beat must not follow transition start")
        envelope = cls(
            plan_id=str(plan["plan_id"]),
            source_window_id=str(selection["source_window_id"]),
            status="draft",
            bound_performance_revision=performance.revision,
            last_performance_revision=performance.revision,
            bound_queue_revision=int(selection["queue_revision"]),
            latest_arm_beat_index=latest_arm,
            transition_start_beat_index=transition_start,
            tracks=(
                PlanTrackBinding(
                    role="a",
                    deck=int(deck_a["deck"]),
                    track_id=str(deck_a["audio_sha256"]),
                    analysis_cache_key=str(deck_a["analysis_cache_key"]),
                ),
                PlanTrackBinding(
                    role="b",
                    deck=int(deck_b["deck"]),
                    track_id=str(deck_b["audio_sha256"]),
                    analysis_cache_key=str(deck_b["analysis_cache_key"]),
                ),
            ),
        )
        envelope.history.append(
            LifecycleEntry(None, "draft", "rolling_selection_bound")
        )
        return envelope

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tracks"] = [asdict(track) for track in self.tracks]
        return payload


class AutomationScheduler(Protocol):
    def cancel_future(self, plan_id: str, *, preserve_playback: bool) -> None: ...


class NoopScheduler:
    def cancel_future(self, plan_id: str, *, preserve_playback: bool) -> None:
        return None


class FileAutomationScheduler:
    """Signal the local executor to stop future actions without touching playback."""

    def __init__(self, takeover_path: Path = DEFAULT_TAKEOVER_PATH) -> None:
        self.takeover_path = takeover_path

    def cancel_future(self, plan_id: str, *, preserve_playback: bool) -> None:
        if not preserve_playback:
            raise ValueError("human takeover must preserve playback")
        self.takeover_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.takeover_path.with_suffix(self.takeover_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "plan_id": plan_id,
                    "preserve_playback": True,
                    "requested_at": utc_now(),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.takeover_path)


class PlanLifecycleManager:
    def __init__(
        self,
        envelope: PlanEnvelope,
        scheduler: AutomationScheduler | None = None,
    ) -> None:
        self.envelope = envelope
        self.scheduler = scheduler or NoopScheduler()

    def _transition(self, to_status: str, reason: str) -> None:
        previous = self.envelope.status
        self.envelope.status = to_status
        self.envelope.history.append(LifecycleEntry(previous, to_status, reason))

    def _track_mismatch(self, performance: PerformanceSnapshot) -> str | None:
        for binding in self.envelope.tracks:
            deck = performance.decks.get(binding.deck)
            if deck is None:
                return f"deck_{binding.deck}_missing"
            if deck.track_id != binding.track_id:
                return f"deck_{binding.deck}_track_identity_mismatch"
            if deck.analysis_cache_key != binding.analysis_cache_key:
                return f"deck_{binding.deck}_analysis_key_mismatch"
        return None

    def _precondition_error(
        self,
        performance: PerformanceSnapshot,
        queue: QueueSnapshot,
    ) -> str | None:
        if queue.revision != self.envelope.bound_queue_revision:
            return "queue_revision_changed"
        mismatch = self._track_mismatch(performance)
        if mismatch:
            return mismatch
        source = performance.decks[self.envelope.tracks[0].deck]
        if source.beat_index is None:
            return "source_beat_unknown"
        if source.beat_index > self.envelope.latest_arm_beat_index:
            return "latest_arm_beat_passed"
        if source.play is not True:
            return "source_deck_not_playing"
        if performance.revision != self.envelope.last_performance_revision:
            return "performance_revision_changed_without_event"
        return None

    def mark_ready(
        self,
        performance: PerformanceSnapshot,
        queue: QueueSnapshot,
    ) -> None:
        if self.envelope.status != "draft":
            raise ValueError("only a draft plan can become ready")
        error = self._precondition_error(performance, queue)
        if error:
            raise ValueError(f"plan is not ready: {error}")
        self._transition("ready", "all_local_preconditions_satisfied")

    def arm(
        self,
        performance: PerformanceSnapshot,
        queue: QueueSnapshot,
    ) -> None:
        if self.envelope.status != "ready":
            raise ValueError("only a ready plan can be armed")
        error = self._precondition_error(performance, queue)
        if error:
            self.invalidate(error)
            raise ValueError(f"plan cannot arm: {error}")
        self._transition("armed", "operator_or_policy_armed")

    def start_execution(
        self,
        performance: PerformanceSnapshot,
        queue: QueueSnapshot,
    ) -> None:
        if self.envelope.status != "armed":
            raise ValueError("only an armed plan can execute")
        error = self._precondition_error(performance, queue)
        if error:
            self.invalidate(error)
            raise ValueError(f"plan cannot execute: {error}")
        self._transition("executing", "executor_started")

    def complete(self) -> None:
        if self.envelope.status != "executing":
            raise ValueError("only an executing plan can complete")
        self._transition("completed", "executor_completed")

    def invalidate(self, reason: str) -> None:
        if self.envelope.status in TERMINAL_STATES:
            return
        previous = self.envelope.status
        if previous in {"armed", "executing"}:
            self.scheduler.cancel_future(
                self.envelope.plan_id,
                preserve_playback=True,
            )
        self.envelope.invalidation_reason = reason
        target = "cancelled" if previous == "executing" else "invalidated"
        self._transition(target, reason)

    def handle_queue(self, queue: QueueSnapshot) -> None:
        if self.envelope.status in TERMINAL_STATES:
            return
        if queue.revision != self.envelope.bound_queue_revision:
            self.invalidate("queue_revision_changed")

    def reconcile_performance(self, performance: PerformanceSnapshot) -> None:
        if self.envelope.status in TERMINAL_STATES:
            return
        if self.envelope.status in {"ready", "armed", "executing"}:
            mismatch = self._track_mismatch(performance)
            if mismatch:
                self.invalidate(mismatch)
                return
        source = performance.decks.get(self.envelope.tracks[0].deck)
        if (
            self.envelope.status in PRE_EXECUTION_STATES
            and source
            and source.beat_index is not None
            and source.beat_index > self.envelope.latest_arm_beat_index
        ):
            self.invalidate("latest_arm_beat_passed")

    def handle_observer_event(self, event: dict[str, Any]) -> None:
        if self.envelope.status in TERMINAL_STATES:
            return
        revision = int(event["revision"])
        if revision <= self.envelope.last_performance_revision:
            return
        origin = str(event.get("origin", ""))
        event_name = str(event.get("event", ""))
        if origin == "system":
            self.envelope.last_performance_revision = revision
            return
        if origin == "human_or_external":
            details = event.get("details", {})
            source_beat = event.get("source_beat_index")
            if (
                event_name == "position_jump"
                and details.get("deck") == self.envelope.tracks[0].deck
                and details.get("beat_index") is not None
                and int(details["beat_index"])
                < self.envelope.transition_start_beat_index
            ):
                self.envelope.last_performance_revision = revision
                return
            if (
                event_name == "channel_control_changed"
                and details.get("control") in SAFE_BEFORE_TRANSITION_CONTROLS
                and source_beat is not None
                and int(source_beat) < self.envelope.transition_start_beat_index
            ):
                self.envelope.last_performance_revision = revision
                return
            reason = INVALIDATING_HUMAN_EVENTS.get(event_name)
            if reason:
                self.envelope.last_performance_revision = revision
                self.invalidate(reason)
                return
        if origin == "automation" and self.envelope.status == "executing":
            self.envelope.last_performance_revision = revision
            return
        self.envelope.last_performance_revision = revision
        self.invalidate("unexpected_performance_change")


def write_envelope(path: Path, envelope: PlanEnvelope) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(envelope.to_dict(), indent=2, ensure_ascii=False, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)

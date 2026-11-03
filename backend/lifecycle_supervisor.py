#!/usr/bin/env python3
"""Bind a rolling plan lifecycle to live Mixxx observer updates."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from plan_lifecycle import (
    FileAutomationScheduler,
    PerformanceSnapshot,
    PlanEnvelope,
    PlanLifecycleManager,
    write_envelope,
)
from queue_service import QueueStore


from runtime_paths import PRODUCT_ROOT, RUNTIME_ROOT

DEFAULT_REBASE_PATH = RUNTIME_ROOT / "transition-rebase.json"
MIXXX_PYTHON = sys.executable
TRANSITION_EXECUTOR = Path(__file__).with_name("transition.py")


class LifecycleSupervisor:
    def __init__(
        self,
        *,
        selection: dict[str, Any],
        queue_store: QueueStore,
        envelope_path: Path,
        executor_plan_path: Path,
        launch_executor: bool,
        rebase_path: Path = DEFAULT_REBASE_PATH,
    ) -> None:
        self.selection = selection
        self.queue_store = queue_store
        self.envelope_path = envelope_path
        self.executor_plan_path = executor_plan_path
        self.launch_executor = launch_executor
        self.rebase_path = rebase_path
        self.manager: PlanLifecycleManager | None = None
        self.executor: subprocess.Popen[str] | None = None
        self.last_wait_reason: str | None = None
        self._lock = threading.RLock()

    def _emit(self, event: str, **details: Any) -> None:
        print(json.dumps({"supervisor_event": event, **details}), flush=True)

    def _expected_tracks_match(self, performance: PerformanceSnapshot) -> bool:
        plan = self.selection["next_executable_plan"]
        expected = {
            int(plan["deck_a"]["deck"]): (
                plan["deck_a"]["audio_sha256"],
                plan["deck_a"]["analysis_cache_key"],
            ),
            int(plan["deck_b"]["deck"]): (
                plan["deck_b"]["audio_sha256"],
                plan["deck_b"]["analysis_cache_key"],
            ),
        }
        return all(
            deck_id in performance.decks
            and performance.decks[deck_id].track_id == track_id
            and performance.decks[deck_id].analysis_cache_key == cache_key
            for deck_id, (track_id, cache_key) in expected.items()
        )

    def _wait_reason(self, performance: PerformanceSnapshot) -> str | None:
        queue = self.queue_store.load()
        if queue.revision != int(self.selection["queue_revision"]):
            return "queue_revision_mismatch"
        if not self._expected_tracks_match(performance):
            return "expected_tracks_not_loaded"
        source_deck = int(
            self.selection["next_executable_plan"]["deck_a"]["deck"]
        )
        source = performance.decks[source_deck]
        if source.beat_index is None:
            return "source_beat_unknown"
        latest_arm = int(
            self.selection["next_executable_plan"]["deck_a"][
                "play_start_beat_index"
            ]
        )
        if source.beat_index > latest_arm:
            return "latest_arm_beat_passed"
        if source.play is not True:
            return "source_deck_not_playing"
        target_deck = int(
            self.selection["next_executable_plan"]["deck_b"]["deck"]
        )
        target = performance.decks[target_deck]
        if target.play is None:
            return "target_transport_unknown"
        if target.play is True:
            return "target_deck_is_playing"
        return None

    def _persist(self) -> None:
        if self.manager:
            write_envelope(self.envelope_path, self.manager.envelope)

    def _write_safe_seek_rebase(
        self, performance: PerformanceSnapshot, event: dict[str, Any]
    ) -> None:
        assert self.manager is not None
        details = event.get("details", {})
        source_deck = self.manager.envelope.tracks[0].deck
        source = performance.decks[source_deck]
        if (
            event.get("origin") != "human_or_external"
            or event.get("event") != "position_jump"
            or details.get("deck") != source_deck
            or details.get("beat_index") is None
            or int(details["beat_index"])
            >= self.manager.envelope.transition_start_beat_index
            or source.position_seconds is None
            or source.position_observed_monotonic is None
        ):
            return
        payload = {
            "plan_id": self.manager.envelope.plan_id,
            "source_position_seconds": source.position_seconds,
            "source_observed_monotonic": source.position_observed_monotonic,
            "written_monotonic": time.monotonic(),
        }
        self.rebase_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.rebase_path.with_suffix(self.rebase_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(self.rebase_path)
        self._emit(
            "executor_rebased_after_safe_seek",
            plan_id=self.manager.envelope.plan_id,
            beat_index=int(details["beat_index"]),
        )

    def _launch(self, performance: PerformanceSnapshot) -> None:
        assert self.manager is not None
        queue = self.queue_store.load()
        command = [
            str(MIXXX_PYTHON),
            str(TRANSITION_EXECUTOR),
            str(self.executor_plan_path),
            "--execute",
        ]
        plan = self.selection["next_executable_plan"]
        if plan.get("source_start_mode") == "continue_playing":
            source = performance.decks[int(plan["deck_a"]["deck"])]
            if (
                source.position_seconds is None
                or source.position_observed_monotonic is None
            ):
                raise ValueError("continued source position is unavailable")
            command.extend(
                [
                    "--source-position-seconds",
                    str(source.position_seconds),
                    "--source-observed-monotonic",
                    str(source.position_observed_monotonic),
                ]
            )
        self.manager.arm(performance, queue)
        self.executor_plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.executor_plan_path.write_text(
            json.dumps(
                plan,
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            if plan.get("tempo_routine") or plan.get("in_track_actions"):
                # Clear an obsolete cancellation before launching, not inside
                # the child where it could erase a fresh takeover signal.
                runtime = RUNTIME_ROOT
                (runtime / "cancel").unlink(missing_ok=True)
            self.executor = subprocess.Popen(
                command,
                cwd=PRODUCT_ROOT,
                text=True,
            )
        except OSError:
            self.manager.invalidate("executor_launch_failed")
            self._persist()
            raise
        self.manager.start_execution(performance, queue)
        self._persist()
        self._emit(
            "executor_started",
            plan_id=self.manager.envelope.plan_id,
            pid=self.executor.pid,
        )

    def _poll_executor(self) -> None:
        if self.executor is None or self.manager is None:
            return
        returncode = self.executor.poll()
        if returncode is None:
            return
        self._emit("executor_exited", returncode=returncode)
        if self.manager.envelope.status == "executing":
            if returncode == 0:
                self.manager.complete()
            else:
                self.manager.invalidate(f"executor_exit_{returncode}")
            self._persist()
        self.executor = None

    def request_takeover(
        self,
        reason: str = "human_dashboard_takeover",
    ) -> tuple[bool, str]:
        """Cancel future automation while preserving current Mixxx playback."""
        with self._lock:
            self._poll_executor()
            if self.manager is None:
                return False, "no_dashboard_owned_plan"
            if self.manager.envelope.status != "executing":
                return False, f"plan_status_{self.manager.envelope.status}"
            if self.executor is None:
                return False, "transition_executor_not_active"
            self.manager.invalidate(reason)
            self._persist()
            self._emit(
                "takeover_requested",
                plan_id=self.manager.envelope.plan_id,
                reason=reason,
                preserve_playback=True,
            )
            return True, reason

    def update(
        self,
        state: Any,
        events: tuple[Any, ...],
    ) -> None:
        with self._lock:
            self._update_locked(state, events)

    def _update_locked(
        self,
        state: Any,
        events: tuple[Any, ...],
    ) -> None:
        performance = PerformanceSnapshot.from_observer_state(state)
        if self.manager is None:
            reason = self._wait_reason(performance)
            if reason:
                if reason != self.last_wait_reason:
                    self._emit("waiting", reason=reason)
                    self.last_wait_reason = reason
                return
            envelope = PlanEnvelope.from_rolling_selection(
                self.selection,
                performance,
            )
            self.manager = PlanLifecycleManager(
                envelope,
                FileAutomationScheduler(),
            )
            self.manager.mark_ready(performance, self.queue_store.load())
            self._persist()
            self._emit(
                "plan_ready",
                plan_id=envelope.plan_id,
                performance_revision=performance.revision,
                queue_revision=envelope.bound_queue_revision,
            )
            if self.launch_executor:
                self._launch(performance)

        assert self.manager is not None
        before = self.manager.envelope.status
        self.manager.handle_queue(self.queue_store.load())
        for event in events:
            payload = json.loads(event.to_json())
            source_deck = self.manager.envelope.tracks[0].deck
            payload["source_beat_index"] = performance.decks[
                source_deck
            ].beat_index
            self._write_safe_seek_rebase(performance, payload)
            self.manager.handle_observer_event(payload)
        self.manager.reconcile_performance(performance)
        if self.manager.envelope.status != before:
            self._persist()
            self._emit(
                "lifecycle_changed",
                from_status=before,
                to_status=self.manager.envelope.status,
                reason=self.manager.envelope.invalidation_reason,
            )
        self._poll_executor()

    def stop(self) -> None:
        with self._lock:
            if self.manager and self.manager.envelope.status == "executing":
                self.manager.invalidate("supervisor_stopped")
                self._persist()
            if self.executor:
                try:
                    self.executor.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.executor.terminate()
                    self.executor.wait(timeout=5)
            self._poll_executor()

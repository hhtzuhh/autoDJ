#!/usr/bin/env python3
"""Bridge the verified workspace Mixxx observer into the React state contract.

This development bridge intentionally imports the still-private experiment
implementation from a sibling workspace. It proves the live product data path
without claiming that the observer has already been fully promoted into the
public repository.
"""

from __future__ import annotations

import argparse
from collections import deque
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
from transition_pipeline import (  # noqa: E402
    AnalysisCatalog,
    PairAnalysisCache,
    QueueSnapshot,
    TransitionPipeline,
    VersionedQueue,
)
from meta_planner import (  # noqa: E402
    MINIMUM_DWELL_BEATS,
    MetaConfig,
    OpenAICompatibleTransport,
    build_rolling_candidates,
    build_three_transition_candidates,
    load_dotenv,
    select_future_fallback,
    select_with_meta,
)
from set_builder import SetBuilderService  # noqa: E402
from clean_break import compile_clean_break_plan, select_clean_break_with_meta  # noqa: E402
from set_activation import (  # noqa: E402
    load_prepared_activation,
    prepare_activation,
    promote_prepared_activation,
)


DEFAULT_WORKSPACE = Path(__file__).resolve().parents[2]
DEFAULT_PORT = 8765
ACCENTS = ("cyan", "violet", "amber")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def display_title(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}_-_", "", stem)
    stem = re.sub(r"_-_www\..*$", "", stem)
    return stem.replace("_", " ").strip() or "Unknown track"


def clock_time() -> str:
    return datetime.now().astimezone().strftime("%I:%M:%S %p").lstrip("0")


def find_track_deck(performance: Any, track_id: str) -> tuple[int, Any] | None:
    """Resolve the observer dictionary key; DeckState has no deck field."""
    return next(
        (
            (int(deck_number), deck)
            for deck_number, deck in performance.decks.items()
            if deck.track_id == track_id
        ),
        None,
    )


class LiveStateStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.lock = threading.Lock()
        self.state: dict[str, Any] | None = None
        self.error: str | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=20)
        self.raw_performance: Any | None = None
        self.current_track_id: str | None = None
        self.supervisor: Any | None = None
        self.buffered_meta_plan: dict[str, Any] | None = None
        self.future_transition_plan: dict[str, Any] | None = None
        self.future_plan_in_progress = False
        self.lookahead_in_progress: set[str] = set()
        self.human_replan_started_for: set[str] = set()
        self.transition_overrides: dict[tuple[str, str], dict[str, Any]] = {}
        self.action_lock = threading.RLock()
        self.auto_chain_cancel = threading.Event()
        self.auto_chain_thread: threading.Thread | None = None
        self.auto_chain_started_for: set[str] = set()
        self.sampler_slots: dict[str, int] = {}
        self.continuous_set_active = False
        self.auto_arm_enabled = False
        self.continuity_guard_tracks: set[str] = set()
        self.continuity_guard_last: dict[str, float] = {}
        self.activation_in_progress = False
        self.queue = load_json(
            workspace / "artifacts/live-performance/queue-state-v1.json"
        )
        self.set_list_manifest_path = (
            workspace / "artifacts/live-performance/tonight-set-list-v1.json"
        )
        self.analysis_catalog = AnalysisCatalog.from_root(
            workspace / "artifacts/time-map"
        )
        queue_snapshot = QueueSnapshot(
            revision=int(self.queue["revision"]),
            ordered_track_ids=tuple(self.queue["ordered_track_ids"]),
            updated_at=str(self.queue["updated_at"]),
        )
        self.transition_pipeline = TransitionPipeline(
            VersionedQueue(self.analysis_catalog, queue_snapshot),
            PairAnalysisCache(
                workspace / "artifacts/live-performance/dashboard-pairs",
                self.analysis_catalog,
            ),
        )
        _, self.transition_edges = self.transition_pipeline.set_queue(
            queue_snapshot.ordered_track_ids
        )
        self._seed_standby_overrides()
        self.selection = self._load_selection()
        self.envelope = self._load_envelope()
        if not self.envelope or self.envelope.get("status") not in {
            "completed",
            "invalidated",
            "cancelled",
        }:
            self._restore_following_override()
            self._restore_transition_buffer_overrides()
        self.catalog = self._load_catalog()
        self.set_builder = SetBuilderService(workspace)
        self.sampler_slots = self._restore_sampler_slot_registry()

    def _load_selection(self) -> dict[str, Any]:
        candidates = (
            self.workspace
            / "artifacts/live-performance/dashboard-meta-selection-v1.json",
            self.workspace
            / "artifacts/live-performance/live-replan-selection-v1.json",
            self.workspace
            / "artifacts/live-performance/rolling-selection-v1.json",
        )
        for path in candidates:
            if path.exists():
                return load_json(path)
        raise FileNotFoundError("no live or rolling selection artifact is available")

    def _load_envelope(self) -> dict[str, Any] | None:
        runtime = self.workspace / "artifacts/live-performance"
        for path in (
            runtime / "dashboard-active-envelope-v1.json",
            runtime / "live-replan-envelope-v1.json",
        ):
            if path.exists():
                return load_json(path)
        return None

    def _load_catalog(self) -> dict[str, dict[str, Any]]:
        root = self.workspace / "artifacts/time-map"
        catalog: dict[str, dict[str, Any]] = {}
        for path in sorted(root.glob("*/track-analysis-v1.json")):
            payload = load_json(path)
            catalog[str(payload["audio_sha256"])] = payload
        return catalog

    def _verify_set_list_edge(self, source_id: str, target_id: str) -> None:
        if not self.set_list_manifest_path.exists():
            raise RuntimeError("exact_set_list_manifest_missing")
        manifest = load_json(self.set_list_manifest_path)
        if int(manifest.get("queue_revision", -1)) != int(self.queue["revision"]):
            raise RuntimeError("exact_set_list_revision_mismatch")
        track_ids = [str(row["track_id"]) for row in manifest["ordered_tracks"]]
        expected = [str(value) for value in self.queue["ordered_track_ids"]]
        if track_ids != expected:
            raise RuntimeError("exact_set_list_order_mismatch")
        source_index = track_ids.index(source_id)
        if source_index + 1 >= len(track_ids) or track_ids[source_index + 1] != target_id:
            raise RuntimeError("exact_set_list_edge_mismatch")

    def _seed_standby_overrides(self) -> None:
        """Expose a deterministic emergency candidate for every analyzed edge."""
        for edge in self.transition_edges:
            payload = self.transition_pipeline.cache.payload(
                edge.source_track_id, edge.target_track_id
            )
            clean = payload.get("clean_break_pairs", [])
            sync = payload.get("pairs", [])
            candidate = clean[0] if clean else sync[0] if sync else None
            if candidate is None:
                continue
            bars = 0 if clean else int(candidate["transition_bar_options"][0])
            self.transition_overrides[(edge.source_track_id, edge.target_track_id)] = {
                "status": "standby",
                "standbyReady": True,
                "selectedBy": "deterministic_fallback",
                "reason": "Emergency fallback is locally validated; Meta has not selected the live plan.",
                "sourceExitBeat": int(candidate["a_exit"]["beat_index"]),
                "targetEntryBeat": int(candidate["b_entry"]["beat_index"]),
                "bars": bars,
            }

    def set_error(self, message: str) -> None:
        with self.lock:
            self.error = message

    def _executor_running(self) -> bool:
        executor = self.supervisor.executor if self.supervisor else None
        return executor is not None and executor.poll() is None

    def snapshot(self) -> tuple[dict[str, Any] | None, str | None]:
        with self.lock:
            state = deepcopy(self.state) if self.state is not None else None
            error = self.error
        if state is not None:
            state["setBuilder"] = self.set_builder.snapshot()
            state["setBuilder"]["activation"] = self._activation_snapshot()
        return state, error

    def _activation_snapshot(self) -> dict[str, Any] | None:
        path = self.workspace / "artifacts/live-performance/prepared-activation-v1.json"
        if not path.exists():
            return None
        try:
            payload = load_prepared_activation(path)
            queue = payload["queue"]
            builder = self.set_builder.snapshot()
            if (
                builder.get("status") != "ready"
                or not builder.get("activationReady")
                or [str(value) for value in builder.get("orderedTrackIds", [])]
                != [str(value) for value in queue["ordered_track_ids"]]
            ):
                return None
            return {
                "status": payload["status"],
                "queueRevision": int(queue["revision"]),
                "trackCount": len(queue["ordered_track_ids"]),
                "playlist": str(payload["playlist_path"]),
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def request_set_build(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self._executor_running():
            return 409, {"error": "cannot replace the prepared set while a transition executes"}
        try:
            prompt = str(payload.get("prompt", ""))
            count = int(payload.get("count", 10))
            return 202, self.set_builder.build(prompt, count)
        except (TypeError, ValueError) as error:
            return 400, {"error": str(error)}
        except Exception as error:
            return 502, {"error": str(error)}

    def request_queue_reorder(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if self._executor_running():
            return 409, {"error": "queue is locked while a transition executes"}
        try:
            ordered = payload.get("orderedTrackIds")
            if not isinstance(ordered, list):
                raise ValueError("orderedTrackIds must be a list")
            return 200, self.set_builder.reorder(ordered)
        except (TypeError, ValueError) as error:
            return 400, {"error": str(error)}

    def request_activate_set(self) -> tuple[int, dict[str, Any]]:
        with self.action_lock:
            if self._executor_running():
                return 409, {"error": "cannot activate a set while a transition executes"}
            if self.raw_performance and any(
                deck.play is True for deck in self.raw_performance.decks.values()
            ):
                return 409, {"error": "stop both Decks before preparing a new live set"}
            try:
                prepared = prepare_activation(
                    self.workspace,
                    self.set_builder.snapshot(),
                    active_queue_revision=int(self.queue["revision"]),
                )
            except ValueError as error:
                return 409, {"error": str(error)}
            self._append_system_event(
                "Mixxx set prepared",
                "Hash-verified playlist written. Open it in Mixxx and select the first row before Start set.",
            )
            return 200, prepared.public()

    def _wait_for_loaded_track(
        self,
        deck_number: int,
        expected_id: str,
        previous_id: str | None,
        *,
        timeout: float = 4.0,
    ) -> bool:
        deadline = time.monotonic() + timeout
        saw_change = False
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with self.lock:
                performance = self.raw_performance
            if performance is None:
                continue
            observed_id = performance.decks[deck_number].track_id
            if observed_id == expected_id:
                return True
            if observed_id is None or observed_id != previous_id:
                saw_change = True
            if saw_change and observed_id not in (None, expected_id):
                return False
        return False

    def _wait_for_loaded_track_after_revision(
        self,
        deck_number: int,
        expected_id: str,
        previous_revision: int,
        *,
        timeout: float = 4.0,
    ) -> bool:
        """Require a fresh Mixxx load event before accepting track identity."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.05)
            with self.lock:
                performance = self.raw_performance
            if performance is None:
                continue
            if int(performance.revision) <= previous_revision:
                continue
            observed_id = performance.decks[deck_number].track_id
            if observed_id == expected_id:
                return True
            # A Mixxx load publishes track_samples, sample_rate, and file_bpm as
            # separate control updates.  Ignore an intermediate identity and wait
            # for the complete fingerprint (or the timeout) before rejecting it.
        return False

    @staticmethod
    def _sampler_slot_registry(track_ids: list[str]) -> dict[str, int]:
        if len(track_ids) > 10:
            raise ValueError("live queue supports at most 10 staged sampler slots")
        if len(set(track_ids)) != len(track_ids):
            raise ValueError("live queue cannot stage duplicate track identities")
        return {track_id: index + 1 for index, track_id in enumerate(track_ids)}

    def _restore_sampler_slot_registry(self) -> dict[str, int]:
        """Recover the exact staged queue mapping after a bridge restart."""
        activation_path = (
            self.workspace
            / "artifacts/live-performance/prepared-activation-v1.json"
        )
        if not activation_path.exists() or not self.set_list_manifest_path.exists():
            return {}
        try:
            activation = load_prepared_activation(activation_path)
            activation_queue = activation["queue"]
            activation_ids = [
                str(value) for value in activation_queue["ordered_track_ids"]
            ]
            active_ids = [str(value) for value in self.queue["ordered_track_ids"]]
            manifest = load_json(self.set_list_manifest_path)
            manifest_ids = [
                str(row["track_id"]) for row in manifest["ordered_tracks"]
            ]
            revisions = {
                int(activation_queue["revision"]),
                int(self.queue["revision"]),
                int(manifest["queue_revision"]),
            }
            if len(revisions) != 1 or activation_ids != active_ids or manifest_ids != active_ids:
                return {}
            return self._sampler_slot_registry(active_ids)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _install_active_queue(self, queue: dict[str, Any]) -> None:
        self.queue = queue
        self.set_list_manifest_path = (
            self.workspace / "artifacts/live-performance/tonight-set-list-v1.json"
        )
        self.analysis_catalog = AnalysisCatalog.from_root(
            self.workspace / "artifacts/time-map"
        )
        snapshot = QueueSnapshot(
            revision=int(queue["revision"]),
            ordered_track_ids=tuple(str(value) for value in queue["ordered_track_ids"]),
            updated_at=str(queue["updated_at"]),
        )
        self.transition_pipeline = TransitionPipeline(
            VersionedQueue(self.analysis_catalog, snapshot),
            PairAnalysisCache(
                self.workspace / "artifacts/live-performance/dashboard-pairs",
                self.analysis_catalog,
            ),
        )
        _, self.transition_edges = self.transition_pipeline.set_queue(
            snapshot.ordered_track_ids
        )
        self.catalog = self._load_catalog()
        self.transition_overrides.clear()
        self._seed_standby_overrides()
        self.selection = {}
        self.buffered_meta_plan = None
        self.future_transition_plan = None
        self.future_plan_in_progress = False
        self.lookahead_in_progress.clear()
        self.supervisor = None
        self.envelope = None
        self.auto_chain_started_for.clear()
        self.human_replan_started_for.clear()

    def request_start_set(self) -> tuple[int, dict[str, Any]]:
        """Stage the queue, require an initial Meta plan, then start Deck 1."""
        with self.action_lock:
            if self._executor_running():
                return 409, {"error": "cannot start a set while a transition executes"}
            if self.raw_performance is None:
                return 503, {"error": "Mixxx performance snapshot is unavailable"}
            if any(deck.play is True for deck in self.raw_performance.decks.values()):
                return 409, {"error": "stop both Decks before starting a prepared set"}
            activation_path = (
                self.workspace / "artifacts/live-performance/prepared-activation-v1.json"
            )
            if not activation_path.exists():
                return 409, {"error": "prepare the Mixxx set before starting it"}
            try:
                activation = load_prepared_activation(activation_path)
                builder = self.set_builder.snapshot()
                if (
                    builder.get("status") != "ready"
                    or not builder.get("activationReady")
                    or [str(value) for value in builder.get("orderedTrackIds", [])]
                    != [
                        str(value)
                        for value in activation["queue"]["ordered_track_ids"]
                    ]
                ):
                    return 409, {
                        "error": "prepared_playlist_is_stale",
                        "message": "Prepare Mixxx set again after the latest queue change.",
                    }
                ordered_ids = [
                    str(value) for value in activation["queue"]["ordered_track_ids"]
                ]
                first_id, second_id = ordered_ids[:2]
                sampler_slots = self._sampler_slot_registry(ordered_ids)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                return 409, {"error": f"invalid prepared set: {error}"}

            mixxx_root = self.workspace / "experiments/mixxx-control"
            if str(mixxx_root) not in sys.path:
                sys.path.insert(0, str(mixxx_root))
            self.activation_in_progress = True
            try:
                import mido
                from probe import (
                    port_name,
                    send_library_value,
                    send_load_sampler_value,
                    send_stage_sampler_value,
                    send_sync_value,
                    send_value,
                )

                with mido.open_output(port_name()) as output:
                    for index, track_id in enumerate(ordered_ids):
                        slot = sampler_slots[track_id]
                        send_stage_sampler_value(output, slot)
                        time.sleep(0.35)
                        with self.lock:
                            previous_revision = int(self.raw_performance.revision)
                        send_load_sampler_value(output, 1, slot)
                        if not self._wait_for_loaded_track_after_revision(
                            1, track_id, previous_revision
                        ):
                            return 409, {
                                "error": "mixxx_sampler_staging_identity_mismatch",
                                "track_id": track_id,
                                "sampler_slot": slot,
                                "message": (
                                    "Open the generated playlist and select its first row; "
                                    "the exact queue registry was not activated."
                                ),
                            }
                        if index + 1 < len(ordered_ids):
                            send_library_value(output, "next")
                            time.sleep(0.15)

                    for deck_number, track_id in ((1, first_id), (2, second_id)):
                        with self.lock:
                            previous_revision = int(self.raw_performance.revision)
                        send_load_sampler_value(
                            output, deck_number, sampler_slots[track_id]
                        )
                        if not self._wait_for_loaded_track_after_revision(
                            deck_number, track_id, previous_revision
                        ):
                            return 409, {
                                "error": "mixxx_sampler_registry_final_load_failed",
                                "track_id": track_id,
                                "deck": deck_number,
                                "message": "The verified queue stayed stopped and was not activated.",
                            }

                previous_queue = deepcopy(self.queue)
                previous_current_id = self.current_track_id
                previous_selection = deepcopy(self.selection)
                previous_supervisor = self.supervisor
                previous_envelope = deepcopy(self.envelope)
                previous_overrides = deepcopy(self.transition_overrides)
                previous_sampler_slots = dict(self.sampler_slots)
                previous_buffer = deepcopy(self.buffered_meta_plan)
                previous_future_plan = deepcopy(self.future_transition_plan)
                previous_continuous_set_active = self.continuous_set_active
                try:
                    self._install_active_queue(activation["queue"])
                    self.sampler_slots = sampler_slots
                    self.current_track_id = first_id
                    self.continuous_set_active = True
                    planning_deadline = time.monotonic() + 3.0
                    while True:
                        plan_status, plan_payload = self.request_meta_plan()
                        if plan_status == 200:
                            break
                        if (
                            plan_payload.get("error")
                            == "current_source_deck_is_not_observable"
                            and time.monotonic() < planning_deadline
                        ):
                            time.sleep(0.1)
                            continue
                        self._install_active_queue(previous_queue)
                        self.current_track_id = previous_current_id
                        self.selection = previous_selection
                        self.supervisor = previous_supervisor
                        self.envelope = previous_envelope
                        self.transition_overrides = previous_overrides
                        self.sampler_slots = previous_sampler_slots
                        self.buffered_meta_plan = previous_buffer
                        self.future_transition_plan = previous_future_plan
                        self.continuous_set_active = previous_continuous_set_active
                        self._write_dashboard_selection(previous_selection)
                        return 409, {
                            "error": "initial_meta_plan_required",
                            "detail": plan_payload.get("error"),
                            "message": (
                                "Meta did not produce the initial three-track buffer; "
                                "both Decks remain stopped."
                            ),
                        }
                    queue = promote_prepared_activation(self.workspace, activation)
                except Exception:
                    self._install_active_queue(previous_queue)
                    self.current_track_id = previous_current_id
                    self.selection = previous_selection
                    self.supervisor = previous_supervisor
                    self.envelope = previous_envelope
                    self.transition_overrides = previous_overrides
                    self.sampler_slots = previous_sampler_slots
                    self.buffered_meta_plan = previous_buffer
                    self.future_transition_plan = previous_future_plan
                    self.continuous_set_active = previous_continuous_set_active
                    self._write_dashboard_selection(previous_selection)
                    raise
                with mido.open_output(port_name()) as output:
                    for deck_number in (1, 2):
                        send_sync_value(output, deck_number, "enabled", False)
                        send_value(output, str(deck_number), "rate", 0.0)
                        send_value(output, str(deck_number), "volume", 0.75)
                    send_value(output, "master", "crossfader", 0.0)
                    send_value(output, "1", "play", 1.0)
                if self.auto_arm_enabled:
                    self.auto_chain_cancel.clear()
                    threading.Thread(
                        target=self._arm_ready_plan_with_retry,
                        name="idj-initial-auto-arm",
                        daemon=True,
                    ).start()
            except Exception as error:
                return 502, {
                    "error": "mixxx_set_start_failed",
                    "detail": f"{type(error).__name__}: {error}",
                }
            finally:
                self.activation_in_progress = False
            self._append_system_event(
                "Prepared set started",
                "The initial Meta A→B→C plan was ready before Deck 1 started.",
            )
            return 202, {
                "accepted": True,
                "queueRevision": int(queue["revision"]),
                "planning": False,
                "plan_id": plan_payload["plan_id"],
                "message": "Initial three-track Meta buffer is ready; Deck 1 started.",
            }

    def request_arm(self) -> tuple[int, dict[str, Any]]:
        with self.action_lock:
            if self.raw_performance is None:
                return 503, {"error": "Mixxx performance snapshot is unavailable"}
            if self._executor_running():
                return 409, {"error": "a transition executor is already active"}
            if not isinstance(self.selection.get("next_executable_plan"), dict):
                return 409, {
                    "error": "meta_plan_still_pending",
                    "message": "Playback is continuing while Meta prepares the first transition.",
                }

            live_selection = deepcopy(self.selection)
            plan = live_selection["next_executable_plan"]
            unique = f"dashboard-{int(time.time() * 1000)}"
            plan["plan_id"] = f"{plan['plan_id']}-{unique}"
            live_selection["source_window_id"] = (
                f"{live_selection['source_window_id']}-{unique}"
            )

            live_root = self.workspace / "experiments/live-performance"
            if str(live_root) not in sys.path:
                sys.path.insert(0, str(live_root))
            from lifecycle_supervisor import LifecycleSupervisor
            from plan_lifecycle import PerformanceSnapshot
            from queue_service import QueueStore

            runtime = self.workspace / "artifacts/live-performance"
            supervisor = LifecycleSupervisor(
                selection=live_selection,
                queue_store=QueueStore(runtime / "queue-state-v1.json"),
                envelope_path=runtime / "dashboard-active-envelope-v1.json",
                executor_plan_path=runtime / "dashboard-executor-plan-v1.json",
                launch_executor=True,
            )
            performance = PerformanceSnapshot.from_observer_state(
                self.raw_performance
            )
            reason = supervisor._wait_reason(performance)
            if reason:
                return 409, {
                    "error": reason,
                    "message": "Mixxx was not changed because local preconditions failed.",
                }
            self.selection = live_selection
            self.envelope = None
            self.supervisor = supervisor
            self._schedule_future_transition_plan()
            return 202, {
                "accepted": True,
                "plan_id": plan["plan_id"],
                "message": "Plan passed preflight and is waiting for the observer lifecycle update.",
            }

    def request_auto_arm(self, enabled: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(enabled, bool):
            return 400, {"error": "enabled_must_be_boolean"}
        with self.action_lock:
            self.auto_arm_enabled = enabled
            self._append_system_event(
                "Auto arm enabled" if enabled else "Auto arm disabled",
                (
                    "Ready transitions will arm automatically after deck rotation."
                    if enabled
                    else "Meta keeps the ready buffer full; every transition now requires human approval."
                ),
            )
            arm_payload: dict[str, Any] | None = None
            if (
                enabled
                and isinstance(self.selection.get("next_executable_plan"), dict)
                and not self._executor_running()
                and self.supervisor is None
            ):
                arm_status, candidate = self.request_arm()
                if arm_status == 202:
                    arm_payload = candidate
            return 200, {
                "accepted": True,
                "auto_arm_enabled": self.auto_arm_enabled,
                "plan_armed": arm_payload is not None,
                "message": (
                    "Auto arm is on. The current ready plan was armed."
                    if arm_payload is not None
                    else "Auto arm is on; the next ready plan will arm automatically."
                    if enabled
                    else "Auto arm is off. Planning continues, but execution requires Approve & arm."
                ),
            }

    def _schedule_future_transition_plan(self) -> bool:
        """Re-optimize the three transitions after the committed active edge."""
        if self.future_plan_in_progress:
            return False
        active = self.selection.get("next_executable_plan")
        if not isinstance(active, dict):
            return False
        queue_ids = [str(value) for value in self.queue["ordered_track_ids"]]
        source_id = str(active["deck_b"]["audio_sha256"])
        if source_id not in queue_ids or queue_ids.index(source_id) + 3 >= len(queue_ids):
            return False
        existing = self.future_transition_plan
        if (
            isinstance(existing, dict)
            and existing.get("based_on_active_plan_id") == active.get("plan_id")
        ):
            return False
        self.future_plan_in_progress = True
        threading.Thread(
            target=self._future_transition_plan_worker,
            args=(str(active["plan_id"]),),
            name=f"idj-meta-future-{source_id[:8]}",
            daemon=True,
        ).start()
        return True

    def _future_transition_plan_worker(self, active_plan_id: str) -> None:
        try:
            with self.action_lock:
                active = deepcopy(self.selection.get("next_executable_plan"))
                if not isinstance(active, dict) or active.get("plan_id") != active_plan_id:
                    return
                queue_ids = [str(value) for value in self.queue["ordered_track_ids"]]
                source_id = str(active["deck_b"]["audio_sha256"])
                source_index = queue_ids.index(source_id)
                track_ids = tuple(queue_ids[source_index : source_index + 4])
                if len(track_ids) != 4:
                    return
                pairings = tuple(
                    self.transition_pipeline.cache.payload(
                        track_ids[index], track_ids[index + 1]
                    )
                    for index in range(3)
                )
                minimum_first_exit = (
                    int(active["deck_b"]["cue_beat_index"])
                    + int(active.get("mix", {}).get("bars", 0)) * 4
                    + MINIMUM_DWELL_BEATS
                )
                candidates = [
                    candidate
                    for candidate in build_three_transition_candidates(pairings)
                    if int(
                        candidate["transitions"][0]["source_exit_beat_index"]
                    )
                    >= minimum_first_exit
                ]
                candidates = [
                    candidate
                    for candidate in candidates
                    if self._preserves_shifted_three_transition_window(
                        candidate, source_index, queue_ids
                    )
                ]
                if not candidates:
                    raise RuntimeError("no_feasible_future_three_transition_window")
                queue_revision = int(self.queue["revision"])
                source_deck = int(active["deck_b"]["deck"])
                target_deck = int(active["deck_a"]["deck"])
                load_dotenv(BACKEND_ROOT.parent / ".env")
                load_dotenv(self.workspace / "experiments/meta-dj-agent/.env")
                config = MetaConfig.from_environment()
                self._append_system_event(
                    "Future Meta window started",
                    (
                        f"{active_plan_id} stays locked; {config.model} is jointly "
                        "planning the next three transitions."
                    ),
                )

            selection = select_with_meta(
                candidates,
                queue_revision=queue_revision,
                intent="maintain_energy",
                track_ids=track_ids,
                model=config.model,
                transport=OpenAICompatibleTransport(config),
            )
            compiled = self._compile_three_transition_candidate(
                track_ids,
                selection,
                config.model,
                source_deck,
                target_deck,
            )
            compiled["selection"] = "meta_future_three_transition_window"
            compiled["based_on_active_plan_id"] = active_plan_id
            with self.action_lock:
                latest = self.selection.get("next_executable_plan", {})
                if (
                    latest.get("plan_id") != active_plan_id
                    or int(self.queue["revision"]) != queue_revision
                ):
                    raise RuntimeError("active_plan_changed_during_future_meta_call")
                self.future_transition_plan = compiled
                for edge in compiled["transition_buffer"]:
                    self.transition_overrides[
                        (str(edge["source_track_id"]), str(edge["target_track_id"]))
                    ] = {
                        "status": "ready",
                        "standbyReady": True,
                        "selectedBy": "meta",
                        "reason": "Jointly optimized inside the next three-transition Meta window.",
                        "sourceExitBeat": int(edge["source_exit_beat_index"]),
                        "targetEntryBeat": int(edge["target_entry_beat_index"]),
                        "bars": int(edge["transition_bars"]),
                    }
                output = (
                    self.workspace
                    / "artifacts/live-performance/dashboard-future-meta-selection-v1.json"
                )
                temporary = output.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(compiled, indent=2, ensure_ascii=False, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                temporary.replace(output)
                self._append_system_event(
                    "Future three-transition window ready",
                    (
                        f"{config.model} jointly selected "
                        f"{track_ids[0][:8]}→{track_ids[1][:8]}→"
                        f"{track_ids[2][:8]}→{track_ids[3][:8]}."
                    ),
                )
        except Exception as error:
            self._append_system_event(
                "Future Meta window unavailable",
                f"{type(error).__name__}: {error}",
            )
        finally:
            self.future_plan_in_progress = False

    def _promote_future_transition_plan(
        self,
        source_id: str,
        target_id: str,
        source_deck: int,
        target_deck: int,
        live_beat: int,
    ) -> tuple[int, dict[str, Any]] | None:
        future = self.future_transition_plan
        if not isinstance(future, dict):
            return None
        buffer = future.get("transition_buffer")
        if not isinstance(buffer, list) or not buffer:
            return None
        first = buffer[0]
        if (
            future.get("queue_revision") != int(self.queue["revision"])
            or first.get("source_track_id") != source_id
            or first.get("target_track_id") != target_id
        ):
            return None
        if int(first["source_exit_beat_index"]) <= live_beat + 16:
            self.future_transition_plan = None
            return None
        plan = future["next_executable_plan"]
        if (
            int(plan["deck_a"]["deck"]) != source_deck
            or int(plan["deck_b"]["deck"]) != target_deck
        ):
            return None
        self.selection = future
        self.future_transition_plan = None
        self.envelope = None
        self.supervisor = None
        self.transition_overrides[(source_id, target_id)] = {
            "status": "ready",
            "standbyReady": True,
            "selectedBy": "meta",
            "reason": "Promoted from the jointly optimized future Meta window.",
        }
        self._restore_transition_buffer_overrides()
        self._write_dashboard_selection(future)
        return 200, {
            "accepted": True,
            "status": "ready",
            "selected_by": "meta",
            "model_called": False,
            "plan_id": plan["plan_id"],
            "message": "Future three-transition plan promoted without cue-time inference.",
        }

    def _arm_ready_plan_with_retry(
        self,
        *,
        timeout: float = 4.0,
    ) -> tuple[int, dict[str, Any]]:
        """Retry only observer-settling failures after a verified deck load."""
        transient = {
            "expected_tracks_not_loaded",
            "source_beat_unknown",
            "source_deck_not_playing",
            "target_transport_unknown",
        }
        deadline = time.monotonic() + timeout
        while True:
            if not self.auto_arm_enabled or self.auto_chain_cancel.is_set():
                return 409, {"error": "auto_arm_cancelled"}
            status, payload = self.request_arm()
            if status == 202:
                return status, payload
            if payload.get("error") not in transient or time.monotonic() >= deadline:
                return status, payload
            time.sleep(0.1)

    def request_meta_plan(self) -> tuple[int, dict[str, Any]]:
        with self.action_lock:
            if self.raw_performance is None or self.current_track_id is None:
                return 503, {"error": "Mixxx performance snapshot is unavailable"}
            if self._executor_running():
                return 409, {"error": "a transition executor is already active"}
            queue_ids = [str(value) for value in self.queue["ordered_track_ids"]]
            if self.current_track_id not in queue_ids:
                return 409, {"error": "current_track_is_not_in_queue"}
            current_index = queue_ids.index(self.current_track_id)
            if current_index + 1 >= len(queue_ids):
                return 409, {"error": "queue_has_no_next_track"}
            source_id, target_id = queue_ids[current_index : current_index + 2]
            source_match = find_track_deck(self.raw_performance, source_id)
            if source_match is None or source_match[1].beat_index is None:
                return 409, {"error": "current_source_deck_is_not_observable"}
            source_deck_number, source_deck = source_match
            target_deck_number = 1 if source_deck_number == 2 else 2
            future = self._promote_future_transition_plan(
                source_id,
                target_id,
                source_deck_number,
                target_deck_number,
                int(source_deck.beat_index),
            )
            if future is not None:
                return future
            buffered = self._promote_buffered_meta_plan(
                source_id,
                target_id,
                source_deck_number,
                target_deck_number,
                int(source_deck.beat_index),
            )
            if buffered is not None:
                return buffered
            draft = self.selection.get("following_feasible_draft")
            if (
                isinstance(draft, dict)
                and draft.get("status") == "feasible_not_armed"
                and draft.get("source_track_id") == source_id
                and draft.get("target_track_id") == target_id
            ):
                transition_buffer = self.selection.get("transition_buffer")
                buffered_edge = (
                    transition_buffer[1]
                    if isinstance(transition_buffer, list)
                    and len(transition_buffer) >= 2
                    else draft
                )
                if self._edge_preserves_future_window(
                    buffered_edge, current_index, queue_ids
                ):
                    return self._promote_following_draft(source_id, target_id)
                self.selection["transition_buffer"] = []
                self.selection["following_feasible_draft"] = None
                self._append_system_event(
                    "Buffered cue rejected",
                    (
                        "The preserved cue would leave less than "
                        f"{MINIMUM_DWELL_BEATS} beats before the next feasible "
                        "transition; replanning forward from the live position."
                    ),
                )
            immediate = self.transition_pipeline.cache.payload(source_id, target_id)
            if current_index + 3 < len(queue_ids):
                track_ids_4 = tuple(queue_ids[current_index : current_index + 4])
                pairings = (
                    immediate,
                    self.transition_pipeline.cache.payload(
                        track_ids_4[1], track_ids_4[2]
                    ),
                    self.transition_pipeline.cache.payload(
                        track_ids_4[2], track_ids_4[3]
                    ),
                )
                candidates_4 = [
                    candidate
                    for candidate in build_three_transition_candidates(pairings)
                    if int(
                        candidate["transitions"][0]["source_exit_beat_index"]
                    )
                    > int(source_deck.beat_index) + 16
                ]
                candidates_4 = [
                    candidate
                    for candidate in candidates_4
                    if self._preserves_shifted_three_transition_window(
                        candidate, current_index, queue_ids
                    )
                ]
                if candidates_4:
                    return self._plan_three_transition_buffer(
                        track_ids_4,
                        candidates_4,
                        source_deck_number,
                        target_deck_number,
                    )
            if not immediate.get("pairs") and immediate.get("clean_break_pairs"):
                return self._plan_clean_break(
                    source_id,
                    target_id,
                    source_deck_number,
                    target_deck_number,
                    int(source_deck.beat_index),
                    immediate["clean_break_pairs"],
                )
            if current_index + 2 >= len(queue_ids):
                return self._promote_following_draft(source_id, target_id)
            following_id = queue_ids[current_index + 2]
            track_ids = (source_id, target_id, following_id)
            following = self.transition_pipeline.cache.payload(target_id, following_id)
            all_candidates = build_rolling_candidates(immediate, following)
            candidates = [
                row
                for row in all_candidates
                if int(row["a_exit_beat_index"]) > int(source_deck.beat_index) + 16
            ]
            if not candidates:
                self.transition_overrides[(source_id, target_id)] = {
                    "status": "needs_human",
                    "standbyReady": False,
                    "selectedBy": None,
                    "reason": "All locally validated exit windows have expired.",
                }
                return 409, {"error": "no_future_feasible_abc_candidates"}

            edge_key = (source_id, target_id)
            self.transition_overrides[edge_key] = {
                "status": "planning",
                "selectedBy": None,
                "reason": None,
            }

            load_dotenv(BACKEND_ROOT.parent / ".env")
            load_dotenv(self.workspace / "experiments/meta-dj-agent/.env")
            try:
                config = MetaConfig.from_environment()
                selection = select_with_meta(
                    candidates,
                    queue_revision=int(self.queue["revision"]),
                    intent="maintain_energy",
                    track_ids=track_ids,  # type: ignore[arg-type]
                    model=config.model,
                    transport=OpenAICompatibleTransport(config),
                )
                compiled = self._compile_meta_selection(
                    track_ids,
                    selection.candidate,
                    selection.decision.to_dict(),
                    config.model,
                    source_deck_number,
                    target_deck_number,
                )
            except Exception as error:
                latest_match = find_track_deck(self.raw_performance, source_id)
                latest_beat = (
                    int(latest_match[1].beat_index)
                    if latest_match and latest_match[1].beat_index is not None
                    else int(source_deck.beat_index)
                )
                fallback = select_future_fallback(
                    all_candidates, live_beat=latest_beat
                )
                if fallback is None:
                    self.transition_overrides[edge_key] = {
                        "status": "needs_human",
                        "standbyReady": False,
                        "selectedBy": None,
                        "reason": f"Meta failed and every standby exit expired: {type(error).__name__}",
                    }
                    return 409, {
                        "error": "meta_failed_and_fallback_expired",
                        "detail": f"{type(error).__name__}: {error}",
                    }
                compiled = self._compile_meta_selection(
                    track_ids,
                    fallback,
                    {
                        "intent": "maintain_energy",
                        "use_bass_swap": False,
                        "confidence": float(fallback["combined_score"]),
                        "reason": f"Meta unavailable ({type(error).__name__}); using the best still-future local standby.",
                    },
                    "local-fallback",
                    source_deck_number,
                    target_deck_number,
                )
                compiled["selection"] = "deterministic_fallback_after_meta_failure"
                compiled["model"] = None
                compiled["next_executable_plan"]["source"] = (
                    "deterministic_fallback_after_meta_failure"
                )
                self.selection = compiled
                self.envelope = None
                self.supervisor = None
                self.transition_overrides[edge_key] = {
                    "status": "ready",
                    "standbyReady": True,
                    "selectedBy": "deterministic_fallback",
                    "reason": compiled["model_decision"]["reason"],
                }
                self._restore_following_override()
                self._write_dashboard_selection(compiled)
                self._maintain_ready_buffer()
                self._append_system_event(
                    "Meta fallback activated",
                    "Meta planning failed; the best still-future locally validated standby is ready to arm.",
                )
                return 200, {
                    "accepted": True,
                    "status": "ready",
                    "selected_by": "deterministic_fallback",
                    "model_called": True,
                    "plan_id": compiled["next_executable_plan"]["plan_id"],
                    "message": "Meta failed; local standby passed fresh live-beat validation.",
                }

            self.selection = compiled
            self.envelope = None
            self.supervisor = None
            self.transition_overrides[edge_key] = {
                "status": "ready",
                "standbyReady": True,
                "selectedBy": "meta",
                "reason": selection.decision.reason,
            }
            self._restore_following_override()
            self._write_dashboard_selection(compiled)
            self._maintain_ready_buffer()
            return 200, {
                "accepted": True,
                "status": "ready",
                "selected_by": "meta",
                "model": config.model,
                "candidate_count": selection.candidate_count,
                "plan_id": compiled["next_executable_plan"]["plan_id"],
                "message": "Meta selected one locally feasible candidate; local compilation succeeded.",
            }

    def _preserves_shifted_three_transition_window(
        self,
        candidate: dict[str, Any],
        source_index: int,
        queue_ids: list[str],
    ) -> bool:
        """Require a feasible next horizon while allowing its last two cues to move."""
        if source_index + 4 >= len(queue_ids):
            return True
        shifted_ids = queue_ids[source_index + 1 : source_index + 5]
        shifted_pairings = tuple(
            self.transition_pipeline.cache.payload(
                shifted_ids[index], shifted_ids[index + 1]
            )
            for index in range(3)
        )
        minimum_next_exit = int(
            candidate["transitions"][0]["target_entry_beat_index"]
        ) + int(
            candidate["transitions"][0]["transition_bars"]
        ) * 4 + MINIMUM_DWELL_BEATS
        return any(
            int(row["transitions"][0]["source_exit_beat_index"])
            >= minimum_next_exit
            for row in build_three_transition_candidates(shifted_pairings)
        )

    def _edge_preserves_future_window(
        self,
        edge: dict[str, Any],
        source_index: int,
        queue_ids: list[str],
    ) -> bool:
        """Reject a cue that would require the next track to move backward."""
        if source_index + 4 >= len(queue_ids):
            return True
        shifted_ids = queue_ids[source_index + 1 : source_index + 5]
        shifted_pairings = tuple(
            self.transition_pipeline.cache.payload(
                shifted_ids[index], shifted_ids[index + 1]
            )
            for index in range(3)
        )
        minimum_exit = (
            int(edge["target_entry_beat_index"])
            + int(edge.get("transition_bars", 0)) * 4
            + MINIMUM_DWELL_BEATS
        )
        return any(
            int(candidate["transitions"][0]["source_exit_beat_index"])
            >= minimum_exit
            for candidate in build_three_transition_candidates(shifted_pairings)
        )

    def _plan_from_buffer_edge(
        self,
        edge: dict[str, Any],
        *,
        source_id: str,
        target_id: str,
        source_deck: int,
        target_deck: int,
        model: str,
        suffix: str,
    ) -> dict[str, Any]:
        if edge["mode"] == "clean_break":
            payload = self.transition_pipeline.cache.payload(source_id, target_id)
            pair = next(
                row
                for row in payload.get("clean_break_pairs", [])
                if int(row["rank"]) == int(edge["pair_rank"])
            )
            plan = compile_clean_break_plan(
                source_id=source_id,
                target_id=target_id,
                pair=pair,
                catalog=self.analysis_catalog,
                queue_revision=int(self.queue["revision"]),
                source_deck=source_deck,
                target_deck=target_deck,
                model=model,
            )
            plan["plan_id"] = f"{plan['plan_id']}-{suffix}"
            return plan
        return self._build_executable_plan(
            source_id=source_id,
            target_id=target_id,
            source_exit_beat=int(edge["source_exit_beat_index"]),
            target_entry_beat=int(edge["target_entry_beat_index"]),
            transition_bars=int(edge["transition_bars"]),
            model=model,
            source_deck=source_deck,
            target_deck=target_deck,
            plan_suffix=suffix,
            use_bass_swap=False,
        )

    @staticmethod
    def _draft_from_buffer_edge(edge: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "feasible_not_armed",
            "source_track_id": str(edge["source_track_id"]),
            "target_track_id": str(edge["target_track_id"]),
            "source_exit_beat_index": int(edge["source_exit_beat_index"]),
            "target_entry_beat_index": int(edge["target_entry_beat_index"]),
            "transition_bars": int(edge["transition_bars"]),
            "transition_mode": str(edge["mode"]),
        }

    def _restore_transition_buffer_overrides(self) -> None:
        buffer = self.selection.get("transition_buffer", [])
        for edge in buffer[1:]:
            source_id = str(edge["source_track_id"])
            target_id = str(edge["target_track_id"])
            self.transition_overrides[(source_id, target_id)] = {
                "status": "ready",
                "standbyReady": True,
                "selectedBy": "meta",
                "reason": "Preserved inside the single three-transition Meta plan.",
                "sourceExitBeat": int(edge["source_exit_beat_index"]),
                "targetEntryBeat": int(edge["target_entry_beat_index"]),
                "bars": int(edge["transition_bars"]),
            }

    def _plan_three_transition_buffer(
        self,
        track_ids: tuple[str, str, str, str],
        candidates: list[dict[str, Any]],
        source_deck: int,
        target_deck: int,
    ) -> tuple[int, dict[str, Any]]:
        edge_key = (track_ids[0], track_ids[1])
        self.transition_overrides[edge_key] = {
            "status": "planning",
            "selectedBy": None,
            "reason": None,
        }
        load_dotenv(BACKEND_ROOT.parent / ".env")
        load_dotenv(self.workspace / "experiments/meta-dj-agent/.env")
        try:
            config = MetaConfig.from_environment()
            self._append_system_event(
                "Meta API request started",
                f"Calling {config.model} once to select one plan containing three transitions.",
            )
            selection = select_with_meta(
                candidates,
                queue_revision=int(self.queue["revision"]),
                intent="maintain_energy",
                track_ids=track_ids,
                model=config.model,
                transport=OpenAICompatibleTransport(config),
            )
        except Exception as error:
            self.transition_overrides[edge_key] = {
                "status": "needs_human",
                "standbyReady": False,
                "selectedBy": None,
                "reason": f"Three-transition Meta planning failed: {type(error).__name__}",
            }
            return 409, {
                "error": "three_transition_meta_plan_failed",
                "detail": f"{type(error).__name__}: {error}",
            }

        compiled = self._compile_three_transition_candidate(
            track_ids,
            selection,
            config.model,
            source_deck,
            target_deck,
        )
        plan = compiled["next_executable_plan"]
        self.selection = compiled
        self.envelope = None
        self.supervisor = None
        self.transition_overrides[edge_key] = {
            "status": "ready",
            "standbyReady": True,
            "selectedBy": "meta",
            "reason": selection.decision.reason,
        }
        self._restore_transition_buffer_overrides()
        self._write_dashboard_selection(compiled)
        self._append_system_event(
            "Three-transition Meta plan ready",
            (
                f"{config.model} selected candidate "
                f"{selection.candidate['candidate_id']} in one API call: "
                "A→B, B→C, and C→D are preserved in one plan."
            ),
        )
        return 200, {
            "accepted": True,
            "status": "ready",
            "selected_by": "meta",
            "model": config.model,
            "candidate_count": selection.candidate_count,
            "plan_id": plan["plan_id"],
            "transition_count": 3,
            "message": "One Meta plan now contains three ready transitions.",
        }

    def _compile_three_transition_candidate(
        self,
        track_ids: tuple[str, str, str, str],
        selection: Any,
        model: str,
        source_deck: int,
        target_deck: int,
    ) -> dict[str, Any]:
        transition_buffer = [
            {
                **edge,
                "source_track_id": track_ids[index],
                "target_track_id": track_ids[index + 1],
            }
            for index, edge in enumerate(selection.candidate["transitions"])
        ]
        plan = self._plan_from_buffer_edge(
            transition_buffer[0],
            source_id=track_ids[0],
            target_id=track_ids[1],
            source_deck=source_deck,
            target_deck=target_deck,
            model=model,
            suffix=str(selection.candidate["candidate_id"]),
        )
        return {
            "schema_version": 2,
            "selection": "meta_three_transition_single_plan",
            "source_window_id": (
                f"buffer-q{self.queue['revision']}-{selection.candidate['candidate_id']}"
            ),
            "queue_revision": int(self.queue["revision"]),
            "intent": selection.decision.intent,
            "model": model,
            "planning_window_track_ids": list(track_ids),
            "planning_mode": "rolling_abcd",
            "model_decision": selection.decision.to_dict(),
            "selected_candidate": selection.candidate,
            "transition_buffer": transition_buffer,
            "next_executable_plan": plan,
            "following_feasible_draft": self._draft_from_buffer_edge(
                transition_buffer[1]
            ),
        }

    def _plan_clean_break(
        self,
        source_id: str,
        target_id: str,
        source_deck: int,
        target_deck: int,
        live_beat: int,
        candidates: list[dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        future = [
            candidate
            for candidate in candidates
            if int(candidate["a_exit"]["beat_index"]) > live_beat + 16
        ]
        edge_key = (source_id, target_id)
        if not future:
            self.transition_overrides[edge_key] = {
                "status": "needs_human",
                "standbyReady": False,
                "selectedBy": None,
                "reason": "All clean-break exit windows have expired.",
            }
            return 409, {"error": "no_future_clean_break_candidate"}
        self.transition_overrides[edge_key] = {
            "status": "planning",
            "standbyReady": True,
            "selectedBy": "deterministic_fallback",
            "reason": "Meta is selecting; a local standby remains available.",
            "sourceExitBeat": int(future[0]["a_exit"]["beat_index"]),
            "targetEntryBeat": int(future[0]["b_entry"]["beat_index"]),
            "bars": 0,
        }
        load_dotenv(BACKEND_ROOT.parent / ".env")
        load_dotenv(self.workspace / "experiments/meta-dj-agent/.env")
        try:
            config = MetaConfig.from_environment()
            pair, confidence, reason = select_clean_break_with_meta(
                future,
                queue_revision=int(self.queue["revision"]),
                source_id=source_id,
                target_id=target_id,
                transport=OpenAICompatibleTransport(config),
            )
            plan = compile_clean_break_plan(
                source_id=source_id,
                target_id=target_id,
                pair=pair,
                catalog=self.analysis_catalog,
                queue_revision=int(self.queue["revision"]),
                source_deck=source_deck,
                target_deck=target_deck,
                model=config.model,
            )
        except Exception as error:
            latest_match = find_track_deck(self.raw_performance, source_id)
            latest_beat = (
                int(latest_match[1].beat_index)
                if latest_match and latest_match[1].beat_index is not None
                else live_beat
            )
            pair = next(
                (
                    candidate
                    for candidate in candidates
                    if int(candidate["a_exit"]["beat_index"])
                    > latest_beat + 16
                ),
                None,
            )
            if pair is None:
                self.transition_overrides[edge_key] = {
                    "status": "needs_human",
                    "standbyReady": False,
                    "selectedBy": None,
                    "reason": f"Meta failed and every clean-break standby expired: {type(error).__name__}",
                }
                return 409, {
                    "error": "meta_failed_and_clean_break_fallback_expired",
                    "detail": str(error),
                }
            confidence = float(pair["heuristic_score"])
            reason = (
                f"Meta unavailable ({type(error).__name__}); using the best "
                "still-future local clean-break standby."
            )
            plan = compile_clean_break_plan(
                source_id=source_id,
                target_id=target_id,
                pair=pair,
                catalog=self.analysis_catalog,
                queue_revision=int(self.queue["revision"]),
                source_deck=source_deck,
                target_deck=target_deck,
                model="local-fallback",
            )
            plan["source"] = "deterministic_fallback_after_meta_failure"
            config = None
        compiled = {
            "schema_version": 1,
            "selection": (
                "meta_clean_break_single_call"
                if config is not None
                else "deterministic_fallback_after_meta_failure"
            ),
            "source_window_id": f"clean-q{self.queue['revision']}-{source_id[:8]}-{target_id[:8]}",
            "queue_revision": int(self.queue["revision"]),
            "intent": "clean_break",
            "model": config.model if config is not None else None,
            "planning_window_track_ids": [source_id, target_id],
            "planning_mode": "pair_clean_break",
            "model_decision": {
                "confidence": confidence,
                "reason": reason,
                "use_bass_swap": False,
            },
            "selected_candidate": pair,
            "next_executable_plan": plan,
        }
        self.selection = compiled
        self.envelope = None
        self.supervisor = None
        selected_by = "meta" if config is not None else "deterministic_fallback"
        self.transition_overrides[edge_key] = {
            "status": "ready",
            "standbyReady": True,
            "selectedBy": selected_by,
            "reason": reason,
        }
        self._write_dashboard_selection(compiled)
        self._maintain_ready_buffer()
        return 200, {
            "accepted": True,
            "status": "ready",
            "selected_by": selected_by,
            "model": config.model if config is not None else None,
            "candidate_count": min(10, len(future)),
            "plan_id": plan["plan_id"],
            "message": (
                "Meta selected a clean-break candidate; native tempos remain unchanged."
                if config is not None
                else "Meta failed; local clean-break standby passed fresh live-beat validation."
            ),
        }

    def _maintain_ready_buffer(self) -> bool:
        """Plan the next three-track window independently from arm approval."""
        plan = self.selection.get("next_executable_plan")
        if not isinstance(plan, dict):
            return False
        target = plan.get("deck_b")
        if not isinstance(target, dict) or not target.get("audio_sha256"):
            return False
        return self._schedule_lookahead(str(target["audio_sha256"]))

    def _schedule_lookahead(self, source_id: str) -> bool:
        """Start at most one background Meta request for a future source track."""
        if not getattr(self, "continuous_set_active", False):
            return False
        queue_ids = [str(value) for value in self.queue["ordered_track_ids"]]
        if source_id not in queue_ids or queue_ids.index(source_id) + 2 >= len(queue_ids):
            return False
        buffered_ids = (
            [str(value) for value in self.buffered_meta_plan.get("track_ids", [])]
            if isinstance(self.buffered_meta_plan, dict)
            else []
        )
        if buffered_ids and buffered_ids[0] == source_id:
            return False
        if source_id in self.lookahead_in_progress:
            return False
        self.lookahead_in_progress.add(source_id)
        threading.Thread(
            target=self._plan_lookahead_worker,
            args=(source_id,),
            name=f"idj-meta-lookahead-{source_id[:8]}",
            daemon=True,
        ).start()
        return True

    def _plan_lookahead_worker(self, source_id: str) -> None:
        """Keep one Meta-selected transition ahead of the executable plan."""
        try:
            with self.action_lock:
                queue_ids = [
                    str(value) for value in self.queue["ordered_track_ids"]
                ]
                if source_id not in queue_ids:
                    return
                source_index = queue_ids.index(source_id)
                if source_index + 2 >= len(queue_ids):
                    return
                target_id, following_id = queue_ids[
                    source_index + 1 : source_index + 3
                ]
                active_plan = self.selection.get("next_executable_plan", {})
                active_target = active_plan.get("deck_b", {})
                source_entry_beat = (
                    int(active_target.get("cue_beat_index", -1))
                    if active_target.get("audio_sha256") == source_id
                    else -1
                )
                immediate = self.transition_pipeline.cache.payload(
                    source_id, target_id
                )
                following = self.transition_pipeline.cache.payload(
                    target_id, following_id
                )
                queue_revision = int(self.queue["revision"])
                load_dotenv(BACKEND_ROOT.parent / ".env")
                load_dotenv(self.workspace / "experiments/meta-dj-agent/.env")
                config = MetaConfig.from_environment()
                transport = OpenAICompatibleTransport(config)
                self._append_system_event(
                    "Rolling Meta request started",
                    f"Calling {config.model} for the legacy lookahead window.",
                )

                def rolling_transport(system: str, user: str) -> str:
                    try:
                        return transport(system, user)
                    except TimeoutError:
                        self._append_system_event(
                            "Rolling Meta response delayed",
                            (
                                "The first lookahead request timed out; retrying once. "
                                "The already-armed transition remains under local control."
                            ),
                        )
                        return transport(system, user)

            # Do not hold the action lock during network inference: human takeover
            # and the currently armed transition remain responsive.
            if immediate.get("pairs") and following.get("pairs"):
                candidates = [
                    row
                    for row in build_rolling_candidates(immediate, following)
                    if int(row["a_exit_beat_index"]) > source_entry_beat + 16
                ]
                if not candidates:
                    raise RuntimeError("no_buffered_rolling_candidates")
                selected = select_with_meta(
                    candidates,
                    queue_revision=queue_revision,
                    intent="maintain_energy",
                    track_ids=(source_id, target_id, following_id),
                    model=config.model,
                    transport=rolling_transport,
                )
                buffered = {
                    "mode": "rolling_abc",
                    "queue_revision": queue_revision,
                    "track_ids": [source_id, target_id, following_id],
                    "candidate": selected.candidate,
                    "decision": selected.decision.to_dict(),
                    "model": config.model,
                }
            elif immediate.get("clean_break_pairs"):
                candidates = [
                    row
                    for row in immediate["clean_break_pairs"]
                    if int(row["a_exit"]["beat_index"])
                    > source_entry_beat + 16
                ]
                if not candidates:
                    raise RuntimeError("no_buffered_clean_break_candidates")
                pair, confidence, reason = select_clean_break_with_meta(
                    candidates,
                    queue_revision=queue_revision,
                    source_id=source_id,
                    target_id=target_id,
                    transport=rolling_transport,
                )
                buffered = {
                    "mode": "pair_clean_break",
                    "queue_revision": queue_revision,
                    "track_ids": [source_id, target_id, following_id],
                    "candidate": pair,
                    "decision": {
                        "intent": "clean_break",
                        "use_bass_swap": False,
                        "confidence": confidence,
                        "reason": reason,
                    },
                    "model": config.model,
                }
            else:
                raise RuntimeError("lookahead_pair_has_no_supported_candidates")

            with self.action_lock:
                latest_ids = [
                    str(value) for value in self.queue["ordered_track_ids"]
                ]
                if not self.continuous_set_active:
                    return
                if (
                    int(self.queue["revision"]) != queue_revision
                    or latest_ids[source_index : source_index + 3]
                    != [source_id, target_id, following_id]
                ):
                    raise RuntimeError("lookahead_queue_changed_during_meta_call")
                self.buffered_meta_plan = buffered
                if buffered["mode"] == "rolling_abc":
                    source_exit_beat = int(buffered["candidate"]["a_exit_beat_index"])
                    target_entry_beat = int(buffered["candidate"]["b_entry_beat_index"])
                    transition_bars = int(buffered["candidate"]["ab_transition_bars"])
                else:
                    source_exit_beat = int(buffered["candidate"]["a_exit"]["beat_index"])
                    target_entry_beat = int(buffered["candidate"]["b_entry"]["beat_index"])
                    transition_bars = 0
                self.transition_overrides[(source_id, target_id)] = {
                    "status": "ready",
                    "standbyReady": True,
                    "selectedBy": "meta",
                    "reason": (
                        "Meta selected this future transition before the source "
                        "track started; the buffer is ready but not armed."
                    ),
                    "sourceExitBeat": source_exit_beat,
                    "targetEntryBeat": target_entry_beat,
                    "bars": transition_bars,
                }
                self._append_system_event(
                    "Rolling Meta buffer replenished",
                    (
                        f"{source_id[:8]} → {target_id[:8]} is planned before "
                        f"{source_id[:8]} becomes the live source."
                    ),
                )
        except Exception as error:
            self._append_system_event(
                "Rolling Meta buffer unavailable",
                f"{type(error).__name__}: {error}",
            )
        finally:
            self.lookahead_in_progress.discard(source_id)

    def _promote_buffered_meta_plan(
        self,
        source_id: str,
        target_id: str,
        source_deck: int,
        target_deck: int,
        live_beat: int,
    ) -> tuple[int, dict[str, Any]] | None:
        buffered = self.buffered_meta_plan
        if not isinstance(buffered, dict):
            return None
        track_ids = [str(value) for value in buffered.get("track_ids", [])]
        if (
            buffered.get("queue_revision") != int(self.queue["revision"])
            or track_ids[:2] != [source_id, target_id]
        ):
            return None
        candidate = buffered["candidate"]
        mode = str(buffered["mode"])
        exit_beat = int(
            candidate["a_exit_beat_index"]
            if mode == "rolling_abc"
            else candidate["a_exit"]["beat_index"]
        )
        if exit_beat <= live_beat + 16:
            self.buffered_meta_plan = None
            return None

        model = str(buffered["model"])
        decision = dict(buffered["decision"])
        if mode == "rolling_abc":
            compiled = self._compile_meta_selection(
                (track_ids[0], track_ids[1], track_ids[2]),
                candidate,
                decision,
                model,
                source_deck,
                target_deck,
            )
        else:
            plan = compile_clean_break_plan(
                source_id=source_id,
                target_id=target_id,
                pair=candidate,
                catalog=self.analysis_catalog,
                queue_revision=int(self.queue["revision"]),
                source_deck=source_deck,
                target_deck=target_deck,
                model=model,
            )
            compiled = {
                "schema_version": 1,
                "selection": "meta_buffered_clean_break_promotion",
                "source_window_id": (
                    f"buffer-q{self.queue['revision']}-{source_id[:8]}-{target_id[:8]}"
                ),
                "queue_revision": int(self.queue["revision"]),
                "intent": "clean_break",
                "model": model,
                "planning_window_track_ids": track_ids,
                "planning_mode": "promoted_rolling_draft",
                "model_decision": decision,
                "selected_candidate": candidate,
                "next_executable_plan": plan,
            }
        self.buffered_meta_plan = None
        self.selection = compiled
        self.envelope = None
        self.supervisor = None
        self.transition_overrides[(source_id, target_id)] = {
            "status": "ready",
            "standbyReady": True,
            "selectedBy": "meta",
            "reason": "Promoted from the rolling Meta buffer without a new model call.",
        }
        self._restore_following_override()
        self._write_dashboard_selection(compiled)
        self._maintain_ready_buffer()
        return 200, {
            "accepted": True,
            "status": "ready",
            "selected_by": "meta",
            "model": model,
            "model_called": False,
            "plan_id": compiled["next_executable_plan"]["plan_id"],
            "message": "Buffered Meta plan promoted locally; no cue-time model wait.",
        }

    def _promote_following_draft(
        self,
        source_id: str,
        target_id: str,
    ) -> tuple[int, dict[str, Any]]:
        transition_buffer = self.selection.get("transition_buffer")
        if isinstance(transition_buffer, list) and len(transition_buffer) >= 2:
            return self._promote_transition_buffer(
                source_id, target_id, transition_buffer
            )
        draft = self.selection.get("following_feasible_draft")
        if (
            not isinstance(draft, dict)
            or draft.get("status") != "feasible_not_armed"
            or draft.get("source_track_id") != source_id
            or draft.get("target_track_id") != target_id
        ):
            return 409, {
                "error": "rolling_window_requires_current_next_and_following_tracks"
            }
        source_match = find_track_deck(self.raw_performance, source_id)
        target_match = find_track_deck(self.raw_performance, target_id)
        if source_match is None or source_match[1].beat_index is None:
            return 409, {"error": "current_source_deck_is_not_observable"}
        if target_match is None:
            return 409, {
                "error": "following_target_is_not_loaded",
                "message": "Load the preserved next track into the free Deck first.",
            }
        source_deck_number, source_deck = source_match
        target_deck_number, _ = target_match
        if source_deck_number == target_deck_number:
            return 409, {"error": "source_and_target_must_use_distinct_decks"}
        transition_start = int(draft["source_exit_beat_index"])
        if transition_start <= int(source_deck.beat_index) + 16:
            return self._recover_expired_following(source_id, target_id)

        model = str(self.selection.get("model") or MetaConfig.from_environment().model)
        plan = self._build_executable_plan(
            source_id=source_id,
            target_id=target_id,
            source_exit_beat=transition_start,
            target_entry_beat=int(draft["target_entry_beat_index"]),
            transition_bars=int(draft["transition_bars"]),
            model=model,
            source_deck=source_deck_number,
            target_deck=target_deck_number,
            plan_suffix=f"following-{source_id[:8]}-{target_id[:8]}",
            use_bass_swap=False,
        )
        prior_decision = self.selection.get("model_decision", {})
        compiled = {
            "schema_version": 1,
            "selection": "meta_following_draft_promotion",
            "source_window_id": f"dashboard-q{self.queue['revision']}-following",
            "queue_revision": int(self.queue["revision"]),
            "intent": self.selection.get("intent", "maintain_energy"),
            "model": model,
            "planning_window_track_ids": list(
                self.selection.get(
                    "planning_window_track_ids", [source_id, target_id]
                )
            ),
            "planning_mode": "promoted_rolling_draft",
            "model_decision": {
                "selected_candidate_id": prior_decision.get(
                    "selected_candidate_id", "preserved-following-draft"
                ),
                "intent": prior_decision.get("intent", "maintain_energy"),
                "use_bass_swap": False,
                "confidence": prior_decision.get("confidence", 0.75),
                "reason": "Promoted the B→C leg preserved by the earlier Meta A–B–C decision.",
            },
            "selected_candidate": dict(draft),
            "next_executable_plan": plan,
        }
        self.selection = compiled
        self.envelope = None
        self.supervisor = None
        self.transition_overrides[(source_id, target_id)] = {
            "status": "ready",
            "selectedBy": "meta",
            "reason": "Promoted from the previously selected Meta following draft.",
        }
        self._write_dashboard_selection(compiled)
        self._maintain_ready_buffer()
        return 200, {
            "accepted": True,
            "status": "ready",
            "selected_by": "meta",
            "model": model,
            "model_called": False,
            "plan_id": plan["plan_id"],
            "message": "Preserved Meta following draft promoted locally; no new model call.",
        }

    def _promote_transition_buffer(
        self,
        source_id: str,
        target_id: str,
        transition_buffer: list[dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        edge = transition_buffer[1]
        if (
            edge.get("source_track_id") != source_id
            or edge.get("target_track_id") != target_id
        ):
            return 409, {"error": "transition_buffer_identity_mismatch"}
        source_match = find_track_deck(self.raw_performance, source_id)
        target_match = find_track_deck(self.raw_performance, target_id)
        if source_match is None or source_match[1].beat_index is None:
            return 409, {"error": "current_source_deck_is_not_observable"}
        if target_match is None:
            return 409, {"error": "following_target_is_not_loaded"}
        source_deck, source = source_match
        target_deck, _ = target_match
        if int(edge["source_exit_beat_index"]) <= int(source.beat_index) + 16:
            return self._recover_expired_following(source_id, target_id)
        model = str(self.selection.get("model") or MetaConfig.from_environment().model)
        plan = self._plan_from_buffer_edge(
            edge,
            source_id=source_id,
            target_id=target_id,
            source_deck=source_deck,
            target_deck=target_deck,
            model=model,
            suffix=f"promoted-{source_id[:8]}-{target_id[:8]}",
        )
        shifted = [dict(item) for item in transition_buffer[1:]]
        compiled = deepcopy(self.selection)
        compiled["selection"] = "meta_transition_buffer_shift"
        compiled["transition_buffer"] = shifted
        compiled["planning_window_track_ids"] = [
            shifted[0]["source_track_id"],
            *[item["target_track_id"] for item in shifted],
        ]
        compiled["next_executable_plan"] = plan
        compiled["following_feasible_draft"] = (
            self._draft_from_buffer_edge(shifted[1])
            if len(shifted) > 1
            else None
        )
        self.selection = compiled
        self.envelope = None
        self.supervisor = None
        self.transition_overrides[(source_id, target_id)] = {
            "status": "ready",
            "standbyReady": True,
            "selectedBy": "meta",
            "reason": "Promoted from the existing single Meta transition buffer.",
        }
        self._restore_transition_buffer_overrides()
        self._write_dashboard_selection(compiled)
        return 200, {
            "accepted": True,
            "status": "ready",
            "selected_by": "meta",
            "model": model,
            "model_called": False,
            "plan_id": plan["plan_id"],
            "message": "Next buffered transition promoted; the future window will be re-optimized after arm.",
        }

    def _recover_expired_following(
        self,
        source_id: str,
        target_id: str,
    ) -> tuple[int, dict[str, Any]]:
        source_match = find_track_deck(self.raw_performance, source_id)
        if source_match is None or source_match[1].play is not True:
            return 409, {
                "error": "recovery_source_must_be_playing",
                "message": "Resume the source Deck before requesting live recovery.",
            }
        live_root = self.workspace / "experiments/live-performance"
        if str(live_root) not in sys.path:
            sys.path.insert(0, str(live_root))
        from live_replanner import build_live_replan_selection
        from plan_lifecycle import PerformanceSnapshot
        from queue_service import QueueSnapshot as LiveQueueSnapshot

        queue = LiveQueueSnapshot(
            revision=int(self.queue["revision"]),
            ordered_track_ids=tuple(self.queue["ordered_track_ids"]),
            updated_at=str(self.queue["updated_at"]),
        )
        performance = PerformanceSnapshot.from_observer_state(self.raw_performance)
        try:
            recovered = build_live_replan_selection(
                self.selection,
                queue,
                performance,
                self.analysis_catalog,
            )
        except ValueError as error:
            return 409, {
                "error": "live_recovery_unavailable",
                "detail": str(error),
            }
        plan = recovered["next_executable_plan"]
        if (
            plan["deck_a"]["audio_sha256"] != source_id
            or plan["deck_b"]["audio_sha256"] != target_id
        ):
            return 409, {"error": "live_recovery_identity_mismatch"}
        self.selection = recovered
        self.envelope = None
        self.supervisor = None
        self.transition_overrides[(source_id, target_id)] = {
            "status": "ready",
            "selectedBy": "deterministic_fallback",
            "reason": "Original Meta draft expired; selected the next analyzed live downbeat.",
        }
        self._write_dashboard_selection(recovered)
        self._maintain_ready_buffer()
        return 200, {
            "accepted": True,
            "status": "ready",
            "selected_by": "deterministic_fallback",
            "model_called": False,
            "plan_id": plan["plan_id"],
            "message": "Expired following draft recovered from the authoritative live beat.",
        }

    def _write_dashboard_selection(self, selection: dict[str, Any]) -> None:
        output = (
            self.workspace
            / "artifacts/live-performance/dashboard-meta-selection-v1.json"
        )
        temporary = output.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(selection, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(output)

    def _restore_following_override(self) -> None:
        draft = self.selection.get("following_feasible_draft")
        if not isinstance(draft, dict) or draft.get("status") != "feasible_not_armed":
            return
        source_id = str(draft["source_track_id"])
        target_id = str(draft["target_track_id"])
        self.transition_overrides[(source_id, target_id)] = {
            "status": "ready",
            "standbyReady": True,
            "selectedBy": (
                "deterministic_fallback"
                if "fallback" in str(self.selection.get("selection", ""))
                else "meta"
            ),
            "reason": "Meta preserved this future transition in the ready buffer; it is not armed.",
            "sourceExitBeat": int(draft["source_exit_beat_index"]),
            "targetEntryBeat": int(draft["target_entry_beat_index"]),
            "bars": int(draft["transition_bars"]),
        }

    def _compile_meta_selection(
        self,
        track_ids: tuple[str, str, str],
        candidate: dict[str, Any],
        decision: dict[str, Any],
        model: str,
        source_deck: int,
        target_deck: int,
    ) -> dict[str, Any]:
        source_id, target_id, following_id = track_ids
        plan = self._build_executable_plan(
            source_id=source_id,
            target_id=target_id,
            source_exit_beat=int(candidate["a_exit_beat_index"]),
            target_entry_beat=int(candidate["b_entry_beat_index"]),
            transition_bars=int(candidate["ab_transition_bars"]),
            model=model,
            source_deck=source_deck,
            target_deck=target_deck,
            plan_suffix=str(candidate["candidate_id"]),
            use_bass_swap=bool(decision["use_bass_swap"]),
        )
        return {
            "schema_version": 1,
            "selection": "meta_model_api_single_call",
            "source_window_id": f"dashboard-q{self.queue['revision']}-{candidate['candidate_id']}",
            "queue_revision": int(self.queue["revision"]),
            "intent": decision["intent"],
            "model": model,
            "planning_window_track_ids": [source_id, target_id, following_id],
            "planning_mode": "rolling_abc",
            "model_decision": decision,
            "selected_candidate": candidate,
            "next_executable_plan": plan,
            "following_feasible_draft": {
                "status": "feasible_not_armed",
                "source_track_id": target_id,
                "target_track_id": following_id,
                "source_exit_beat_index": int(candidate["b_exit_beat_index"]),
                "target_entry_beat_index": int(candidate["c_entry_beat_index"]),
                "transition_bars": int(candidate["bc_transition_bars"]),
            },
        }

    def _build_executable_plan(
        self,
        *,
        source_id: str,
        target_id: str,
        source_exit_beat: int,
        target_entry_beat: int,
        transition_bars: int,
        model: str,
        source_deck: int,
        target_deck: int,
        plan_suffix: str,
        use_bass_swap: bool,
    ) -> dict[str, Any]:
        source = self.analysis_catalog.tracks[source_id]
        target = self.analysis_catalog.tracks[target_id]
        beats = transition_bars * 4
        plan = {
            "schema_version": 1,
            "schema_uri": "schemas/dj-transition-plan-v1.schema.json",
            "plan_id": f"meta-q{self.queue['revision']}-{plan_suffix}",
            "source": f"meta_model_api_{model.replace('-', '_').replace('.', '_')}",
            "source_start_mode": "continue_playing",
            "completion_mode": "continue_target",
            "deck_a": {
                "audio_sha256": source_id,
                "analysis_cache_key": source.analysis_cache_key,
                "analysis_filename": "track-analysis-v1.json",
                "deck": source_deck,
                "play_start_beat_index": max(0, source_exit_beat - 8),
                "transition_start_beat_index": source_exit_beat,
                "transition_end_beat_index": source_exit_beat + beats,
            },
            "deck_b": {
                "audio_sha256": target_id,
                "analysis_cache_key": target.analysis_cache_key,
                "analysis_filename": "track-analysis-v1.json",
                "deck": target_deck,
                "cue_beat_index": target_entry_beat,
            },
            "mix": {
                "bars": transition_bars,
                "beats": beats,
                "beats_per_bar": 4,
                "crossfader_curve": "linear",
                "steps": 33,
                "tail_beats": 8,
                "tempo_source": "deck_a_sync_leader",
            },
        }
        if use_bass_swap:
            plan["mix"]["bass_swap"] = {
                "start_beat": beats // 4,
                "end_beat": beats * 3 // 4,
            }
        return plan

    def _append_system_event(self, title: str, detail: str) -> None:
        self.events.appendleft(
            {
                "id": f"system-{time.time_ns()}",
                "time": clock_time(),
                "kind": "system",
                "title": title,
                "detail": detail,
            }
        )

    def _maybe_start_auto_chain(self) -> None:
        """Recycle the idle deck and promote B→C, independently from arm mode."""
        supervisor = self.supervisor
        if supervisor is None or supervisor.manager is None:
            return
        envelope = supervisor.manager.envelope
        if envelope.status != "completed":
            return
        completed_plan_id = str(envelope.plan_id)
        if completed_plan_id in self.auto_chain_started_for:
            return
        draft = self.selection.get("following_feasible_draft")
        plan = self.selection["next_executable_plan"]
        if isinstance(draft, dict) and draft.get("status") == "feasible_not_armed":
            source_id = str(draft["source_track_id"])
            target_id = str(draft["target_track_id"])
        else:
            source_id = str(plan["deck_b"]["audio_sha256"])
            queue_ids = [str(value) for value in self.queue["ordered_track_ids"]]
            if source_id not in queue_ids:
                return
            source_index = queue_ids.index(source_id)
            if source_index + 1 >= len(queue_ids):
                return
            target_id = queue_ids[source_index + 1]
        recycled_deck = int(plan["deck_a"]["deck"])
        self.auto_chain_started_for.add(completed_plan_id)
        self.auto_chain_cancel.clear()
        self.auto_chain_thread = threading.Thread(
            target=self._auto_chain_worker,
            args=(source_id, target_id, recycled_deck),
            name="idj-auto-deck-rotation",
            daemon=True,
        )
        self.auto_chain_thread.start()

    def _auto_chain_worker(
        self,
        source_id: str,
        target_id: str,
        recycled_deck: int,
    ) -> None:
        """Load the next exact set-list row and fail closed on identity mismatch."""
        try:
            with self.lock:
                performance = self.raw_performance
            source_match = (
                find_track_deck(performance, source_id)
                if performance is not None
                else None
            )
            recycled = (
                performance.decks[recycled_deck]
                if performance is not None
                else None
            )
            if (
                source_match is None
                or source_match[1].play is not True
                or recycled is None
                or recycled.play is True
            ):
                raise RuntimeError("automatic_rotation_preconditions_failed")
            if self.auto_chain_cancel.is_set():
                return
            self._verify_set_list_edge(source_id, target_id)

            self._append_system_event(
                "Loading next queue track",
                f"Loading the verified queue slot for {target_id[:8]} directly into Deck {recycled_deck}.",
            )
            mixxx_root = self.workspace / "experiments/mixxx-control"
            if str(mixxx_root) not in sys.path:
                sys.path.insert(0, str(mixxx_root))
            import mido
            from probe import port_name, send_load_sampler_value

            sampler_slot = self.sampler_slots.get(target_id)
            if sampler_slot is None:
                raise RuntimeError(f"exact_sampler_slot_missing:{target_id[:8]}")
            with mido.open_output(port_name()) as output:
                with self.lock:
                    previous_revision = int(self.raw_performance.revision)
                send_load_sampler_value(output, recycled_deck, sampler_slot)
            if not self._wait_for_loaded_track_after_revision(
                recycled_deck, target_id, previous_revision
            ):
                with self.lock:
                    latest = self.raw_performance
                loaded_id = latest.decks[recycled_deck].track_id if latest else None
                shown = loaded_id[:8] if loaded_id else "unresolved"
                raise RuntimeError(f"loaded_track_identity_mismatch:{shown}")

            self._append_system_event(
                "Next track verified",
                f"Deck {recycled_deck} matches the queued analysis ID; promoting the preserved Meta plan.",
            )
            if self.auto_chain_cancel.is_set():
                return
            status, payload = self.request_meta_plan()
            if status != 200:
                raise RuntimeError(
                    f"following_plan_promotion_failed:{payload.get('error')}"
                )
            if self.auto_chain_cancel.is_set():
                return
            if self.auto_arm_enabled:
                status, payload = self._arm_ready_plan_with_retry()
                if status != 202:
                    raise RuntimeError(
                        f"following_plan_arm_failed:{payload.get('error')}"
                    )
                self._append_system_event(
                    "Following transition auto-armed",
                    "The ready B→C plan is now controlled by the local lifecycle supervisor.",
                )
            else:
                self._append_system_event(
                    "Following transition ready",
                    "The B→C plan is buffered and waiting for human Approve & arm.",
                )
        except Exception as error:
            self._append_system_event(
                "Automatic deck rotation stopped",
                f"{type(error).__name__}: {error}. Current playback was preserved.",
            )

    def _maybe_start_continuity_guard(self, performance: Any) -> None:
        if (
            not self.continuous_set_active
            or not self.auto_arm_enabled
            or self.activation_in_progress
        ):
            return
        if self._executor_running():
            return
        if self.envelope and self.envelope.get("status") in {"armed", "executing"}:
            return
        playing = [
            (number, deck)
            for number, deck in performance.decks.items()
            if deck.play is True and deck.track_id in self.catalog
        ]
        candidates = playing
        if not candidates:
            candidates = [
                (number, deck)
                for number, deck in performance.decks.items()
                if deck.track_id in self.catalog
                and deck.position_seconds is not None
                and float(deck.position_seconds)
                >= float(self.catalog[deck.track_id]["duration_seconds"]) - 0.75
            ]
        if len(candidates) != 1:
            return
        deck_number, deck = candidates[0]
        track_id = str(deck.track_id)
        duration = float(self.catalog[track_id]["duration_seconds"])
        position = float(deck.position_seconds or 0)
        if deck.play is True and duration - position > 12.0:
            return
        now = time.monotonic()
        if track_id in self.continuity_guard_tracks:
            return
        if now - self.continuity_guard_last.get(track_id, 0.0) < 8.0:
            return
        self.continuity_guard_tracks.add(track_id)
        self.continuity_guard_last[track_id] = now
        threading.Thread(
            target=self._continuity_guard_worker,
            args=(deck_number, track_id),
            name="idj-continuity-guard",
            daemon=True,
        ).start()

    def _continuity_guard_worker(self, deck_number: int, track_id: str) -> None:
        try:
            with self.lock:
                performance = self.raw_performance
            if performance is None:
                return
            deck = performance.decks[deck_number]
            if deck.track_id != track_id:
                return
            queue_ids = [str(value) for value in self.queue["ordered_track_ids"]]
            if track_id not in queue_ids:
                raise RuntimeError("continuity_source_is_not_in_queue")
            source_index = queue_ids.index(track_id)
            if source_index + 1 >= len(queue_ids):
                self.continuous_set_active = False
                self._append_system_event(
                    "Prepared set completed",
                    "The final queued track reached its end; no track was looped.",
                )
                return
            next_track_id = queue_ids[source_index + 1]
            target_deck = 1 if deck_number == 2 else 2
            mixxx_root = self.workspace / "experiments/mixxx-control"
            if str(mixxx_root) not in sys.path:
                sys.path.insert(0, str(mixxx_root))
            import mido
            from probe import (
                port_name,
                send_load_sampler_value,
                send_sync_value,
                send_value,
            )

            target = performance.decks[target_deck]
            if target.track_id != next_track_id:
                sampler_slot = self.sampler_slots.get(next_track_id)
                if sampler_slot is None:
                    raise RuntimeError(
                        f"exact_sampler_slot_missing:{next_track_id[:8]}"
                    )
                with mido.open_output(port_name()) as output:
                    with self.lock:
                        previous_revision = int(self.raw_performance.revision)
                    send_load_sampler_value(output, target_deck, sampler_slot)
                if not self._wait_for_loaded_track_after_revision(
                    target_deck, next_track_id, previous_revision
                ):
                    raise RuntimeError("emergency_next_track_identity_mismatch")

            with mido.open_output(port_name()) as output:
                send_sync_value(output, target_deck, "enabled", False)
                send_value(output, str(target_deck), "rate", 0.0)
                send_value(output, str(target_deck), "volume", 0.75)
                send_value(output, str(target_deck), "play", 1.0)
                send_value(
                    output,
                    "master",
                    "crossfader",
                    0.0 if target_deck == 1 else 1.0,
                )
                send_value(output, str(deck_number), "play", 0.0)

            self.current_track_id = next_track_id
            self.supervisor = None
            self.envelope = None
            self.auto_chain_started_for.clear()
            self._append_system_event(
                "Emergency queue handoff",
                (
                    f"No transition was armed near the end of {track_id[:8]}; "
                    f"Deck {target_deck} started the next verified queue track "
                    f"{next_track_id[:8]} instead of looping the source."
                ),
            )

            if source_index + 2 < len(queue_ids):
                following_id = queue_ids[source_index + 2]
                time.sleep(0.35)
                self.auto_chain_cancel.clear()
                self.auto_chain_thread = threading.Thread(
                    target=self._auto_chain_worker,
                    args=(next_track_id, following_id, deck_number),
                    name="idj-emergency-deck-recovery",
                    daemon=True,
                )
                self.auto_chain_thread.start()
        except Exception as error:
            self._append_system_event(
                "Continuity guard failed",
                f"{type(error).__name__}: {error}",
            )
        finally:
            self.continuity_guard_tracks.discard(track_id)

    def request_takeover(self) -> tuple[int, dict[str, Any]]:
        with self.action_lock:
            self.auto_arm_enabled = False
            self.auto_chain_cancel.set()
            if (
                self.auto_chain_thread is not None
                and self.auto_chain_thread.is_alive()
                and (not self.supervisor or self.supervisor.executor is None)
            ):
                self._append_system_event(
                    "Automatic deck rotation cancelled",
                    "Human takeover stopped the pending queue load and follow-up arm.",
                )
                return 202, {
                    "accepted": True,
                    "status": "cancelled",
                    "message": "Pending automatic deck rotation cancelled; current playback is preserved.",
                }
            if not self.supervisor:
                return 409, {"error": "no dashboard-owned plan is executing"}
            accepted, reason = self.supervisor.request_takeover()
            if not accepted:
                return 409, {"error": reason}
            self.envelope = self._load_envelope()
            manager = self.supervisor.manager
            return 202, {
                "accepted": True,
                "plan_id": manager.envelope.plan_id,
                "status": manager.envelope.status,
                "invalidation_reason": manager.envelope.invalidation_reason,
                "message": "Future automation cancelled; current playback is preserved.",
            }

    def _maybe_schedule_human_replan(self) -> None:
        envelope = self.envelope
        if not isinstance(envelope, dict) or envelope.get("status") != "invalidated":
            return
        reason = str(envelope.get("invalidation_reason") or "")
        plan_id = str(envelope.get("plan_id") or "")
        if not reason.startswith("human_") or not plan_id:
            return
        if reason == "human_dashboard_takeover" or plan_id in self.human_replan_started_for:
            return
        self.human_replan_started_for.add(plan_id)
        threading.Thread(
            target=self._human_replan_worker,
            args=(plan_id, reason),
            name=f"idj-human-replan-{plan_id[-8:]}",
            daemon=True,
        ).start()

    def _human_replan_worker(self, invalidated_plan_id: str, reason: str) -> None:
        """Replace all three edges only after a human invalidates the cue plan."""
        try:
            deadline = time.monotonic() + 5.0
            while self._executor_running() and time.monotonic() < deadline:
                time.sleep(0.1)
            if self._executor_running():
                raise RuntimeError("invalidated_executor_did_not_stop")
            with self.action_lock:
                if str((self.envelope or {}).get("plan_id")) != invalidated_plan_id:
                    return
                self.selection = {}
                self.buffered_meta_plan = None
                self.future_transition_plan = None
                self.future_plan_in_progress = False
                self.supervisor = None
                self.envelope = None
            status, payload = self.request_meta_plan()
            if status != 200 or int(payload.get("transition_count", 0)) != 3:
                raise RuntimeError(
                    f"full_three_transition_replan_failed:{payload.get('error')}"
                )
            self._append_system_event(
                "Human cue change replanned",
                f"{reason} invalidated the old cue; Meta replaced all three transitions in one plan.",
            )
            if self.auto_arm_enabled:
                arm_status, arm_payload = self._arm_ready_plan_with_retry()
                if arm_status != 202:
                    raise RuntimeError(
                        f"human_replan_arm_failed:{arm_payload.get('error')}"
                    )
        except Exception as error:
            self._append_system_event(
                "Human cue replan unavailable",
                f"{type(error).__name__}: {error}. Current playback was preserved.",
            )

    def _track(self, track_id: str, index: int) -> dict[str, Any]:
        analysis = self.catalog.get(track_id, {})
        rhythm = analysis.get("rhythm", {})
        return {
            "id": track_id,
            "title": display_title(str(analysis.get("source_filename", track_id[:8]))),
            "artist": "Local analyzed track",
            "bpm": round(float(rhythm.get("bpm", 0)), 2),
            "durationSeconds": round(float(analysis.get("duration_seconds", 0)), 3),
            "energy": "medium",
            "accent": ACCENTS[index % len(ACCENTS)],
        }

    def _event(self, observed: Any) -> dict[str, Any]:
        details = dict(observed.details)
        origin = str(observed.origin)
        if origin == "automation":
            kind = "executor"
        elif origin == "human_or_external":
            kind = "human"
        else:
            kind = "system"
        summary = ", ".join(
            f"{key}={value}"
            for key, value in details.items()
            if key not in {"track_id", "analysis_cache_key"}
        )
        return {
            "id": f"mixxx-{observed.revision}-{observed.event}-{observed.observed_at}",
            "time": clock_time(),
            "kind": kind,
            "title": str(observed.event).replace("_", " ").title(),
            "detail": summary or f"Mixxx performance revision {observed.revision}",
        }

    def update(self, performance: Any, observed_events: tuple[Any, ...]) -> None:
        for observed in observed_events:
            self.events.appendleft(self._event(observed))

        if self.activation_in_progress:
            with self.lock:
                self.raw_performance = performance
            return

        queue_ids = [str(value) for value in self.queue["ordered_track_ids"]]
        playing_ids = {
            deck.track_id
            for deck in performance.decks.values()
            if deck.play is True and deck.track_id
        }
        loaded_ids = {
            deck.track_id for deck in performance.decks.values() if deck.track_id
        }
        current_id = next(
            (track_id for track_id in queue_ids if track_id in playing_ids),
            None,
        )
        if current_id is None:
            crossfader = float(performance.crossfader or 0)
            favored_deck = 2 if crossfader > 0.25 else 1 if crossfader < -0.25 else None
            if favored_deck is not None:
                favored_id = performance.decks[favored_deck].track_id
                if favored_id in loaded_ids:
                    current_id = favored_id
        if current_id is None:
            if self.current_track_id in loaded_ids:
                current_id = self.current_track_id
        if current_id is None:
            current_id = next(
                (track_id for track_id in reversed(queue_ids) if track_id in loaded_ids),
                queue_ids[0] if queue_ids else None,
            )
        current_index = queue_ids.index(current_id) if current_id in queue_ids else 0
        self.current_track_id = current_id
        if (
            not self.continuous_set_active
            and self.sampler_slots
            and current_id in self.sampler_slots
            and current_id in playing_ids
        ):
            # Start set stages sampler slots in Mixxx itself. The registry can be
            # safely resumed when the exact promoted queue is actively playing.
            self.continuous_set_active = True

        queue = []
        for index, track_id in enumerate(queue_ids):
            if index < current_index:
                queue_state = "played"
            elif index == current_index:
                queue_state = "current"
            elif index == current_index + 1:
                queue_state = "next"
            else:
                queue_state = "queued"
            queue.append({**self._track(track_id, index), "state": queue_state})

        decks = []
        for deck_number in (1, 2):
            observed = performance.decks[deck_number]
            track = None
            if observed.track_id:
                index = queue_ids.index(observed.track_id) if observed.track_id in queue_ids else deck_number - 1
                track = self._track(observed.track_id, index)
            sync_role = {0: "off", 1: "follower", 2: "leader"}.get(
                observed.sync_mode, "off"
            )
            decks.append(
                {
                    "deck": deck_number,
                    "track": track,
                    "playing": observed.play is True,
                    "beatIndex": observed.beat_index,
                    "positionSeconds": round(float(observed.position_seconds or 0), 3),
                    "syncRole": sync_role,
                }
            )

        planning_pending = not isinstance(
            self.selection.get("next_executable_plan"), dict
        )
        if planning_pending:
            source_id = current_id or queue_ids[0]
            source_index = queue_ids.index(source_id)
            target_id = queue_ids[min(source_index + 1, len(queue_ids) - 1)]
            plan_payload = {
                "plan_id": f"meta-planning-q{self.queue['revision']}",
                "source": "meta_pending",
                "transition_mode": "beat_sync_crossfade",
                "deck_a": {
                    "audio_sha256": source_id,
                    "play_start_beat_index": 0,
                    "transition_start_beat_index": 0,
                    "transition_end_beat_index": 0,
                },
                "deck_b": {
                    "audio_sha256": target_id,
                    "cue_beat_index": 0,
                },
                "mix": {"bars": 0},
            }
        else:
            plan_payload = self.selection["next_executable_plan"]
        transition_mode = str(
            plan_payload.get("transition_mode", "beat_sync_crossfade")
        )
        source_analysis = self.catalog[plan_payload["deck_a"]["audio_sha256"]]
        source_beats = source_analysis["rhythm"]["beats_seconds"]
        source_duration = float(source_analysis["duration_seconds"])
        target_analysis = self.catalog[plan_payload["deck_b"]["audio_sha256"]]
        target_beats = target_analysis["rhythm"]["beats_seconds"]
        target_duration = float(target_analysis["duration_seconds"])

        def beat_progress(index: int) -> float:
            return min(1.0, max(0.0, float(source_beats[index]) / source_duration))

        context = self.selection.get("replan_context", {})
        candidate = self.selection.get(
            "selected_candidate",
            context.get("selected_candidate", {}),
        )
        model_decision = self.selection.get("model_decision", {})
        selection_name = str(self.selection.get("selection", ""))
        plan_status = "planning" if planning_pending else "ready"
        invalidation_reason = None
        if self.envelope and str(self.envelope.get("plan_id", "")).startswith(
            str(plan_payload["plan_id"])
        ):
            plan_status = str(self.envelope.get("status", "ready"))
            invalidation_reason = self.envelope.get("invalidation_reason")
        source = str(plan_payload.get("source", ""))
        selected_by = (
            "deterministic_fallback"
            if "deterministic_fallback" in source
            or "deterministic_fallback" in selection_name
            else "meta"
            if "meta" in source or "meta" in selection_name
            else "deterministic_fallback"
        )
        confidence = float(
            model_decision.get("confidence", candidate.get("intent_score", 0.0))
        )
        if confidence <= 0 and not planning_pending:
            confidence = 0.75

        transition_end_beat = int(
            plan_payload["deck_a"].get(
                "transition_end_beat_index",
                plan_payload["deck_a"]["transition_start_beat_index"],
            )
        )
        planning_window_track_ids = self.selection.get(
            "planning_window_track_ids"
        )
        if not isinstance(planning_window_track_ids, list):
            source_index = queue_ids.index(plan_payload["deck_a"]["audio_sha256"])
            if self.selection.get("selection") == "meta_following_draft_promotion":
                planning_window_track_ids = queue_ids[
                    max(0, source_index - 1) : min(len(queue_ids), source_index + 2)
                ]
            else:
                planning_window_track_ids = [
                    plan_payload["deck_a"]["audio_sha256"],
                    plan_payload["deck_b"]["audio_sha256"],
                ]
        following_draft = self.selection.get("following_feasible_draft")
        future_window_ids = (
            self.future_transition_plan.get("planning_window_track_ids", [])
            if isinstance(self.future_transition_plan, dict)
            else []
        )
        plan = {
            "id": plan_payload["plan_id"],
            "status": plan_status,
            "sourceTrackId": plan_payload["deck_a"]["audio_sha256"],
            "targetTrackId": plan_payload["deck_b"]["audio_sha256"],
            "sourceCueBeat": int(plan_payload["deck_a"]["play_start_beat_index"]),
            "sourceExitBeat": int(plan_payload["deck_a"]["transition_start_beat_index"]),
            "sourceCuePosition": beat_progress(
                int(plan_payload["deck_a"]["play_start_beat_index"])
            ),
            "sourceMixStartPosition": beat_progress(
                int(plan_payload["deck_a"]["transition_start_beat_index"])
            ),
            "sourceMixEndPosition": beat_progress(
                transition_end_beat
            ),
            "targetEntryBeat": int(plan_payload["deck_b"]["cue_beat_index"]),
            "targetEntryPosition": min(
                1.0,
                max(
                    0.0,
                    float(target_beats[int(plan_payload["deck_b"]["cue_beat_index"])])
                    / target_duration,
                ),
            ),
            "bars": int(plan_payload["mix"].get("bars", 0)),
            "transitionMode": transition_mode,
            "intent": str(self.selection.get("intent", "maintain_energy")).replace("_", " ").title(),
            "confidence": min(1.0, max(0.0, confidence)),
            "reason": str(
                "Meta is selecting the first transition while playback continues."
                if planning_pending
                else model_decision.get("reason")
                or (
                    "A bounded local recovery selected the next future analyzed downbeat."
                    if selected_by == "deterministic_fallback"
                    else "Meta selected this candidate from the locally constrained window."
                )
            ),
            "selectedBy": selected_by,
            "model": self.selection.get("model") if selected_by == "meta" else None,
            "queueRevision": int(self.queue["revision"]),
            "performanceRevision": int(performance.revision),
            "invalidationReason": invalidation_reason,
            "planningWindowTrackIds": [
                str(track_id) for track_id in planning_window_track_ids
            ],
            "planningMode": str(
                self.selection.get(
                    "planning_mode",
                    "promoted_rolling_draft"
                    if self.selection.get("selection")
                    == "meta_following_draft_promotion"
                    else "pair_clean_break"
                    if transition_mode == "clean_break"
                    else "rolling_abc",
                )
            ),
            "activeTransitionLocked": plan_status in {"armed", "executing"},
            "futurePlanStatus": (
                "ready"
                if self.future_transition_plan is not None
                else "planning"
                if self.future_plan_in_progress
                else "idle"
            ),
            "futurePlanningWindowTrackIds": [
                str(track_id) for track_id in future_window_ids
            ],
            "followingDraft": (
                {
                    "sourceTrackId": str(following_draft["source_track_id"]),
                    "targetTrackId": str(following_draft["target_track_id"]),
                    "sourceExitBeat": int(following_draft["source_exit_beat_index"]),
                    "targetEntryBeat": int(following_draft["target_entry_beat_index"]),
                    "bars": int(following_draft["transition_bars"]),
                    "status": str(following_draft["status"]),
                }
                if isinstance(following_draft, dict)
                else None
            ),
        }

        transitions = []
        for edge in self.transition_edges:
            payload = edge.to_dict()
            override = self.transition_overrides.get(
                (edge.source_track_id, edge.target_track_id)
            )
            if override:
                payload.update(override)
            edge_source_index = queue_ids.index(edge.source_track_id)
            if edge_source_index < current_index:
                payload["status"] = "completed"
            if (
                edge.source_track_id == plan["sourceTrackId"]
                and edge.target_track_id == plan["targetTrackId"]
            ):
                if plan_status in {"draft", "ready", "armed", "executing"}:
                    payload["status"] = (
                        "ready" if plan_status in {"draft", "ready"} else plan_status
                    )
                    payload["selectedBy"] = selected_by
                    payload["reason"] = plan["reason"]
                elif plan_status == "completed":
                    payload["status"] = "completed"
                    payload["selectedBy"] = selected_by
                elif plan_status in {"invalidated", "cancelled"}:
                    payload["status"] = "invalidated"
                    payload["selectedBy"] = selected_by
                    payload["reason"] = invalidation_reason
            transitions.append(payload)

        live_state = {
            "dataSource": "live",
            "connected": True,
            "autoArmEnabled": self.auto_arm_enabled,
            "queueRevision": int(self.queue["revision"]),
            "performanceRevision": int(performance.revision),
            "crossfader": float(performance.crossfader or 0),
            "decks": decks,
            "queue": queue,
            "transitions": transitions,
            "plan": plan,
            "events": list(self.events),
        }
        with self.lock:
            self.state = live_state
            self.error = None
            self.raw_performance = performance

        supervisor = self.supervisor
        if supervisor is not None:
            supervisor.update(performance, observed_events)
            self.envelope = self._load_envelope()
            self._maybe_schedule_human_replan()
            self._maybe_start_auto_chain()
        self._maybe_start_continuity_guard(performance)


def make_handler(store: LiveStateStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _request_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if length <= 0:
                return {}
            if length > 64 * 1024:
                raise ValueError("request body is too large")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def do_GET(self) -> None:  # noqa: N802
            state, error = store.snapshot()
            if self.path == "/api/health":
                self._json(
                    200,
                    {
                        "ok": state is not None,
                        "observer": "connected" if state is not None else "waiting",
                        "error": error,
                    },
                )
                return
            if self.path == "/api/state":
                if state is None:
                    self._json(
                        503,
                        {
                            "error": error
                            or "waiting for the first complete Mixxx snapshot"
                        },
                    )
                else:
                    self._json(200, state)
                return
            self._json(404, {"error": "not found"})

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            try:
                payload = self._request_json()
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                self._json(400, {"error": str(error)})
                return
            if self.path == "/api/plan":
                status, payload = store.request_meta_plan()
                self._json(status, payload)
                return
            if self.path == "/api/arm":
                status, payload = store.request_arm()
                self._json(status, payload)
                return
            if self.path == "/api/auto-arm":
                status, response = store.request_auto_arm(payload.get("enabled"))
                self._json(status, response)
                return
            if self.path == "/api/takeover":
                status, payload = store.request_takeover()
                self._json(status, payload)
                return
            if self.path == "/api/set-builder/build":
                status, response = store.request_set_build(payload)
                self._json(status, response)
                return
            if self.path == "/api/set-builder/activate":
                status, response = store.request_activate_set()
                self._json(status, response)
                return
            if self.path == "/api/set-builder/start":
                status, response = store.request_start_set()
                self._json(status, response)
                return
            if self.path == "/api/queue/reorder":
                status, response = store.request_queue_reorder(payload)
                self._json(status, response)
                return
            self._json(404, {"error": "not found"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--duration", type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace_root.resolve()
    mixxx_root = workspace / "experiments/mixxx-control"
    sys.path.insert(0, str(mixxx_root))
    from observer import run_observer
    from probe import COMMAND_LOG, DEFAULT_ANALYSIS_ROOT, port_name

    store = LiveStateStore(workspace)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(
        json.dumps(
            {
                "event": "dashboard_bridge_started",
                "url": f"http://127.0.0.1:{args.port}/api/state",
                "mixxx_port": port_name(),
            }
        ),
        flush=True,
    )
    try:
        run_observer(
            port_name=port_name(),
            analysis_root=DEFAULT_ANALYSIS_ROOT,
            command_log=COMMAND_LOG,
            duration=args.duration,
            on_update=store.update,
        )
    except KeyboardInterrupt:
        print(json.dumps({"event": "dashboard_bridge_stopped"}), flush=True)
        return 0
    except Exception as error:
        store.set_error(f"{type(error).__name__}: {error}")
        raise
    finally:
        if store.supervisor is not None:
            store.supervisor.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

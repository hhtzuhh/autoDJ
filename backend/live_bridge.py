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
    MetaConfig,
    OpenAICompatibleTransport,
    build_rolling_candidates,
    load_dotenv,
    select_with_meta,
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
        self.transition_overrides: dict[tuple[str, str], dict[str, Any]] = {}
        self.action_lock = threading.Lock()
        self.auto_chain_cancel = threading.Event()
        self.auto_chain_thread: threading.Thread | None = None
        self.auto_chain_started_for: set[str] = set()
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
        self.selection = self._load_selection()
        self._restore_following_override()
        self.envelope = self._load_envelope()
        self.catalog = self._load_catalog()

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

    def set_error(self, message: str) -> None:
        with self.lock:
            self.error = message

    def snapshot(self) -> tuple[dict[str, Any] | None, str | None]:
        with self.lock:
            return self.state, self.error

    def request_arm(self) -> tuple[int, dict[str, Any]]:
        with self.action_lock:
            if self.raw_performance is None:
                return 503, {"error": "Mixxx performance snapshot is unavailable"}
            if self.supervisor and self.supervisor.executor is not None:
                return 409, {"error": "a transition executor is already active"}

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
            return 202, {
                "accepted": True,
                "plan_id": plan["plan_id"],
                "message": "Plan passed preflight and is waiting for the observer lifecycle update.",
            }

    def request_meta_plan(self) -> tuple[int, dict[str, Any]]:
        with self.action_lock:
            if self.raw_performance is None or self.current_track_id is None:
                return 503, {"error": "Mixxx performance snapshot is unavailable"}
            if self.supervisor and self.supervisor.executor is not None:
                return 409, {"error": "a transition executor is already active"}
            queue_ids = [str(value) for value in self.queue["ordered_track_ids"]]
            if self.current_track_id not in queue_ids:
                return 409, {"error": "current_track_is_not_in_queue"}
            current_index = queue_ids.index(self.current_track_id)
            if current_index + 1 >= len(queue_ids):
                return 409, {"error": "queue_has_no_next_track"}
            if current_index + 2 >= len(queue_ids):
                return self._promote_following_draft(
                    queue_ids[current_index],
                    queue_ids[current_index + 1],
                )
            track_ids = tuple(queue_ids[current_index : current_index + 3])
            source_id, target_id, following_id = track_ids
            source_match = find_track_deck(self.raw_performance, source_id)
            if source_match is None or source_match[1].beat_index is None:
                return 409, {"error": "current_source_deck_is_not_observable"}
            source_deck_number, source_deck = source_match
            target_deck_number = 1 if source_deck_number == 2 else 2
            immediate = self.transition_pipeline.cache.payload(source_id, target_id)
            following = self.transition_pipeline.cache.payload(target_id, following_id)
            candidates = build_rolling_candidates(immediate, following)
            candidates = [
                row
                for row in candidates
                if int(row["a_exit_beat_index"]) > int(source_deck.beat_index) + 16
            ]
            if not candidates:
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
                self.transition_overrides[edge_key] = {
                    "status": "analyzed",
                    "selectedBy": None,
                    "reason": f"meta_planning_failed:{type(error).__name__}",
                }
                return 502, {
                    "error": "meta_planning_failed",
                    "detail": f"{type(error).__name__}: {error}",
                }

            self.selection = compiled
            self.envelope = None
            self.supervisor = None
            self.transition_overrides[edge_key] = {
                "status": "ready",
                "selectedBy": "meta",
                "reason": selection.decision.reason,
            }
            self._restore_following_override()
            self._write_dashboard_selection(compiled)
            return 200, {
                "accepted": True,
                "status": "ready",
                "selected_by": "meta",
                "model": config.model,
                "candidate_count": selection.candidate_count,
                "plan_id": compiled["next_executable_plan"]["plan_id"],
                "message": "Meta selected one locally feasible candidate; local compilation succeeded.",
            }

    def _promote_following_draft(
        self,
        source_id: str,
        target_id: str,
    ) -> tuple[int, dict[str, Any]]:
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
        return 200, {
            "accepted": True,
            "status": "ready",
            "selected_by": "meta",
            "model": model,
            "model_called": False,
            "plan_id": plan["plan_id"],
            "message": "Preserved Meta following draft promoted locally; no new model call.",
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
            "status": "feasible",
            "selectedBy": "meta",
            "reason": "Meta preserved this next transition; it is not loaded or armed.",
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
        """Recycle the idle deck after a completed A→B leg, then arm B→C."""
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
        if not isinstance(draft, dict) or draft.get("status") != "feasible_not_armed":
            return
        plan = self.selection["next_executable_plan"]
        source_id = str(draft["source_track_id"])
        target_id = str(draft["target_track_id"])
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
                f"Advancing tonight's exact Mixxx set list and loading its next row into Deck {recycled_deck}.",
            )
            mixxx_root = self.workspace / "experiments/mixxx-control"
            if str(mixxx_root) not in sys.path:
                sys.path.insert(0, str(mixxx_root))
            import mido
            from probe import port_name, send_library_value, send_load_value

            loaded_id = recycled.track_id
            attempt_limit = max(1, len(self.queue["ordered_track_ids"]))
            with mido.open_output(port_name()) as output:
                for attempt in range(1, attempt_limit + 1):
                    previous_id = loaded_id
                    send_library_value(output, "next")
                    time.sleep(0.25)
                    if self.auto_chain_cancel.is_set():
                        return
                    send_load_value(output, recycled_deck)

                    deadline = time.monotonic() + 3.0
                    saw_unresolved = False
                    while time.monotonic() < deadline:
                        if self.auto_chain_cancel.wait(0.05):
                            return
                        with self.lock:
                            latest = self.raw_performance
                        if latest is None:
                            continue
                        observed_id = latest.decks[recycled_deck].track_id
                        if observed_id is None:
                            saw_unresolved = True
                            continue
                        loaded_id = observed_id
                        if loaded_id == target_id:
                            break
                        if loaded_id != previous_id or saw_unresolved:
                            break
                    if loaded_id == target_id:
                        break
                    shown = loaded_id[:8] if loaded_id else "unresolved"
                    self._append_system_event(
                        "Queue load retry",
                        f"Deck {recycled_deck} loaded {shown}, not the queued target; advancing the exact set list again ({attempt}/{attempt_limit}).",
                    )
                else:
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
            status, payload = self.request_arm()
            if status != 202:
                raise RuntimeError(f"following_plan_arm_failed:{payload.get('error')}")
            self._append_system_event(
                "Following transition armed",
                "The preserved B→C plan is now controlled by the local lifecycle supervisor.",
            )
        except Exception as error:
            self._append_system_event(
                "Automatic deck rotation stopped",
                f"{type(error).__name__}: {error}. Current playback was preserved.",
            )

    def request_takeover(self) -> tuple[int, dict[str, Any]]:
        with self.action_lock:
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

        plan_payload = self.selection["next_executable_plan"]
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
        plan_status = "ready"
        invalidation_reason = None
        if self.envelope and self.envelope.get("plan_id") == plan_payload["plan_id"]:
            plan_status = str(self.envelope.get("status", "ready"))
            invalidation_reason = self.envelope.get("invalidation_reason")
        source = str(plan_payload.get("source", ""))
        selected_by = (
            "meta"
            if "meta" in source or "meta" in selection_name
            else "deterministic_fallback"
        )
        confidence = float(
            model_decision.get("confidence", candidate.get("intent_score", 0.0))
        )
        if confidence <= 0:
            confidence = 0.75

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
                int(plan_payload["deck_a"]["transition_end_beat_index"])
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
            "bars": int(plan_payload["mix"]["bars"]),
            "intent": str(self.selection.get("intent", "maintain_energy")).replace("_", " ").title(),
            "confidence": min(1.0, max(0.0, confidence)),
            "reason": str(
                model_decision.get("reason")
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
        }

        transitions = []
        for edge in self.transition_edges:
            payload = edge.to_dict()
            override = self.transition_overrides.get(
                (edge.source_track_id, edge.target_track_id)
            )
            if override:
                payload.update(override)
            if (
                edge.source_track_id == plan["sourceTrackId"]
                and edge.target_track_id == plan["targetTrackId"]
            ):
                if plan_status in {"draft", "ready", "armed", "executing"}:
                    payload["status"] = "ready"
                    payload["selectedBy"] = selected_by
                elif plan_status in {"invalidated", "cancelled"}:
                    payload["status"] = "invalidated"
                    payload["selectedBy"] = selected_by
                    payload["reason"] = invalidation_reason
            transitions.append(payload)

        live_state = {
            "dataSource": "live",
            "connected": True,
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
            self._maybe_start_auto_chain()


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
            if self.path == "/api/plan":
                status, payload = store.request_meta_plan()
                self._json(status, payload)
                return
            if self.path == "/api/arm":
                status, payload = store.request_arm()
                self._json(status, payload)
                return
            if self.path == "/api/takeover":
                status, payload = store.request_takeover()
                self._json(status, payload)
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
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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


class LiveStateStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.lock = threading.Lock()
        self.state: dict[str, Any] | None = None
        self.error: str | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=20)
        self.raw_performance: Any | None = None
        self.supervisor: Any | None = None
        self.action_lock = threading.Lock()
        self.queue = load_json(
            workspace / "artifacts/live-performance/queue-state-v1.json"
        )
        self.selection = self._load_selection()
        self.envelope = self._load_envelope()
        self.catalog = self._load_catalog()

    def _load_selection(self) -> dict[str, Any]:
        candidates = (
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

            live_selection = deepcopy(self._load_selection())
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

    def request_takeover(self) -> tuple[int, dict[str, Any]]:
        with self.action_lock:
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
        current_id = next(iter(playing_ids), None)
        if current_id is None:
            current_id = next(
                (track_id for track_id in reversed(queue_ids) if track_id in loaded_ids),
                queue_ids[0] if queue_ids else None,
            )
        current_index = queue_ids.index(current_id) if current_id in queue_ids else 0

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
        context = self.selection.get("replan_context", {})
        candidate = context.get("selected_candidate", {})
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
        confidence = float(candidate.get("intent_score", 0.0))
        if confidence <= 0:
            confidence = 0.75

        plan = {
            "id": plan_payload["plan_id"],
            "status": plan_status,
            "sourceTrackId": plan_payload["deck_a"]["audio_sha256"],
            "targetTrackId": plan_payload["deck_b"]["audio_sha256"],
            "sourceExitBeat": int(plan_payload["deck_a"]["transition_start_beat_index"]),
            "targetEntryBeat": int(plan_payload["deck_b"]["cue_beat_index"]),
            "bars": int(plan_payload["mix"]["bars"]),
            "intent": str(self.selection.get("intent", "maintain_energy")).replace("_", " ").title(),
            "confidence": min(1.0, max(0.0, confidence)),
            "reason": (
                "A bounded local recovery selected the next future analyzed downbeat."
                if selected_by == "deterministic_fallback"
                else "Meta selected this candidate from the locally constrained window."
            ),
            "selectedBy": selected_by,
            "model": "muse-spark-1.3" if selected_by == "meta" else None,
            "queueRevision": int(self.queue["revision"]),
            "performanceRevision": int(performance.revision),
            "invalidationReason": invalidation_reason,
        }

        live_state = {
            "dataSource": "live",
            "connected": True,
            "queueRevision": int(self.queue["revision"]),
            "performanceRevision": int(performance.revision),
            "crossfader": float(performance.crossfader or 0),
            "decks": decks,
            "queue": queue,
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

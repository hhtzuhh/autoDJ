"""Local catalog, Meta set curation, and background DJ analysis jobs.

This service prepares a future performance queue.  It deliberately does not
mutate the queue currently executing in Mixxx; activation belongs to the live
plan lifecycle once every required track and adjacent edge is ready.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

from meta_planner import MetaConfig, MetaOutputLimitError, OpenAICompatibleTransport
from pair_analyzer import PAIR_VERSION
from transition_pipeline import AnalysisCatalog, PairAnalysisCache, adjacent_pairs, utc_now


AUDIO_SUFFIXES = {".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}
CURATOR_SYSTEM_INSTRUCTION = """
You curate a DJ set from a bounded local catalog. Select and order exactly the
requested number of supplied track IDs. Prefer tracks matching the request,
then create a coherent energy arc using only the provided metadata. Never
invent IDs or facts. Return exactly one JSON object with only
ordered_track_ids and reason. ordered_track_ids must contain unique IDs.
Use track_count as the exact requested count; request prose may contain an old
count. Keep reason to one concise sentence, at most 500 characters. Prefer a
simple feasible ordering; do not enumerate permutations or write an essay.
""".strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_title(path: Path) -> str:
    return re.sub(r"[_-]+", " ", path.stem).strip() or "Unknown track"


def probe_audio(path: Path, ffprobe: str = "/opt/homebrew/bin/ffprobe") -> dict[str, Any]:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:format_tags=title,artist,genre",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    format_payload = payload.get("format", {})
    tags = {str(key).lower(): value for key, value in format_payload.get("tags", {}).items()}
    return {
        "title": str(tags.get("title") or _display_title(path)),
        "artist": str(tags.get("artist") or "Unknown artist"),
        "genre": str(tags.get("genre") or "Unknown"),
        "durationSeconds": round(float(format_payload.get("duration") or 0), 3),
    }


@dataclass(frozen=True)
class CatalogTrack:
    id: str
    path: Path
    title: str
    artist: str
    genre: str
    duration_seconds: float
    bpm: float
    analysis_status: str

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "genre": self.genre,
            "durationSeconds": self.duration_seconds,
            "bpm": self.bpm,
            "analysisStatus": self.analysis_status,
        }


def scan_catalog(
    library_root: Path,
    analysis_root: Path,
    *,
    metadata_reader: Callable[[Path], dict[str, Any]] = probe_audio,
) -> dict[str, CatalogTrack]:
    analyses: dict[str, dict[str, Any]] = {}
    for analysis_path in analysis_root.glob("*/track-analysis-v1.json"):
        payload = json.loads(analysis_path.read_text(encoding="utf-8"))
        analyses[str(payload["audio_sha256"])] = payload
    tracks: dict[str, CatalogTrack] = {}
    if not library_root.exists():
        return tracks
    for path in sorted(library_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        track_id = sha256_file(path)
        metadata = metadata_reader(path)
        analysis = analyses.get(track_id, {})
        tracks[track_id] = CatalogTrack(
            id=track_id,
            path=path.resolve(),
            title=str(metadata["title"]),
            artist=str(metadata["artist"]),
            genre=str(metadata["genre"]),
            duration_seconds=float(analysis.get("duration_seconds", metadata["durationSeconds"])),
            bpm=float(analysis.get("rhythm", {}).get("bpm", 0)),
            analysis_status="ready" if analysis else "pending",
        )
    return tracks


def parse_curator_response(
    text: str,
    *,
    allowed_ids: set[str],
    count: int,
) -> tuple[tuple[str, ...], str]:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Meta curator did not return a JSON object")
        text = text[start : end + 1]
    payload = json.loads(text)
    if set(payload) != {"ordered_track_ids", "reason"}:
        raise ValueError("Meta curator fields do not match the strict schema")
    ordered = tuple(str(value) for value in payload["ordered_track_ids"])
    if len(ordered) != count or len(set(ordered)) != count:
        raise ValueError(f"Meta curator must select exactly {count} unique tracks")
    if any(track_id not in allowed_ids for track_id in ordered):
        raise ValueError("Meta curator selected an unknown track ID")
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValueError("Meta curator reason must be 1..500 characters")
    return ordered, reason.strip()


def curate_with_meta(
    tracks: Iterable[CatalogTrack],
    *,
    prompt: str,
    count: int,
    model: str,
    transport: Callable[[str, str], str],
) -> tuple[tuple[str, ...], str]:
    candidates = tuple(tracks)
    request = json.dumps(
        {
            "task": "curate_local_dj_set",
            "request": prompt,
            "track_count": count,
            "catalog": [track.public() for track in candidates],
            "required_output_fields": ["ordered_track_ids", "reason"],
            "model": model,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    response = transport(CURATOR_SYSTEM_INSTRUCTION, request)
    return parse_curator_response(
        response,
        allowed_ids={track.id for track in candidates},
        count=count,
    )


class SetBuilderService:
    """Thread-safe preparation queue with one bounded background worker."""

    def __init__(
        self,
        workspace: Path,
        *,
        library_root: Path | None = None,
        data_root: Path | None = None,
        metadata_reader: Callable[[Path], dict[str, Any]] = probe_audio,
        curator: Callable[[tuple[CatalogTrack, ...], str, int], tuple[tuple[str, ...], str]] | None = None,
        post_curator: Callable[[tuple[CatalogTrack, ...], str, int], tuple[tuple[str, ...], str]] | None = None,
        analyzer: Callable[[CatalogTrack], dict[str, Any] | None] | None = None,
        pair_builder: Callable[[str, str], int | tuple[int, str | None]] | None = None,
        start_worker: bool = True,
    ) -> None:
        self.workspace = workspace.resolve()
        self.data_root = (data_root or workspace).resolve()
        self.library_root = (library_root or self.data_root / "local-assets/samples").resolve()
        self.analysis_root = self.data_root / "artifacts/time-map"
        self.pair_root = self.data_root / "artifacts/live-performance/dashboard-pairs"
        self.state_path = self.data_root / "artifacts/live-performance/set-builder-state-v1.json"
        self.saved_root = self.data_root / "artifacts/live-performance/saved-sets"
        self.lock = threading.RLock()
        self.jobs: queue.Queue[tuple[str, ...]] = queue.Queue()
        self.queued_jobs: set[tuple[str, ...]] = set()
        self.metadata_reader = metadata_reader
        self.catalog = scan_catalog(
            self.library_root,
            self.analysis_root,
            metadata_reader=metadata_reader,
        )
        self.curator = curator or self._meta_curator
        self.post_curator = post_curator or self._meta_post_curator
        self.analyzer = analyzer or self._run_analyzer
        self.analysis_engine = "custom" if analyzer else "all_in_one_plus_dsp"
        self.pair_builder = pair_builder or self._build_pair
        self.state = self._initial_state()
        if self.state["orderedTrackIds"]:
            self._schedule_current()
            self._recompute_status()
        if start_worker:
            threading.Thread(target=self._worker, daemon=True, name="set-builder-worker").start()

    def _initial_state(self) -> dict[str, Any]:
        state = {
            "status": "idle",
            "prompt": "",
            "model": None,
            "reason": None,
            "error": None,
            "queueRevision": 0,
            "orderedTrackIds": [],
            "trackStatuses": {track_id: track.analysis_status for track_id, track in self.catalog.items()},
            "analysisOrigins": {track_id: "available" if track.analysis_status == "ready" else "pending"
                                for track_id, track in self.catalog.items()},
            "analysisRuns": {},
            "transitions": [],
            "updatedAt": utc_now(),
            "activationReady": False,
            "preparationReady": False,
            "postAnalysisPlanning": "idle",
            "pairAnalysisVersion": PAIR_VERSION,
        }
        if self.state_path.exists():
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            saved_ids = [str(value) for value in saved.get("orderedTrackIds", [])]
            if saved_ids and all(track_id in self.catalog for track_id in saved_ids):
                pair_version_changed = saved.get("pairAnalysisVersion") != PAIR_VERSION
                state.update(saved)
                for run in state.get("analysisRuns", {}).values():
                    if run.get("status") == "running":
                        run["status"] = "interrupted"
                state["pairAnalysisVersion"] = PAIR_VERSION
                state["trackStatuses"] = {
                    track_id: self.catalog[track_id].analysis_status for track_id in self.catalog
                }
                self._refresh_edges(state)
                for edge in state["transitions"]:
                    if pair_version_changed or edge["status"] in {"analyzing", "planning"}:
                        edge["status"] = "pending"
                        edge["candidateCount"] = 0
                        edge["reason"] = None
                if pair_version_changed:
                    state["postAnalysisPlanning"] = "idle"
                    state["error"] = None
        return state

    def _meta_curator(
        self, tracks: tuple[CatalogTrack, ...], prompt: str, count: int, *, stage: str = "curation"
    ) -> tuple[tuple[str, ...], str]:
        from meta_planner import load_dotenv

        load_dotenv(self.data_root / ".env")
        load_dotenv(self.workspace / "submission/.env")
        load_dotenv(self.workspace / ".env")
        load_dotenv(self.workspace / "experiments/meta-dj-agent/.env")
        # Preparation has no live cue deadline. Do not raise the rolling
        # transition planner's global budget or add retries to its transport.
        config = replace(MetaConfig.from_environment(),
            max_tokens=int(os.environ.get("META_CURATOR_MAX_TOKENS", "4096")),
            timeout_seconds=float(os.environ.get("META_CURATOR_TIMEOUT_SECONDS", "60")))
        attempts = [config]
        if config.max_tokens < 8192:
            attempts.append(replace(config, max_tokens=min(8192, config.max_tokens * 2)))
        for index, attempt in enumerate(attempts):
            call = {"id": f"curator-{time.time_ns()}", "stage": stage,
                    "buildId": self.state.get("buildId"), "attempt": index + 1, "model": attempt.model, "maxTokens": attempt.max_tokens,
                    "reasoningEffort": attempt.reasoning_effort, "startedAt": utc_now(), "status": "calling"}
            with self.lock:
                self.state["curatorCalls"] = [*self.state.get("curatorCalls", []), call][-6:]
            def record(event):
                with self.lock:
                    call.update(event)
            try:
                result = curate_with_meta(tracks, prompt=prompt, count=count, model=attempt.model,
                    transport=OpenAICompatibleTransport(attempt, on_event=record))
            except MetaOutputLimitError:
                record({"status": "truncated", "finishedAt": utc_now()})
                if index + 1 == len(attempts):
                    raise
            except Exception:
                record({"status": "failed", "finishedAt": utc_now()})
                raise
            else:
                record({"status": "completed", "finishedAt": utc_now()})
                with self.lock:
                    self.state["model"] = attempt.model
                return result
        raise RuntimeError("Meta curator did not complete")

    def _run_analyzer(self, track: CatalogTrack) -> dict[str, Any]:
        python = self.workspace / "experiments/time-map/.venv/bin/python"
        script = self.workspace / "experiments/time-map/unified_analysis.py"
        output = self.analysis_root / track.id / "track-analysis-v1.json"
        # All-In-One itself reuses source.json unless --overwrite is requested.
        # Running unified_analysis.py therefore does not always run the network.
        neural_cached = (self.analysis_root / track.id / "allin1-raw/source.json").is_file()
        output.parent.mkdir(parents=True, exist_ok=True)
        with (output.parent / "analyzer.log").open("a", encoding="utf-8") as log:
            subprocess.run(
                [str(python), str(script), str(track.path), "--output", str(output),
                 "--work-directory", str(output.parent), "--device", "cpu", "--limit", "5"],
                check=True, cwd=str(output.parent), stdout=log, stderr=subprocess.STDOUT,
            )
        return {"neuralNet": "cache" if neural_cached else "computed"}

    @staticmethod
    def _tempo_compatible(source: CatalogTrack, target: CatalogTrack) -> bool:
        if source.bpm <= 0 or target.bpm <= 0:
            return False
        return abs(source.bpm / target.bpm - 1.0) <= 0.06

    def _blocked_count(self, ordered: Iterable[str]) -> int:
        return sum(
            not self._tempo_compatible(self.catalog[source_id], self.catalog[target_id])
            for source_id, target_id in adjacent_pairs(ordered)
        )

    def _meta_post_curator(
        self, tracks: tuple[CatalogTrack, ...], prompt: str, count: int
    ) -> tuple[tuple[str, ...], str]:
        compatibility = {
            track.id: [
                candidate.id
                for candidate in tracks
                if candidate.id != track.id and self._tempo_compatible(track, candidate)
            ]
            for track in tracks
        }
        request = (
            f"{prompt}\n\nThis is the post-analysis pass. Every BPM is now measured. "
            "Prioritize executable adjacency above the earlier speculative order and minimize "
            "the number of adjacent pairs not present in compatible_followers. The catalog may "
            "make a fully compatible ordering impossible; do not invent compatibility. "
            f"compatible_followers={json.dumps(compatibility, separators=(',', ':'))}"
        )
        return self._meta_curator(tracks, request, count, stage="post_analysis")

    def _build_pair(self, source_id: str, target_id: str) -> tuple[int, str | None]:
        catalog = AnalysisCatalog.from_root(self.analysis_root)
        result = PairAnalysisCache(self.pair_root, catalog).ensure(source_id, target_id)
        reason = (
            "clean_break_required"
            if result.available_modes == ("clean_break",)
            else None
        )
        return result.candidate_count, reason

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)
        if self.state.get("status") == "ready" and self.state.get("activationReady") and not self.state.get("loadedFromSavedSet"):
            build_id = self.state.get("buildId")
            if build_id and re.fullmatch(r"build-\d+", build_id):
                self.saved_root.mkdir(parents=True, exist_ok=True)
                saved = self.saved_root / f"{build_id}-q{int(self.state['queueRevision'])}.json"
                if not saved.exists():
                    payload = {"id": saved.stem, "savedAt": utc_now(), "state": self.state,
                               "analysisKeys": {t: json.loads((self.analysis_root / t / "track-analysis-v1.json").read_text())["analysis_cache_key"] for t in self.state["orderedTrackIds"]},
                               "tracks": [self.catalog[t].public() for t in self.state["orderedTrackIds"]]}
                    pending = saved.with_suffix(".json.tmp")
                    pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    pending.replace(saved)

    def saved_sets(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.saved_root.glob("build-*-q*.json"), reverse=True):
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                state = saved["state"]
                ids = state["orderedTrackIds"]
                available = all(t in self.catalog and (self.analysis_root / t / "track-analysis-v1.json").is_file() for t in ids)
                result.append({"id": path.stem, "savedAt": saved["savedAt"], "prompt": state["prompt"],
                               "trackCount": len(ids), "tracks": saved["tracks"], "available": available,
                               "reason": None if available else "A song or its analysis is missing from this local library."})
            except (KeyError, TypeError, ValueError, OSError):
                continue
        return result

    def load_saved_set(self, saved_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"build-\d+-q\d+", saved_id):
            raise ValueError("Invalid saved playlist ID")
        with self.lock:
            if self.state.get("status") in {"curating", "analyzing", "planning"} or self.queued_jobs:
                raise ValueError("Wait for current preparation to finish before loading a playlist")
            path = self.saved_root / f"{saved_id}.json"
            if not path.is_file():
                raise ValueError("Saved playlist was not found")
            record = json.loads(path.read_text(encoding="utf-8"))
            saved = record["state"]
            catalog = scan_catalog(self.library_root, self.analysis_root, metadata_reader=self.metadata_reader)
            ids = saved["orderedTrackIds"]
            if len(ids) < 2 or len(set(ids)) != len(ids) or any(t not in catalog or catalog[t].analysis_status != "ready" for t in ids):
                raise ValueError("Saved playlist requires songs and analyses that are no longer available")
            if saved.get("pairAnalysisVersion") != PAIR_VERSION:
                raise ValueError("Saved playlist uses an older analysis version; build a new playlist")
            keys = {t: json.loads((self.analysis_root / t / "track-analysis-v1.json").read_text())["analysis_cache_key"] for t in ids}
            if record.get("analysisKeys") != keys:
                raise ValueError("Analysis changed since this playlist was saved; build a new playlist")
            self.catalog = catalog
            self.state = {**saved, "queueRevision": int(self.state["queueRevision"]) + 1,
                          "loadedFromSavedSet": saved_id, "analysisOrigins": {t: "cache" for t in ids},
                          "analysisRuns": {}, "trackStatuses": {t: track.analysis_status for t, track in catalog.items()},
                          "transitions": [], "error": None}
            self._refresh_edges()
            self._schedule_current()
            self._recompute_status()
            self._persist()
            return self.snapshot()

    def _edge(self, source_id: str, target_id: str, status: str = "pending") -> dict[str, Any]:
        current_state = getattr(self, "state", {})
        return {
            "id": f"prepare-{source_id[:8]}-{target_id[:8]}",
            "sourceTrackId": source_id,
            "targetTrackId": target_id,
            "status": status,
            "candidateCount": 0,
            "standbyReady": False,
            "selectedBy": None,
            "reason": None,
            "queueRevision": int(current_state.get("queueRevision", 0)),
        }

    def _refresh_edges(self, state: dict[str, Any] | None = None) -> None:
        target = state or self.state
        old = {
            (edge["sourceTrackId"], edge["targetTrackId"]): edge
            for edge in target.get("transitions", [])
        }
        revision = int(target.get("queueRevision", 0))
        edges = []
        for source_id, target_id in adjacent_pairs(target.get("orderedTrackIds", [])):
            edge = dict(old.get((source_id, target_id), self._edge(source_id, target_id)))
            edge["queueRevision"] = revision
            edge["standbyReady"] = (
                edge.get("status") in {"analyzed", "ready"}
                and int(edge.get("candidateCount", 0)) > 0
            )
            edges.append(edge)
        target["transitions"] = edges

    def _enqueue(self, job: tuple[str, ...]) -> None:
        if job not in self.queued_jobs:
            self.queued_jobs.add(job)
            self.jobs.put(job)

    def _schedule_current(self) -> None:
        ordered = [str(value) for value in self.state["orderedTrackIds"]]
        for track_id in ordered:
            if self.state["trackStatuses"].get(track_id) != "ready":
                self._enqueue(("track", track_id))
        for source_id, target_id in adjacent_pairs(ordered):
            if (
                self.state["trackStatuses"].get(source_id) == "ready"
                and self.state["trackStatuses"].get(target_id) == "ready"
            ):
                edge = self._find_edge(source_id, target_id)
                if edge and edge["status"] not in {"analyzed", "ready", "blocked", "analyzing"}:
                    self._enqueue(("pair", source_id, target_id))
        tracks_ready = bool(ordered) and all(
            self.state["trackStatuses"].get(track_id) == "ready" for track_id in ordered
        )
        edges_settled = len(self.state["transitions"]) == max(0, len(ordered) - 1) and all(
            edge["status"] in {"analyzed", "blocked"} for edge in self.state["transitions"]
        )
        if (
            tracks_ready
            and edges_settled
            and any(
                edge["status"] == "blocked" or edge.get("reason") == "clean_break_required"
                for edge in self.state["transitions"]
            )
            and self.state.get("postAnalysisPlanning") in {"idle", "pending"}
        ):
            self.state["postAnalysisPlanning"] = "pending"
            self._enqueue(("post_plan",))

    def _find_edge(self, source_id: str, target_id: str) -> dict[str, Any] | None:
        return next(
            (
                edge
                for edge in self.state["transitions"]
                if edge["sourceTrackId"] == source_id and edge["targetTrackId"] == target_id
            ),
            None,
        )

    def _recompute_status(self) -> None:
        ordered = self.state["orderedTrackIds"]
        tracks_ready = bool(ordered) and all(
            self.state["trackStatuses"].get(track_id) == "ready" for track_id in ordered
        )
        edges_ready = len(self.state["transitions"]) == max(0, len(ordered) - 1) and all(
            edge["status"] == "analyzed" for edge in self.state["transitions"]
        )
        needs_post_plan = any(
            edge.get("reason") == "clean_break_required" for edge in self.state["transitions"]
        )
        post_plan_ready = self.state.get("postAnalysisPlanning") not in {"running", "failed"} and (
            not needs_post_plan or self.state.get("postAnalysisPlanning") == "completed"
        )
        preparation_ready = tracks_ready and edges_ready and post_plan_ready and not self.state.get("error")
        self.state["preparationReady"] = preparation_ready
        self.state["activationReady"] = preparation_ready
        if self.state.get("error"):
            self.state["status"] = "error"
        elif self.state["preparationReady"]:
            self.state["status"] = "ready"
        elif self.state.get("postAnalysisPlanning") == "running":
            self.state["status"] = "planning"
        elif (
            tracks_ready
            and self.state.get("postAnalysisPlanning") in {"completed", "failed"}
            and self.state["transitions"]
            and all(edge["status"] in {"analyzed", "blocked"} for edge in self.state["transitions"])
            and any(edge["status"] == "blocked" for edge in self.state["transitions"])
        ):
            self.state["status"] = "blocked"
        elif ordered:
            self.state["status"] = "analyzing"
        else:
            self.state["status"] = "idle"
        self.state["updatedAt"] = utc_now()

    def build(self, prompt: str, count: int) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("set request is empty")
        if count < 2 or count > 50:
            raise ValueError("track count must be between 2 and 50")
        candidates = tuple(self.catalog.values())
        if count > len(candidates):
            raise ValueError(f"catalog contains only {len(candidates)} tracks")
        with self.lock:
            self.state["buildId"] = f"build-{time.time_ns()}"
            self.state.pop("loadedFromSavedSet", None)
            self.state["status"] = "curating"
            self.state["error"] = None
            self.state["prompt"] = prompt
            self.state["updatedAt"] = utc_now()
        try:
            ordered, reason = self.curator(candidates, prompt, count)
        except Exception as error:
            with self.lock:
                self.state["status"] = "error"
                self.state["error"] = str(error)
                self._persist()
            raise
        with self.lock:
            self.state["queueRevision"] = int(self.state["queueRevision"]) + 1
            self.state["orderedTrackIds"] = list(ordered)
            self.state["analysisOrigins"] = {t: "cache" if self.catalog[t].analysis_status == "ready" else "pending" for t in ordered}
            self.state["analysisRuns"] = {}
            self.state["reason"] = reason
            self.state["error"] = None
            self.state["transitions"] = []
            self.state["postAnalysisPlanning"] = "pending"
            self._refresh_edges()
            self._schedule_current()
            self._recompute_status()
            self._persist()
            return self.snapshot()

    def reorder(self, ordered_track_ids: Iterable[str]) -> dict[str, Any]:
        requested = tuple(str(value) for value in ordered_track_ids)
        with self.lock:
            current = tuple(str(value) for value in self.state["orderedTrackIds"])
            if not current:
                raise ValueError("there is no prepared queue to reorder")
            if len(requested) != len(current) or set(requested) != set(current):
                raise ValueError("reorder must contain exactly the current queue tracks")
            if requested == current:
                return self.snapshot()
            old_edges = {
                (edge["sourceTrackId"], edge["targetTrackId"]): edge
                for edge in self.state["transitions"]
            }
            self.state["queueRevision"] = int(self.state["queueRevision"]) + 1
            self.state["orderedTrackIds"] = list(requested)
            self.state.pop("loadedFromSavedSet", None)
            self.state["transitions"] = []
            for source_id, target_id in adjacent_pairs(requested):
                retained = old_edges.get((source_id, target_id))
                self.state["transitions"].append(
                    dict(retained) if retained else self._edge(source_id, target_id)
                )
            self._refresh_edges()
            self.state["error"] = None
            self._schedule_current()
            self._recompute_status()
            self._persist()
            return self.snapshot()

    def retry_post_plan(self) -> dict[str, Any]:
        """Resume only the failed curation pass; keep analyses and active playback."""
        with self.lock:
            ordered = self.state["orderedTrackIds"]
            if (self.state.get("postAnalysisPlanning") != "failed" or not ordered
                or any(self.state["trackStatuses"].get(t) != "ready" for t in ordered)
                or ("post_plan",) in self.queued_jobs):
                raise ValueError("No settled, failed post-analysis plan is available to retry.")
            self.state["postAnalysisPlanning"] = "pending"
            self.state["error"] = None
            self._enqueue(("post_plan",))
            self._recompute_status()
            self._persist()
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            ordered = [str(value) for value in self.state["orderedTrackIds"]]
            return {
                **json.loads(json.dumps(self.state)),
                "catalogCount": len(self.catalog),
                "queue": [
                    {
                        **self.catalog[track_id].public(),
                        "analysisStatus": self.state["trackStatuses"].get(track_id, "pending"),
                        "analysisOrigin": self.state.get("analysisOrigins", {}).get(track_id, "unknown"),
                        "analysisRun": json.loads(json.dumps(self.state.get("analysisRuns", {}).get(track_id))),
                    }
                    for track_id in ordered
                ],
            }

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                if job[0] == "track":
                    self._work_track(job[1])
                elif job[0] == "pair":
                    self._work_pair(job[1], job[2])
                elif job[0] == "post_plan":
                    self._work_post_plan()
            finally:
                with self.lock:
                    self.queued_jobs.discard(job)
                self.jobs.task_done()

    def _work_track(self, track_id: str) -> None:
        with self.lock:
            if track_id not in self.state["orderedTrackIds"]:
                return
            self.state["trackStatuses"][track_id] = "analyzing"
            run = {"jobId": f"analysis-{time.time_ns()}", "buildId": self.state.get("buildId"),
                   "engine": self.analysis_engine, "runner": "local_cpu" if self.analysis_engine == "all_in_one_plus_dsp" else "custom",
                   "status": "running", "startedAt": utc_now(), "finishedAt": None,
                   "durationSeconds": None, "neuralNet": "unverified"}
            self.state.setdefault("analysisRuns", {})[track_id] = run
            started = time.monotonic()
            self._recompute_status()
            self._persist()
        try:
            result = self.analyzer(self.catalog[track_id])
            with self.lock:
                run.update(status="completed", finishedAt=utc_now(), durationSeconds=round(time.monotonic() - started, 3))
                if isinstance(result, dict) and result.get("neuralNet") in {"cache", "computed"}:
                    run["neuralNet"] = result["neuralNet"]
                original = self.catalog[track_id]
                analysis_path = self.analysis_root / track_id / "track-analysis-v1.json"
                analysis = json.loads(analysis_path.read_text(encoding="utf-8")) if analysis_path.exists() else {}
                self.catalog[track_id] = CatalogTrack(
                    **{
                        **original.__dict__,
                        "duration_seconds": float(analysis.get("duration_seconds", original.duration_seconds)),
                        "bpm": float(analysis.get("rhythm", {}).get("bpm", original.bpm)),
                        "analysis_status": "ready",
                    }
                )
                self.state["trackStatuses"][track_id] = "ready"
                self.state.setdefault("analysisOrigins", {})[track_id] = "computed" if run["buildId"] == self.state.get("buildId") else "cache"
                self._schedule_current()
                self._recompute_status()
                self._persist()
        except Exception as error:
            with self.lock:
                run.update(status="failed", finishedAt=utc_now(), durationSeconds=round(time.monotonic() - started, 3))
                self.state["trackStatuses"][track_id] = "failed"
                self.state["error"] = f"{self.catalog[track_id].title}: {error}"
                self._recompute_status()
                self._persist()

    def _work_pair(self, source_id: str, target_id: str) -> None:
        with self.lock:
            edge = self._find_edge(source_id, target_id)
            if edge is None or edge["status"] in {"analyzed", "ready"}:
                return
            edge["status"] = "analyzing"
            self._persist()
        try:
            result = self.pair_builder(source_id, target_id)
            count, strategy_reason = result if isinstance(result, tuple) else (result, None)
            with self.lock:
                edge = self._find_edge(source_id, target_id)
                if edge is not None:
                    edge["candidateCount"] = int(count)
                    edge["status"] = "analyzed" if count else "blocked"
                    edge["standbyReady"] = bool(count)
                    edge["reason"] = strategy_reason if count else "no_transition_candidates"
                self._schedule_current()
                self._recompute_status()
                self._persist()
        except Exception as error:
            with self.lock:
                edge = self._find_edge(source_id, target_id)
                if edge is not None:
                    edge["status"] = "blocked"
                    edge["reason"] = str(error)
                self.state["error"] = f"transition {source_id[:8]}→{target_id[:8]}: {error}"
                self._recompute_status()
                self._persist()

    def _work_post_plan(self) -> None:
        with self.lock:
            if self.state.get("postAnalysisPlanning") not in {"idle", "pending"}:
                return
            current = tuple(str(value) for value in self.state["orderedTrackIds"])
            revision = int(self.state["queueRevision"])
            before_blocked = self._blocked_count(current)
            self.state["postAnalysisPlanning"] = "running"
            self._recompute_status()
            self._persist()
            tracks = tuple(self.catalog[track_id] for track_id in current)
            prompt = str(self.state["prompt"])
        try:
            ordered, reason = self.post_curator(tracks, prompt, len(tracks))
            after_blocked = self._blocked_count(ordered)
            if after_blocked > before_blocked:
                raise ValueError(
                    f"post-analysis order made tempo bridges worse ({before_blocked} → {after_blocked})"
                )
            with self.lock:
                if int(self.state["queueRevision"]) != revision or tuple(self.state["orderedTrackIds"]) != current:
                    return
                self.state["postAnalysisPlanning"] = "completed"
                self.state["reason"] = reason
                self.state["error"] = None
                if ordered != current:
                    self.reorder(ordered)
                else:
                    self._recompute_status()
                    self._persist()
        except Exception as error:
            with self.lock:
                if int(self.state["queueRevision"]) != revision or tuple(self.state["orderedTrackIds"]) != current:
                    return
                self.state["postAnalysisPlanning"] = "failed"
                self.state["error"] = f"post-analysis Meta planning failed: {error}"
                self._recompute_status()
                self._persist()

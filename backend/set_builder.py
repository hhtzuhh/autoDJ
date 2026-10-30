"""Local catalog, Meta set curation, and background DJ analysis jobs.

This service prepares a future performance queue.  It deliberately does not
mutate the queue currently executing in Mixxx; activation belongs to the live
plan lifecycle once every required track and adjacent edge is ready.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
from typing import Any, Callable, Iterable

from meta_planner import MetaConfig, OpenAICompatibleTransport
from pair_analyzer import PAIR_VERSION
from transition_pipeline import AnalysisCatalog, PairAnalysisCache, adjacent_pairs, utc_now


AUDIO_SUFFIXES = {".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}
CURATOR_SYSTEM_INSTRUCTION = """
You curate a DJ set from a bounded local catalog. Select and order exactly the
requested number of supplied track IDs. Prefer tracks matching the request,
then create a coherent energy arc using only the provided metadata. Never
invent IDs or facts. Return exactly one JSON object with only
ordered_track_ids and reason. ordered_track_ids must contain unique IDs.
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
        metadata_reader: Callable[[Path], dict[str, Any]] = probe_audio,
        curator: Callable[[tuple[CatalogTrack, ...], str, int], tuple[tuple[str, ...], str]] | None = None,
        post_curator: Callable[[tuple[CatalogTrack, ...], str, int], tuple[tuple[str, ...], str]] | None = None,
        analyzer: Callable[[CatalogTrack], None] | None = None,
        pair_builder: Callable[[str, str], int | tuple[int, str | None]] | None = None,
        start_worker: bool = True,
    ) -> None:
        self.workspace = workspace.resolve()
        self.library_root = (library_root or workspace / "local-assets/samples").resolve()
        self.analysis_root = workspace / "artifacts/time-map"
        self.pair_root = workspace / "artifacts/live-performance/dashboard-pairs"
        self.state_path = workspace / "artifacts/live-performance/set-builder-state-v1.json"
        self.lock = threading.RLock()
        self.jobs: queue.Queue[tuple[str, ...]] = queue.Queue()
        self.queued_jobs: set[tuple[str, ...]] = set()
        self.catalog = scan_catalog(
            self.library_root,
            self.analysis_root,
            metadata_reader=metadata_reader,
        )
        self.curator = curator or self._meta_curator
        self.post_curator = post_curator or self._meta_post_curator
        self.analyzer = analyzer or self._run_analyzer
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
        self, tracks: tuple[CatalogTrack, ...], prompt: str, count: int
    ) -> tuple[tuple[str, ...], str]:
        from meta_planner import load_dotenv

        load_dotenv(self.workspace / "submission/.env")
        load_dotenv(self.workspace / ".env")
        load_dotenv(self.workspace / "experiments/meta-dj-agent/.env")
        config = MetaConfig.from_environment()
        ordered, reason = curate_with_meta(
            tracks,
            prompt=prompt,
            count=count,
            model=config.model,
            transport=OpenAICompatibleTransport(config),
        )
        self.state["model"] = config.model
        return ordered, reason

    def _run_analyzer(self, track: CatalogTrack) -> None:
        python = self.workspace / "experiments/time-map/.venv/bin/python"
        script = self.workspace / "experiments/time-map/unified_analysis.py"
        output = self.analysis_root / track.id / "track-analysis-v1.json"
        subprocess.run(
            [str(python), str(script), str(track.path), "--output", str(output), "--device", "cpu", "--limit", "5"],
            check=True,
            cwd=str(script.parent),
        )

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
        return self._meta_curator(tracks, request, count)

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

    def _edge(self, source_id: str, target_id: str, status: str = "pending") -> dict[str, Any]:
        current_state = getattr(self, "state", {})
        return {
            "id": f"prepare-{source_id[:8]}-{target_id[:8]}",
            "sourceTrackId": source_id,
            "targetTrackId": target_id,
            "status": status,
            "candidateCount": 0,
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
        post_plan_ready = (
            not needs_post_plan
            or self.state.get("postAnalysisPlanning") in {"completed", "failed"}
        )
        preparation_ready = tracks_ready and edges_ready and post_plan_ready
        self.state["preparationReady"] = preparation_ready
        self.state["activationReady"] = preparation_ready and not needs_post_plan
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
            self._recompute_status()
            self._persist()
        try:
            self.analyzer(self.catalog[track_id])
            with self.lock:
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
                self._schedule_current()
                self._recompute_status()
                self._persist()
        except Exception as error:
            with self.lock:
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
                self.state["postAnalysisPlanning"] = "failed"
                self.state["error"] = f"post-analysis Meta planning failed: {error}"
                self._recompute_status()
                self._persist()

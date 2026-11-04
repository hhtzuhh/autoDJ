"""Prepare a hash-verified Mixxx set without mutating the active live queue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from setlist_service import resolve_set_list, write_mixxx_playlist
from analysis_config import analysis_root, selected_backend
from transition_pipeline import AnalysisCatalog, utc_now


@dataclass(frozen=True)
class PreparedActivation:
    queue: dict[str, Any]
    playlist_path: Path
    manifest_path: Path
    activation_path: Path

    def public(self) -> dict[str, Any]:
        return {
            "status": "playlist_ready",
            "queueRevision": int(self.queue["revision"]),
            "trackCount": len(self.queue["ordered_track_ids"]),
            "playlist": str(self.playlist_path),
            "message": "Open the generated playlist in Mixxx and select its first row, then Build performance plan.",
        }


def prepare_activation(
    workspace: Path,
    builder_state: dict[str, Any],
    *,
    active_queue_revision: int,
) -> PreparedActivation:
    if builder_state.get("status") != "ready" or not builder_state.get("preparationReady"):
        raise ValueError("prepared set analysis is not ready")
    ordered = tuple(str(value) for value in builder_state.get("orderedTrackIds", []))
    if len(ordered) < 2:
        raise ValueError("prepared set needs at least two tracks")
    if any(
        builder_state.get("trackStatuses", {}).get(track_id) != "ready"
        for track_id in ordered
    ):
        raise ValueError("prepared set contains an unanalyzed track")
    transitions = builder_state.get("transitions", [])
    if len(transitions) != len(ordered) - 1 or any(
        edge.get("status") != "analyzed" or int(edge.get("candidateCount", 0)) < 1
        for edge in transitions
    ):
        raise ValueError("prepared set contains an unprepared transition")

    runtime = workspace / "artifacts/live-performance"
    queue = {
        "schema_version": 1,
        "analysis_backend": selected_backend(),
        "revision": max(active_queue_revision + 1, int(builder_state["queueRevision"])),
        "ordered_track_ids": list(ordered),
        "updated_at": utc_now(),
    }
    catalog = AnalysisCatalog.from_root(analysis_root(workspace))
    manifest = resolve_set_list(catalog, queue, [workspace / "local-assets/samples-v3"])
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    short_hash = hashlib.sha256(json.dumps(queue, sort_keys=True).encode("utf-8")).hexdigest()[:5]
    export_name = f"prepared-tonight-set-{timestamp}-{short_hash}"
    playlist_path = runtime / f"{export_name}.m3u8"
    manifest_path = runtime / f"{export_name}-v1.json"
    activation_path = runtime / "prepared-activation-v1.json"
    write_mixxx_playlist(manifest, playlist_path, manifest_path)
    activation = {
        "schema_version": 1,
        "status": "playlist_ready",
        "created_at": utc_now(),
        "builder_revision": int(builder_state["queueRevision"]),
        "queue": queue,
        "playlist_path": str(playlist_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
    }
    temporary = activation_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(activation, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(activation_path)
    return PreparedActivation(queue, playlist_path, manifest_path, activation_path)


def load_prepared_activation(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("status") != "playlist_ready":
        raise ValueError("prepared activation is invalid")
    return payload


def promote_prepared_activation(workspace: Path, activation: dict[str, Any]) -> dict[str, Any]:
    """Atomically make a verified prepared set authoritative for live playback."""
    queue = activation.get("queue")
    if not isinstance(queue, dict) or len(queue.get("ordered_track_ids", [])) < 2:
        raise ValueError("prepared activation queue is invalid")
    runtime = workspace / "artifacts/live-performance"
    prepared_playlist = Path(str(activation["playlist_path"]))
    prepared_manifest = Path(str(activation["manifest_path"]))
    if not prepared_playlist.is_file() or not prepared_manifest.is_file():
        raise ValueError("prepared Mixxx playlist artifacts are missing")

    manifest = json.loads(prepared_manifest.read_text(encoding="utf-8"))
    manifest_ids = [str(row["track_id"]) for row in manifest.get("ordered_tracks", [])]
    if manifest_ids != [str(value) for value in queue["ordered_track_ids"]]:
        raise ValueError("prepared playlist order does not match its queue")
    if int(manifest.get("queue_revision", -1)) != int(queue["revision"]):
        raise ValueError("prepared playlist revision does not match its queue")

    queue_path = runtime / "queue-state-v1.json"
    queue_tmp = queue_path.with_suffix(".json.tmp")
    queue_tmp.write_text(
        json.dumps(queue, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path = runtime / "tonight-set-list-v1.json"
    manifest_tmp = manifest_path.with_suffix(".json.tmp")
    shutil.copyfile(prepared_manifest, manifest_tmp)
    playlist_path = runtime / "tonight-set-list.m3u8"
    playlist_tmp = playlist_path.with_suffix(".m3u8.tmp")
    shutil.copyfile(prepared_playlist, playlist_tmp)
    manifest_tmp.replace(manifest_path)
    playlist_tmp.replace(playlist_path)
    queue_tmp.replace(queue_path)
    return queue

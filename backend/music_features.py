"""Muse music descriptions, promoted from the music-feature-extractor experiment.

Same prompt and inline-audio request; standard-library HTTP matches the bridge's
dependency conventions. Model descriptions remain separate from DSP measurements.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from meta_planner import load_dotenv
from transition_pipeline import utc_now

MODEL = "muse-spark-1.2"
MAX_TOKENS = 4000
PROMPT = (Path(__file__).parent / "prompts/music_features.txt").read_text().strip()
CACHE_KEY = hashlib.sha256(json.dumps({"model": MODEL, "prompt": PROMPT, "max_tokens": MAX_TOKENS,
                                      "formatVersion": 1}, sort_keys=True).encode()).hexdigest()


class MusicFeatures:
    def __init__(self, workspace: Path, data_root: Path, transport=None):
        self.workspace, self.data_root = workspace, data_root
        self.root = data_root / "artifacts/music-features"
        self.transport = transport or self._request

    def folder(self, track_id: str) -> Path:
        if len(track_id) != 64 or any(c not in "0123456789abcdef" for c in track_id):
            raise ValueError("Invalid audio identity")
        return self.root / track_id / CACHE_KEY

    def load(self, track_id: str) -> dict:
        try:
            result = json.loads((self.folder(track_id) / "result.json").read_text())
            if isinstance(result, dict) and result.get("audioSha256") == track_id and result.get("cacheKey") == CACHE_KEY:
                if result.get("status") == "ready" and result.get("content") and result.get("finishReason") == "stop":
                    return {**result, "origin": "cache"}
                if result.get("status") == "failed":
                    return result
                if result.get("status") == "running":
                    return {**result, "status": "failed", "error": "Interrupted; select this song to retry"}
        except (OSError, ValueError, TypeError):
            pass
        return {"status": "pending", "model": MODEL, "origin": "pending"}

    def save(self, track_id: str, result: dict) -> None:
        folder = self.folder(track_id)
        folder.mkdir(parents=True, exist_ok=True)
        pending = folder / "result.tmp"
        pending.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        pending.replace(folder / "result.json")

    def _request(self, payload: dict) -> str:
        load_dotenv(self.data_root / ".env")
        key = os.environ.get("MODEL_API_KEY")
        if not key:
            raise ValueError("MODEL_API_KEY is missing")
        request = Request("https://api.meta.ai/v1/chat/completions", method="POST",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                          data=json.dumps(payload).encode())
        with urlopen(request, timeout=240) as response:
            return response.read().decode("utf-8")

    def extract(self, track, on_started=lambda result: None) -> dict:
        started = time.monotonic()
        run_id = f"music-{time.time_ns()}"
        result = {"status": "running", "model": MODEL, "origin": "computed", "jobId": run_id,
                  "audioSha256": track.id, "cacheKey": CACHE_KEY, "startedAt": utc_now(),
                  "content": None, "error": None, "prompt": PROMPT, "maxTokens": MAX_TOKENS,
                  "preprocessing": "none; entire original MP3/WAV including embedded tags", "requestStartedAt": None}
        folder = self.folder(track.id) / run_id
        try:
            audio = track.path.read_bytes()
            if hashlib.sha256(audio).hexdigest() != track.id:
                raise ValueError("Audio changed since library scan; rescan and analyze it again")
            fmt = track.path.suffix.lower().lstrip(".")
            if fmt not in {"mp3", "wav"}:
                raise ValueError("Muse audio features currently support MP3 and WAV only")
            payload = {"model": MODEL, "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}},
            ]}]}
            folder.mkdir(parents=True, exist_ok=False)
            result.update(audioBytes=len(audio), requestStartedAt=utc_now())
            self.save(track.id, result)
            on_started(dict(result))
            raw = self.transport(payload)
            (folder / "response.raw.json").write_text(raw)
            response = json.loads(raw)
            choice = response["choices"][0]
            content = choice["message"].get("content")
            result.update(finishReason=choice.get("finish_reason"), usage=response.get("usage"),
                          modelReturned=response.get("model"), responseId=response.get("id"), rawAvailable=True)
            if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
                raise ValueError("Incomplete or empty Meta answer; no music description accepted")
            result.update(status="ready", content=content)
            (folder / "content.txt").write_text(content)
        except Exception as error:
            # API errors can contain echoed payloads; never surface them or keys.
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else (
                f"Meta HTTP {error.code}" if isinstance(error, HTTPError) else f"Music analysis failed ({type(error).__name__})")
            result.update(status="failed", error=message)
        result.update(finishedAt=utc_now(), elapsedSeconds=round(time.monotonic() - started, 3))
        self.save(track.id, result)
        if folder.exists():
            (folder / "run.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
        return result

    def raw_response(self, track_id: str) -> dict:
        result = self.load(track_id)
        job_id = result.get("jobId", "")
        if not job_id.startswith("music-") or not job_id[6:].isdigit() or not result.get("rawAvailable"):
            raise ValueError("No raw response available for this song")
        return json.loads((self.folder(track_id) / job_id / "response.raw.json").read_text())

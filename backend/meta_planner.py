"""Single-call Meta selector for locally feasible A-B-C transition candidates."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


Intent = Literal["maintain_energy", "clean_break", "impact_at_handoff"]
ALLOWED_INTENTS = {"maintain_energy", "clean_break", "impact_at_handoff"}
MAX_MODEL_CANDIDATES = 10
DEFAULT_META_MODEL = "muse-spark-1.3"
DEFAULT_META_API_BASE = "https://api.meta.ai/v1"

SYSTEM_INSTRUCTION = """
You are a constrained A-B-C DJ transition planner. Local deterministic code
already rejected candidates that violate track identity, queue revision,
transition duration, minimum dwell, or B entry-before-exit ordering. Choose
exactly one supplied candidate_id. Never invent beat indexes, track IDs,
transition lengths, scores, or candidate IDs. Respond with exactly one JSON
object containing only selected_candidate_id, intent, use_bass_swap,
confidence, and reason. The reason must be concise and grounded in supplied
measurements.
""".strip()


@dataclass(frozen=True)
class MetaConfig:
    api_key: str
    model: str = DEFAULT_META_MODEL
    api_base: str = DEFAULT_META_API_BASE
    timeout_seconds: float = 30.0

    @classmethod
    def from_environment(cls) -> "MetaConfig":
        api_key = os.environ.get("MODEL_API_KEY", "").strip()
        if not api_key:
            raise ValueError("MODEL_API_KEY is unset")
        return cls(
            api_key=api_key,
            model=os.environ.get("META_MODEL", DEFAULT_META_MODEL).strip(),
            api_base=os.environ.get(
                "META_API_BASE", DEFAULT_META_API_BASE
            ).strip().rstrip("/"),
            timeout_seconds=float(os.environ.get("META_TIMEOUT_SECONDS", "30")),
        )


@dataclass(frozen=True)
class MetaDecision:
    selected_candidate_id: str
    intent: Intent
    use_bass_swap: bool
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MetaSelection:
    decision: MetaDecision
    candidate: dict[str, Any]
    model: str
    candidate_count: int


def _candidate_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "rolling-" + hashlib.sha256(encoded).hexdigest()[:16]


def _pair_score(pair: dict[str, Any], bars: int, intent: Intent) -> float:
    return float(
        pair["transition_metrics_by_bars"][str(bars)]["strategy_scores"][intent]
    )


def build_rolling_candidates(
    immediate_pairing: dict[str, Any],
    following_pairing: dict[str, Any],
    *,
    intent: Intent = "maintain_energy",
    minimum_dwell_beats: int = 128,
    limit: int = MAX_MODEL_CANDIDATES,
) -> list[dict[str, Any]]:
    if intent not in ALLOWED_INTENTS:
        raise ValueError(f"unsupported intent: {intent}")
    if minimum_dwell_beats < 0:
        raise ValueError("minimum dwell must not be negative")
    if limit < 1 or limit > MAX_MODEL_CANDIDATES:
        raise ValueError(f"candidate limit must be 1..{MAX_MODEL_CANDIDATES}")
    candidates = []
    for immediate in immediate_pairing.get("pairs", []):
        for immediate_bars in immediate["transition_bar_options"]:
            target_entry = int(immediate["b_entry"]["beat_index"])
            minimum_exit = target_entry + int(immediate_bars) * 4 + minimum_dwell_beats
            for following in following_pairing.get("pairs", []):
                target_exit = int(following["a_exit"]["beat_index"])
                if target_exit < minimum_exit:
                    continue
                for following_bars in following["transition_bar_options"]:
                    identity = {
                        "ab_pair_rank": int(immediate["rank"]),
                        "ab_transition_bars": int(immediate_bars),
                        "a_exit_beat_index": int(immediate["a_exit"]["beat_index"]),
                        "b_entry_beat_index": target_entry,
                        "bc_pair_rank": int(following["rank"]),
                        "bc_transition_bars": int(following_bars),
                        "b_exit_beat_index": target_exit,
                        "c_entry_beat_index": int(following["b_entry"]["beat_index"]),
                    }
                    immediate_score = _pair_score(immediate, int(immediate_bars), intent)
                    following_score = _pair_score(following, int(following_bars), intent)
                    candidates.append(
                        {
                            "candidate_id": _candidate_id(identity),
                            **identity,
                            "minimum_exit_beat_index": minimum_exit,
                            "immediate_intent_score": immediate_score,
                            "following_intent_score": following_score,
                            "combined_score": 0.65 * immediate_score
                            + 0.35 * following_score,
                            "immediate_low_frequency_separation": float(
                                immediate["transition_metrics_by_bars"][
                                    str(immediate_bars)
                                ]["low_frequency_separation"]
                            ),
                        }
                    )
    return sorted(
        candidates,
        key=lambda row: float(row["combined_score"]),
        reverse=True,
    )[:limit]


def select_future_fallback(
    candidates: list[dict[str, Any]],
    *,
    live_beat: int,
    runway_beats: int = 16,
) -> dict[str, Any] | None:
    """Return the best still-armable locally validated rolling candidate."""
    return next(
        (
            candidate
            for candidate in candidates
            if int(candidate["a_exit_beat_index"]) > live_beat + runway_beats
        ),
        None,
    )


def prompt_for(
    candidates: list[dict[str, Any]],
    *,
    queue_revision: int,
    intent: Intent,
    track_ids: tuple[str, str, str],
) -> str:
    if not candidates:
        raise ValueError("no feasible A-B-C candidates")
    bounded = candidates[:MAX_MODEL_CANDIDATES]
    return json.dumps(
        {
            "task": "select_one_temporally_feasible_abc_transition",
            "queue_revision": queue_revision,
            "intent": intent,
            "tracks": {"a": track_ids[0], "b": track_ids[1], "c": track_ids[2]},
            "candidate_data": bounded,
            "required_output_fields": [
                "selected_candidate_id",
                "intent",
                "use_bass_swap",
                "confidence",
                "reason",
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_decision(text: str) -> MetaDecision:
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model did not return a JSON object")
        text = text[start : end + 1]
    payload = json.loads(text)
    required = {
        "selected_candidate_id",
        "intent",
        "use_bass_swap",
        "confidence",
        "reason",
    }
    if set(payload) != required:
        raise ValueError("Meta decision fields do not match the strict schema")
    if not isinstance(payload["selected_candidate_id"], str) or not payload[
        "selected_candidate_id"
    ]:
        raise ValueError("selected_candidate_id must be a non-empty string")
    if payload["intent"] not in ALLOWED_INTENTS:
        raise ValueError("Meta decision has an unsupported intent")
    if not isinstance(payload["use_bass_swap"], bool):
        raise ValueError("use_bass_swap must be boolean")
    confidence = float(payload["confidence"])
    if confidence < 0 or confidence > 1:
        raise ValueError("confidence must be between 0 and 1")
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValueError("reason must be 1..500 characters")
    return MetaDecision(
        selected_candidate_id=payload["selected_candidate_id"],
        intent=payload["intent"],
        use_bass_swap=payload["use_bass_swap"],
        confidence=confidence,
        reason=reason.strip(),
    )


def validate_decision(
    decision: MetaDecision,
    candidates: list[dict[str, Any]],
    *,
    intent: Intent,
) -> dict[str, Any]:
    if decision.intent != intent:
        raise ValueError("Meta decision intent does not match the request")
    allowed = {row["candidate_id"]: row for row in candidates[:MAX_MODEL_CANDIDATES]}
    if decision.selected_candidate_id not in allowed:
        raise ValueError("Meta selected an unknown candidate")
    candidate = allowed[decision.selected_candidate_id]
    if int(candidate["b_exit_beat_index"]) < int(
        candidate["minimum_exit_beat_index"]
    ):
        raise ValueError("Meta selected a candidate that violates minimum dwell")
    return candidate


class OpenAICompatibleTransport:
    def __init__(self, config: MetaConfig) -> None:
        self.config = config

    def __call__(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self.config.model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode("utf-8")
        request = Request(
            f"{self.config.api_base}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise RuntimeError(f"Meta API returned HTTP {error.code}") from error
        except URLError as error:
            raise RuntimeError(f"Meta API request failed: {error.reason}") from error
        try:
            return str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("Meta API response did not contain assistant text") from error


def select_with_meta(
    candidates: list[dict[str, Any]],
    *,
    queue_revision: int,
    intent: Intent,
    track_ids: tuple[str, str, str],
    model: str,
    transport: Callable[[str, str], str],
) -> MetaSelection:
    user_prompt = prompt_for(
        candidates,
        queue_revision=queue_revision,
        intent=intent,
        track_ids=track_ids,
    )
    response = transport(SYSTEM_INSTRUCTION, user_prompt)
    decision = parse_decision(response)
    candidate = validate_decision(decision, candidates, intent=intent)
    return MetaSelection(decision, candidate, model, min(len(candidates), 10))


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE settings without adding a runtime dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Let Meta select one locally feasible A-B-C candidate."
    )
    parser.add_argument("--immediate-pairing", type=Path, required=True)
    parser.add_argument("--following-pairing", type=Path, required=True)
    parser.add_argument("--queue-revision", type=int, required=True)
    parser.add_argument("--intent", choices=sorted(ALLOWED_INTENTS), default="maintain_energy")
    parser.add_argument("--dotenv", type=Path, default=Path(".env"))
    parser.add_argument("tracks", nargs=3)
    args = parser.parse_args()
    load_dotenv(args.dotenv)
    config = MetaConfig.from_environment()
    immediate = json.loads(args.immediate_pairing.read_text(encoding="utf-8"))
    following = json.loads(args.following_pairing.read_text(encoding="utf-8"))
    candidates = build_rolling_candidates(
        immediate,
        following,
        intent=args.intent,
    )
    selection = select_with_meta(
        candidates,
        queue_revision=args.queue_revision,
        intent=args.intent,
        track_ids=tuple(args.tracks),  # type: ignore[arg-type]
        model=config.model,
        transport=OpenAICompatibleTransport(config),
    )
    print(
        json.dumps(
            {
                "model": selection.model,
                "candidate_count": selection.candidate_count,
                "decision": selection.decision.to_dict(),
                "selected_candidate": selection.candidate,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

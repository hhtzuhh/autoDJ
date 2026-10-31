"""Single-call Meta selector for locally feasible A-B-C transition candidates."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import math
import time
from typing import Any, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


Intent = Literal["maintain_energy", "clean_break", "impact_at_handoff"]
ALLOWED_INTENTS = {"maintain_energy", "clean_break", "impact_at_handoff"}
MAX_MODEL_CANDIDATES = 10
MINIMUM_DWELL_BEATS = 32
LATE_ENTRY_PENALTY = 0.08
DEFAULT_META_MODEL = "muse-spark-1.3"
DEFAULT_META_API_BASE = "https://api.meta.ai/v1"

SYSTEM_INSTRUCTION = """
You are a constrained rolling DJ transition planner. Local deterministic code
already rejected candidates that violate track identity, queue revision,
transition duration, minimum dwell, or entry-before-exit ordering. Choose
exactly one supplied candidate_id. Never invent beat indexes, track IDs,
transition lengths, scores, or candidate IDs. Respond with exactly one JSON
object containing only selected_candidate_id, intent, use_bass_swap,
confidence, and reason. The reason must be concise and grounded in supplied
measurements.
Local code has already checked feasibility and calculated scores. Make a brief
comparison, select one candidate, and keep the reason to one short sentence.
When musical scores are comparable, prefer the candidate with earlier target
entries so the following transition retains runway. Never trade chronology for
a higher score: every later source exit must remain after its target entry.
""".strip()


@dataclass(frozen=True)
class MetaConfig:
    api_key: str
    model: str = DEFAULT_META_MODEL
    api_base: str = DEFAULT_META_API_BASE
    timeout_seconds: float = 30.0
    reasoning_effort: str = "low"
    max_tokens: int = 2048

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Meta timeout must be positive and finite")
        if self.reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("invalid META_REASONING_EFFORT")
        if self.max_tokens < 256:
            raise ValueError("META_MAX_TOKENS must be at least 256 (includes reasoning)")

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
            reasoning_effort=os.environ.get("META_REASONING_EFFORT", "low"),
            max_tokens=int(os.environ.get("META_MAX_TOKENS", "2048")),
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
    minimum_dwell_beats: int = MINIMUM_DWELL_BEATS,
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


def _edge_options(
    pairing: dict[str, Any],
    *,
    intent: Intent,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Normalize sync and clean-break pairs into one bounded edge contract."""
    rows: list[dict[str, Any]] = []
    source = pairing.get("pairs") or pairing.get("clean_break_pairs") or []
    for pair in source:
        mode = "clean_break" if pair.get("transition_mode") == "clean_break" else "beat_sync_crossfade"
        bars_options = [0] if mode == "clean_break" else pair["transition_bar_options"]
        for bars in bars_options:
            score = (
                float(pair["heuristic_score"])
                if mode == "clean_break"
                else _pair_score(pair, int(bars), intent)
            )
            rows.append(
                {
                    "mode": mode,
                    "pair_rank": int(pair["rank"]),
                    "source_exit_beat_index": int(pair["a_exit"]["beat_index"]),
                    "target_entry_beat_index": int(pair["b_entry"]["beat_index"]),
                    "transition_bars": int(bars),
                    "score": score,
                    "pair": pair,
                }
            )
    if not rows:
        return []
    entries = [int(row["target_entry_beat_index"]) for row in rows]
    earliest, latest = min(entries), max(entries)
    span = max(1, latest - earliest)
    for row in rows:
        relative_lateness = (
            int(row["target_entry_beat_index"]) - earliest
        ) / span
        row["entry_lateness_penalty"] = LATE_ENTRY_PENALTY * relative_lateness
        row["planning_score"] = float(row["score"]) - float(
            row["entry_lateness_penalty"]
        )
    ranked = sorted(
        rows,
        key=lambda row: float(row["planning_score"]),
        reverse=True,
    )
    # Retain musical leaders AND timing alternatives before joint feasibility.
    # A high-score-only cap can remove every early-entry / late-exit option.
    selected = ranked[:limit]
    for row in sorted(rows, key=lambda row: (
        int(row["target_entry_beat_index"]) + int(row["transition_bars"]) * 4,
        -int(row["source_exit_beat_index"]),
        -float(row["planning_score"]),
    )):
        if row not in selected:
            selected.append(row)
        if len(selected) >= 2 * limit:
            break
    return selected


def build_three_transition_candidates(
    pairings: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    *,
    intent: Intent = "maintain_energy",
    minimum_dwell_beats: int = MINIMUM_DWELL_BEATS,
    limit: int | None = MAX_MODEL_CANDIDATES,
) -> list[dict[str, Any]]:
    """Build one A-B-C-D plan candidate containing exactly three edges."""
    if intent not in ALLOWED_INTENTS:
        raise ValueError(f"unsupported intent: {intent}")
    return build_transition_window_candidates(
        pairings, intent=intent, minimum_dwell_beats=minimum_dwell_beats, limit=limit
    )


def build_transition_window_candidates(
    pairings: tuple[dict[str, Any], ...],
    *,
    intent: Intent = "maintain_energy",
    minimum_dwell_beats: int = MINIMUM_DWELL_BEATS,
    limit: int | None = MAX_MODEL_CANDIDATES,
) -> list[dict[str, Any]]:
    """Jointly plan up to three edges, including a shorter queue tail."""
    from itertools import product

    if intent not in ALLOWED_INTENTS or not 1 <= len(pairings) <= 3:
        raise ValueError("invalid transition window")
    edge_sets = [_edge_options(pairing, intent=intent) for pairing in pairings]
    candidates = []
    for edges in product(*edge_sets):
        if any(
            int(right["source_exit_beat_index"])
            < int(left["target_entry_beat_index"])
            + int(left["transition_bars"]) * 4 + minimum_dwell_beats
            for left, right in zip(edges, edges[1:])
        ):
            continue
        transitions = [
            {key: value for key, value in edge.items() if key != "pair"}
            for edge in edges
        ]
        candidates.append({
            "candidate_id": _candidate_id({"transitions": transitions}),
            "transitions": transitions,
            "combined_score": sum(
                weight * float(edge["planning_score"])
                for weight, edge in zip((0.5, 0.3, 0.2), edges)
            ),
        })
    return sorted(candidates, key=lambda row: float(row["combined_score"]), reverse=True)[:limit]


def build_append_candidates(
    pairing: dict[str, Any],
    *,
    prior_target_entry_beat: int,
    prior_transition_bars: int,
    intent: Intent = "maintain_energy",
    minimum_dwell_beats: int = MINIMUM_DWELL_BEATS,
    limit: int = MAX_MODEL_CANDIDATES,
) -> list[dict[str, Any]]:
    """Build only the new tail edge while preserving the first two edges."""
    minimum_exit = (
        prior_target_entry_beat + prior_transition_bars * 4 + minimum_dwell_beats
    )
    rows = []
    for edge in _edge_options(pairing, intent=intent, limit=25):
        if int(edge["source_exit_beat_index"]) < minimum_exit:
            continue
        transition = {key: value for key, value in edge.items() if key != "pair"}
        identity = {"append_transition": transition}
        rows.append(
            {
                "candidate_id": _candidate_id(identity),
                "transition": transition,
                "combined_score": float(edge["planning_score"]),
            }
        )
    return sorted(rows, key=lambda row: row["combined_score"], reverse=True)[:limit]


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
    track_ids: tuple[str, ...],
) -> str:
    if not candidates:
        raise ValueError("no feasible A-B-C candidates")
    bounded = candidates[:MAX_MODEL_CANDIDATES]
    return json.dumps(
        {
            "task": "select_one_temporally_feasible_rolling_plan",
            "queue_revision": queue_revision,
            "intent": intent,
            "tracks": {
                chr(ord("a") + index): track_id
                for index, track_id in enumerate(track_ids)
            },
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
    if "minimum_exit_beat_index" in candidate and int(candidate["b_exit_beat_index"]) < int(candidate["minimum_exit_beat_index"]):
        raise ValueError("Meta selected a candidate that violates minimum dwell")
    return candidate


class OpenAICompatibleTransport:
    def __init__(self, config: MetaConfig, *, on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.config = config
        self.on_event = on_event or (lambda event: None)

    def __call__(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self.config.model,
                "temperature": 0.2,
                "reasoning_effort": self.config.reasoning_effort,
                "max_tokens": self.config.max_tokens,
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
        started = time.monotonic()
        self.on_event({"phase": "waiting_for_response", "requestBytes": len(body)})
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                self.on_event({"phase": "reading_response", "responseHeadersSeconds": round(time.monotonic() - started, 3)})
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise RuntimeError(f"Meta API returned HTTP {error.code}") from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TimeoutError("Meta connection timed out") from error
            raise RuntimeError(f"Meta API request failed: {error.reason}") from error
        usage = payload.get("usage") or {}
        self.on_event({
            "phase": "validating_response", "responseSeconds": round(time.monotonic() - started, 3),
            "promptTokens": usage.get("prompt_tokens"),
            "completionTokens": usage.get("completion_tokens"),
            "reasoningTokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        })
        try:
            choice = payload["choices"][0]
            if choice.get("finish_reason") == "length":
                raise RuntimeError("Meta output token limit reached; no plan accepted")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("Meta API response did not contain assistant text")
            return content
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("Meta API response did not contain assistant text") from error


def select_with_meta(
    candidates: list[dict[str, Any]],
    *,
    queue_revision: int,
    intent: Intent,
    track_ids: tuple[str, ...],
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

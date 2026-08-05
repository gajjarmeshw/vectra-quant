"""Suggestion contract + validator. Design §6.2, build plan Phase 3 §2.

LLM output is untrusted input (§0.6). The schema has NO field for size, floors, or
limits — those tokens do not exist in its vocabulary, so they cannot leak into
execution even if the model is fully compromised.

One repair retry, then drop.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from sentinel.logging_setup import get

log = get("llm.schema")

SUGGESTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"enum": ["SUGGEST", "NO_TRADE"]},
        "instrument": {"enum": ["SENSEX", "NIFTY"]},
        "direction": {"enum": ["CE", "PE"]},
        "strike_offset": {"enum": ["ATM", "ATM+1", "ATM-1", "ATM+2", "ATM-2"]},
        "entry_zone": {
            "type": "object",
            "additionalProperties": False,
            "required": ["low", "high"],
            "properties": {
                "low": {"type": "number", "exclusiveMinimum": 0},
                "high": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        "stop_loss_premium": {"type": "number", "exclusiveMinimum": 0},
        "target_premium": {"type": "number", "exclusiveMinimum": 0},
        "time_stop_minutes": {"type": "integer", "minimum": 1, "maximum": 360},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "thesis": {"type": "string", "maxLength": 240},
        "invalidation": {"type": "string", "maxLength": 240},
        "risk_reward": {"type": "number", "minimum": 0},
        "reason": {"type": "string", "maxLength": 240},   # for NO_TRADE
    },
}

# Fields a SUGGEST must carry. NO_TRADE needs none of them.
SUGGEST_REQUIRED = (
    "instrument", "direction", "strike_offset", "entry_zone", "stop_loss_premium",
    "target_premium", "time_stop_minutes", "confidence", "thesis", "invalidation",
    "risk_reward",
)

_validator = Draft202012Validator(SUGGESTION_SCHEMA)


@dataclass
class ValidationResult:
    ok: bool
    data: dict[str, Any] | None = None
    errors: list[str] = None      # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    @property
    def is_suggest(self) -> bool:
        return bool(self.data and self.data.get("action") == "SUGGEST")

    @property
    def error_text(self) -> str:
        return "; ".join(self.errors)[:400]


def extract_json(raw: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response.

    Models wrap JSON in prose or fences despite instructions. Tolerating that is
    not the same as tolerating bad *content* — the schema still decides.
    """
    if not raw:
        return None
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        end = -1
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return None
        text = text[start:end]
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


def validate(payload: Any) -> ValidationResult:
    """Schema + cross-field checks. Returns errors specific enough to repair from."""
    if isinstance(payload, str):
        payload = extract_json(payload)
    if not isinstance(payload, dict):
        return ValidationResult(False, None, ["response was not a JSON object"])

    errors = [
        f"{'/'.join(str(p) for p in e.path) or '(root)'}: {e.message}"
        for e in sorted(_validator.iter_errors(payload), key=lambda e: list(e.path))
    ]
    if errors:
        return ValidationResult(False, None, errors)

    action = payload.get("action")
    if action == "NO_TRADE":
        return ValidationResult(True, payload, [])

    missing = [f for f in SUGGEST_REQUIRED if payload.get(f) in (None, "")]
    if missing:
        return ValidationResult(False, None, [f"SUGGEST missing: {', '.join(missing)}"])

    zone = payload["entry_zone"]
    low, high = float(zone["low"]), float(zone["high"])
    sl = float(payload["stop_loss_premium"])
    tgt = float(payload["target_premium"])

    if low > high:
        errors.append("entry_zone.low must be <= entry_zone.high")
    if sl >= low:
        errors.append(f"stop_loss_premium ({sl}) must be below entry_zone.low ({low})")
    if tgt <= high:
        errors.append(f"target_premium ({tgt}) must be above entry_zone.high ({high})")

    # RR must be internally consistent: the model cannot claim 3.0 on a 1.2 setup.
    mid = (low + high) / 2.0
    risk = mid - sl
    reward = tgt - mid
    if risk > 0:
        implied = reward / risk
        claimed = float(payload["risk_reward"])
        if abs(implied - claimed) > max(0.35, implied * 0.25):
            errors.append(
                f"risk_reward {claimed:.2f} contradicts prices (implied {implied:.2f})"
            )

    if errors:
        return ValidationResult(False, None, errors)
    return ValidationResult(True, payload, [])


def repair_instruction(result: ValidationResult) -> str:
    """The single retry prompt. Names the faults instead of asking vaguely again."""
    return (
        "Your previous response was rejected by a strict validator.\n"
        f"Errors: {result.error_text}\n\n"
        "Return ONLY a JSON object matching the contract exactly — no prose, no code "
        "fences. If you cannot satisfy every constraint, return "
        '{"action":"NO_TRADE","reason":"<short reason>"} instead. NO_TRADE is always '
        "an acceptable answer."
    )


def implied_rr(entry_mid: float, stop: float, target: float) -> float:
    risk = entry_mid - stop
    return round((target - entry_mid) / risk, 2) if risk > 0 else 0.0

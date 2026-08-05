"""LLM router: laptop Claude bridge -> Groq -> fail closed. Build plan Phase 3 §4.

Fail-closed is the whole design (§0.2): if both providers are unreachable or the
output will not validate, the event is logged and NOTHING happens. No suggestion is
always safe; an invented one never is.

Every attempt writes an `llm_calls` row with provider, model, latency and tokens.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from sentinel.core.session_clock import session_date
from sentinel.db import LlmCall, session, utcnow
from sentinel.llm.prompts import build_messages
from sentinel.llm.schema import ValidationResult, repair_instruction, validate
from sentinel.logging_setup import get

log = get("llm.router")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


@dataclass
class LlmOutcome:
    ok: bool
    data: dict[str, Any] | None = None
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    error: str = ""
    repaired: bool = False

    @property
    def is_suggest(self) -> bool:
        return bool(self.data and self.data.get("action") == "SUGGEST")


class LlmRouter:
    def __init__(
        self,
        *,
        bridge_url: str = "",
        groq_api_key: str = "",
        groq_model: str = "llama-3.3-70b-versatile",
        groq_fallback_model: str = "openai/gpt-oss-120b",
        timeout_s: int = 10,
        bridge_retries: int = 2,
        daily_cap: int = 10,
    ):
        self.bridge_url = (bridge_url or "").rstrip("/")
        self.groq_api_key = groq_api_key
        self.groq_model = groq_model
        self.groq_fallback_model = groq_fallback_model
        self.timeout_s = timeout_s
        self.bridge_retries = bridge_retries
        self.daily_cap = daily_cap
        self._calls_today = 0
        self._calls_day = session_date()

    # ------------------------------------------------------------------ budget
    def _cap_reached(self, purpose: str) -> bool:
        today = session_date()
        if today != self._calls_day:
            self._calls_day = today
            self._calls_today = 0
        # Reports are scheduled and must not be starved by suggestion traffic.
        if purpose != "suggestion":
            return False
        return self._calls_today >= self.daily_cap

    @property
    def calls_today(self) -> int:
        if session_date() != self._calls_day:
            return 0
        return self._calls_today

    def _log_call(self, *, purpose: str, provider: str, model: str, latency_ms: int,
                  ok: bool, error: str = "", event_kind: str = "",
                  prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        with session() as s:
            s.add(LlmCall(
                ts=utcnow(), session_date=session_date(), purpose=purpose,
                provider=provider, model=model, latency_ms=latency_ms,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                ok=ok, error=error[:500], event_kind=event_kind,
            ))

    # ------------------------------------------------------------------ providers
    def _call_bridge(self, messages: list[dict[str, str]]) -> tuple[str, int, dict]:
        if not self.bridge_url:
            raise RuntimeError("bridge url not configured")
        started = time.monotonic()
        with httpx.Client(timeout=self.timeout_s) as c:
            r = c.post(f"{self.bridge_url}/suggest", json={"messages": messages})
            r.raise_for_status()
            body = r.json()
        latency = int((time.monotonic() - started) * 1000)
        text = body.get("content") or body.get("text") or ""
        if not text and isinstance(body, dict) and "action" in body:
            import json as _json
            text = _json.dumps(body)      # bridge already returned the object
        return text, latency, {}

    def _call_groq(self, messages: list[dict[str, str]], model: str) -> tuple[str, int, dict]:
        if not self.groq_api_key:
            raise RuntimeError("groq api key not configured")
        started = time.monotonic()
        with httpx.Client(timeout=self.timeout_s) as c:
            r = c.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {self.groq_api_key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                    "max_tokens": 700,
                    "response_format": {"type": "json_object"},
                },
            )
            r.raise_for_status()
            body = r.json()
        latency = int((time.monotonic() - started) * 1000)
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        return text, latency, usage

    # ------------------------------------------------------------------ orchestration
    def suggest(self, snapshot: dict[str, Any], *, purpose: str = "suggestion",
                event_kind: str = "") -> LlmOutcome:
        if self._cap_reached(purpose):
            log.info("LLM daily cap reached", extra={"cap": self.daily_cap})
            self._log_call(purpose=purpose, provider="none", model="", latency_ms=0,
                           ok=False, error="daily cap reached", event_kind=event_kind)
            return LlmOutcome(False, error="daily cap reached")

        messages = build_messages(snapshot)
        if purpose == "suggestion":
            self._calls_today += 1

        attempts: list[tuple[str, str]] = []
        if self.bridge_url:
            attempts.extend(("bridge", "claude-bridge") for _ in range(self.bridge_retries))
        if self.groq_api_key:
            attempts.append(("groq", self.groq_model))
            attempts.append(("groq", self.groq_fallback_model))

        if not attempts:
            log.error("no LLM provider configured — failing closed")
            self._log_call(purpose=purpose, provider="none", model="", latency_ms=0,
                           ok=False, error="no provider configured", event_kind=event_kind)
            return LlmOutcome(False, error="no provider configured")

        last_error = ""
        for provider, model in attempts:
            try:
                if provider == "bridge":
                    text, latency, usage = self._call_bridge(messages)
                else:
                    text, latency, usage = self._call_groq(messages, model)
            except Exception as exc:
                last_error = f"{provider}/{model}: {str(exc)[:160]}"
                log.warning("LLM provider failed", extra={"provider": provider,
                                                          "model": model,
                                                          "error": str(exc)[:200]})
                self._log_call(purpose=purpose, provider=provider, model=model,
                               latency_ms=0, ok=False, error=last_error,
                               event_kind=event_kind)
                continue

            result = validate(text)
            if result.ok:
                self._log_call(purpose=purpose, provider=provider, model=model,
                               latency_ms=latency, ok=True, event_kind=event_kind,
                               prompt_tokens=int(usage.get("prompt_tokens", 0)),
                               completion_tokens=int(usage.get("completion_tokens", 0)))
                return LlmOutcome(True, result.data, provider, model, latency)

            # One repair retry, then this provider is done (§0.6).
            log.warning("LLM output rejected, attempting one repair",
                        extra={"provider": provider, "errors": result.error_text})
            repaired = self._repair(messages, result, provider, model)
            if repaired is not None:
                self._log_call(purpose=purpose, provider=provider, model=model,
                               latency_ms=latency, ok=True,
                               error="repaired after validation failure",
                               event_kind=event_kind)
                return LlmOutcome(True, repaired, provider, model, latency, repaired=True)

            last_error = f"{provider}/{model}: {result.error_text}"
            self._log_call(purpose=purpose, provider=provider, model=model,
                           latency_ms=latency, ok=False, error=last_error,
                           event_kind=event_kind)

        log.error("all LLM providers failed — fail closed, no suggestion",
                  extra={"last_error": last_error})
        return LlmOutcome(False, error=last_error or "all providers failed")

    def _repair(self, messages: list[dict[str, str]], result: ValidationResult,
                provider: str, model: str) -> dict[str, Any] | None:
        retry_messages = [*messages, {"role": "user", "content": repair_instruction(result)}]
        try:
            if provider == "bridge":
                text, _lat, _u = self._call_bridge(retry_messages)
            else:
                text, _lat, _u = self._call_groq(retry_messages, model)
        except Exception as exc:
            log.warning("repair attempt failed", extra={"error": str(exc)[:160]})
            return None
        second = validate(text)
        if second.ok:
            return second.data
        log.warning("repair still invalid — dropping", extra={"errors": second.error_text})
        return None

    # ------------------------------------------------------------------ health
    def health(self) -> dict[str, Any]:
        out = {"bridge": "unconfigured", "groq": "unconfigured",
               "calls_today": self.calls_today, "cap": self.daily_cap}
        if self.bridge_url:
            try:
                with httpx.Client(timeout=3) as c:
                    r = c.get(f"{self.bridge_url}/health")
                out["bridge"] = "up" if r.status_code < 400 else f"http {r.status_code}"
            except Exception as exc:
                out["bridge"] = f"down ({type(exc).__name__})"
        if self.groq_api_key:
            try:
                with httpx.Client(timeout=5) as c:
                    r = c.get("https://api.groq.com/openai/v1/models",
                              headers={"Authorization": f"Bearer {self.groq_api_key}"})
                out["groq"] = "up" if r.status_code < 400 else f"http {r.status_code}"
            except Exception as exc:
                out["groq"] = f"down ({type(exc).__name__})"
        return out

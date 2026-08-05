"""Master kill switch. Build plan Phase 2 §4.

DB-persisted so it survives a restart — a switch that forgets it was off is not a
switch. OFF means: no LLM calls, no suggestions, no system-initiated orders.
Re-enabling requires the caller to have typed "ENABLE" exactly. Every flip is logged.

Risk-reducing actions (square-off, SL attach, cancels) are deliberately still
permitted while OFF: turning the system off must never strand an open position.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from sentinel.db import Violation, session, state_get, state_set, utcnow
from sentinel.logging_setup import get

log = get("core.killswitch")

KEY = "kill_switch"
HISTORY_KEY = "kill_switch_history"
ENABLE_PHRASE = "ENABLE"


class KillSwitch:
    def __init__(self) -> None:
        if state_get(KEY, "") == "":
            state_set(KEY, "ON")

    @property
    def is_on(self) -> bool:
        """True = system ENABLED. Defaults to ON only on a fresh database."""
        return state_get(KEY, "ON").upper() == "ON"

    @property
    def is_off(self) -> bool:
        return not self.is_on

    def _record(self, action: str, reason: str) -> None:
        try:
            hist = json.loads(state_get(HISTORY_KEY, "[]"))
        except json.JSONDecodeError:
            hist = []
        hist.append({
            "ts": datetime.now(UTC).isoformat(),
            "action": action,
            "reason": reason,
        })
        state_set(HISTORY_KEY, json.dumps(hist[-200:]))

    def turn_off(self, reason: str, *, square_off: bool = False) -> dict[str, object]:
        was_on = self.is_on
        state_set(KEY, "OFF")
        self._record("OFF", reason)
        log.warning("KILL SWITCH OFF", extra={"reason": reason, "square_off": square_off,
                                              "was_on": was_on})
        return {"ok": True, "state": "OFF", "square_off_requested": square_off,
                "changed": was_on}

    def turn_on(self, typed: str, reason: str = "") -> dict[str, object]:
        """Requires the exact phrase. Deliberate friction — §4.5 layer 4."""
        if (typed or "").strip() != ENABLE_PHRASE:
            log.warning("kill switch re-enable refused", extra={"typed_len": len(typed or "")})
            return {"ok": False, "state": "OFF",
                    "error": f'must type "{ENABLE_PHRASE}" exactly to re-enable'}
        was_off = self.is_off
        state_set(KEY, "ON")
        self._record("ON", reason or "typed ENABLE")
        log.warning("KILL SWITCH ON", extra={"reason": reason, "was_off": was_off})
        return {"ok": True, "state": "ON", "changed": was_off}

    def history(self, limit: int = 50) -> list[dict[str, str]]:
        try:
            return json.loads(state_get(HISTORY_KEY, "[]"))[-limit:]
        except json.JSONDecodeError:
            return []

    def guard_entry(self, session_date: str) -> bool:
        """Call before any system-initiated ENTRY. False = do nothing, and log it."""
        if self.is_on:
            return True
        with session() as s:
            s.add(Violation(
                ts=utcnow(), session_date=session_date, kind="ENTRY_WHILE_KILLED",
                detail="entry attempted while kill switch OFF; blocked",
            ))
        log.warning("entry blocked by kill switch", extra={"session_date": session_date})
        return False

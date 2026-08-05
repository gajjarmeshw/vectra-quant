"""Web Push via VAPID. Build plan Phase 4 §3, design §7 taxonomy.

iOS >= 16.4 only delivers push to a home-screen-installed PWA, and only over a valid
certificate. Copy style is verb-first with numbers in the message (§2).

A failed push must never break the trading loop, so everything here swallows its
errors after logging. A dead subscription is pruned on 404/410.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from pywebpush import WebPushException, webpush

from sentinel.db import PushSub, session
from sentinel.logging_setup import get

log = get("api.push")

# Design §7 / build plan Phase 4 §3.
KINDS = ("SUGGESTION", "FLOOR_WARNING", "STATE_CHANGE", "GUARDIAN", "SYSTEM", "REPORT")


@dataclass
class PushResult:
    sent: int
    pruned: int
    failed: int


# A repeat of the SAME message is suppressed for this long. FLOOR_WARNING is sent from
# the P&L tick, which runs every 2s: without this, hovering Rs.200 from the floor for
# five minutes fired 150 notifications. Per-kind so a suggestion is never delayed.
REPEAT_COOLDOWN_S = {
    "FLOOR_WARNING": 300,
    "SYSTEM": 300,
    "STATE_CHANGE": 60,
    "GUARDIAN": 60,
    "SUGGESTION": 0,        # each suggestion is a distinct message; never suppress
    "REPORT": 0,
}
DEFAULT_REPEAT_COOLDOWN_S = 120


class Pusher:
    def __init__(self, *, public_key: str, private_key: str, subject: str):
        self.public_key = public_key
        self.private_key = private_key
        self.subject = subject
        self._lock = threading.Lock()
        # (kind, message) -> monotonic seconds of the last delivery
        self._last_sent: dict[tuple[str, str], float] = {}
        self.suppressed = 0

    def _throttled(self, kind: str, message: str) -> bool:
        window = REPEAT_COOLDOWN_S.get(kind, DEFAULT_REPEAT_COOLDOWN_S)
        if window <= 0:
            return False
        key = (kind, message)
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < window:
                return True
            self._last_sent[key] = now
            # Bound the map: trading days are short, but a long-running process
            # should not accumulate keys forever.
            if len(self._last_sent) > 500:
                cutoff = now - max(REPEAT_COOLDOWN_S.values() or [window])
                self._last_sent = {k: v for k, v in self._last_sent.items() if v > cutoff}
        return False

    @property
    def configured(self) -> bool:
        return bool(self.private_key and self.subject)

    # ------------------------------------------------------------------ subscriptions
    def subscribe(self, endpoint: str, p256dh: str, auth: str) -> bool:
        if not (endpoint and p256dh and auth):
            return False
        with session() as s:
            existing = s.get(PushSub, endpoint)
            if existing:
                existing.p256dh = p256dh
                existing.auth = auth
            else:
                s.add(PushSub(endpoint=endpoint, p256dh=p256dh, auth=auth))
        log.info("push subscription stored")
        return True

    def unsubscribe(self, endpoint: str) -> bool:
        with session() as s:
            row = s.get(PushSub, endpoint)
            if row is None:
                return False
            s.delete(row)
        return True

    def subscription_count(self) -> int:
        with session() as s:
            return s.query(PushSub).count()

    # ------------------------------------------------------------------ send
    def send(self, kind: str, message: str, data: dict[str, Any] | None = None,
             *, title: str = "SENTINEL", urgent: bool = False) -> PushResult:
        if kind not in KINDS:
            log.warning("unknown push kind", extra={"kind": kind})
        if not self.configured:
            log.info("push not configured; skipping", extra={"kind": kind,
                                                             "message": message[:80]})
            return PushResult(0, 0, 0)

        if self._throttled(kind, message):
            self.suppressed += 1
            log.debug("duplicate push suppressed", extra={"kind": kind,
                                                          "message": message[:80]})
            return PushResult(0, 0, 0)

        payload = json.dumps({
            "title": title, "body": message, "kind": kind,
            "data": data or {}, "urgent": urgent,
        })

        with session() as s:
            subs = [(r.endpoint, r.p256dh, r.auth) for r in s.query(PushSub).all()]

        if not subs:
            log.info("no push subscriptions", extra={"kind": kind})
            return PushResult(0, 0, 0)

        sent = pruned = failed = 0
        for endpoint, p256dh, auth in subs:
            try:
                with self._lock:
                    webpush(
                        subscription_info={
                            "endpoint": endpoint,
                            "keys": {"p256dh": p256dh, "auth": auth},
                        },
                        data=payload,
                        vapid_private_key=self.private_key,
                        vapid_claims={"sub": self.subject},
                        ttl=120 if urgent else 600,
                    )
                sent += 1
            except WebPushException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                if status in (404, 410):
                    self.unsubscribe(endpoint)
                    pruned += 1
                    log.info("pruned dead push subscription", extra={"status": status})
                else:
                    failed += 1
                    log.warning("push failed", extra={"status": status,
                                                      "error": str(exc)[:160]})
            except Exception as exc:
                failed += 1
                log.warning("push error", extra={"error": str(exc)[:160]})

        log.info("push dispatched", extra={"kind": kind, "sent": sent,
                                           "pruned": pruned, "failed": failed})
        return PushResult(sent, pruned, failed)

    # ------------------------------------------------------------------ taxonomy helpers
    def suggestion(self, instrument: str, direction: str, strike: float,
                   confidence: int, risk: float, sid: str) -> PushResult:
        return self.send("SUGGESTION", (
            f"{instrument} {strike:.0f} {direction} — conf {confidence}, "
            f"risk Rs.{risk:.0f}. Expires in 90s."
        ), {"suggestion_id": sid}, urgent=True)

    def floor_warning(self, day_pnl: float, floor: float, distance: float) -> PushResult:
        return self.send("FLOOR_WARNING", (
            f"Rs.{distance:.0f} from your floor. "
            f"dayPnL {day_pnl:+,.0f}, floor {floor:+,.0f}."
        ), {"day_pnl": day_pnl, "floor": floor}, urgent=True)

    def state_change(self, state: str, floor: float, day_pnl: float) -> PushResult:
        return self.send("STATE_CHANGE", (
            f"{state} — floor {floor:+,.0f}, dayPnL {day_pnl:+,.0f}."
        ), {"state": state, "floor": floor})

    def guardian(self, message: str, data: dict[str, Any] | None = None) -> PushResult:
        return self.send("GUARDIAN", message, data, urgent=True)

    def system(self, message: str, data: dict[str, Any] | None = None) -> PushResult:
        return self.send("SYSTEM", message, data, urgent=True)

    def report(self, message: str, data: dict[str, Any] | None = None) -> PushResult:
        return self.send("REPORT", message, data)

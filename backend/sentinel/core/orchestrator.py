"""Orchestrator — single owner of the RiskEngine. Build plan Phase 2 §1.

One engine instance per session. Everything that can change `dayPnL` funnels through
here so there is exactly one place that decides state, floors, and locks.

Crash safety: every state change is written to `system_state`, and `rearm()` rebuilds
the engine from `trades` + `pnl_curve` on boot. A restart mid-session must produce an
identical FSM, not a fresh NORMAL day — otherwise a reboot silently refunds the loss
limit, which is the worst possible bug in this system.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sentinel.core.session_clock import (
    now_ist,
    session_date,
    week_start,
)
from sentinel.db import PnlCurve, Trade, Violation, session, state_get, state_set, utcnow
from sentinel.logging_setup import get
from sentinel.risk_engine import DayState, EngineEvent, RiskConfig, RiskEngine, TradeResult

log = get("core.orchestrator")

STATE_KEY = "fsm_state"
WEEK_KEY = "week_state"
RED_DAYS_KEY = "consecutive_red_days"
FORCE_PAPER_KEY = "force_paper_next_session"


@dataclass
class FsmSnapshot:
    session_date: str
    state: str
    day_pnl: float
    realized: float
    unrealized: float
    peak: float
    floor: float
    trades_taken: int
    trade_cap: int
    consecutive_losses: int
    bonus_unlocked: bool
    lock_reason: str
    updated_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class WeekState:
    week: str
    pnl: float = 0.0
    locked: bool = False
    red_days: int = 0
    days: dict[str, float] = field(default_factory=dict)


class Orchestrator:
    def __init__(
        self,
        cfg: RiskConfig,
        *,
        week_loss_limit_mult: float = 2.5,
        red_days_to_paper: int = 3,
        kill_switch_on: bool = True,
        on_engine_event: Callable[[EngineEvent, FsmSnapshot], None] | None = None,
        on_state_change: Callable[[FsmSnapshot], None] | None = None,
    ):
        self.cfg = cfg
        self.engine = RiskEngine(cfg, kill_switch_on=kill_switch_on)
        self.week_loss_limit = week_loss_limit_mult * cfg.loss_limit
        self.red_days_to_paper = red_days_to_paper
        self._on_engine_event = on_engine_event
        self._on_state_change = on_state_change
        self._lock = threading.RLock()
        self._unrealized = 0.0
        self._last_state = self.engine.state
        self._last_floor = self.engine.floor
        self.session_date = session_date()
        self.forced_paper = False
        # True when the process started after the square-off time. Only the FIRST
        # on_clock is suppressed; a normal 15:10 during a live session still exits.
        self._booted_after_cutoff = now_ist().time() >= cfg.squareoff_at

    # ------------------------------------------------------------------ snapshots
    def snapshot(self) -> FsmSnapshot:
        e = self.engine
        return FsmSnapshot(
            session_date=self.session_date,
            state=e.state.value,
            day_pnl=round(e.day_pnl, 2),
            realized=round(e.realized, 2),
            unrealized=round(self._unrealized, 2),
            peak=round(e.peak, 2),
            floor=round(e.floor, 2),
            trades_taken=e.trades_taken,
            trade_cap=e.trade_cap,
            consecutive_losses=e.consecutive_losses,
            bonus_unlocked=e.bonus_unlocked,
            lock_reason=e.lock_reason,
            updated_at=datetime.now(UTC).isoformat(),
        )

    def _persist(self, *, curve: bool = True) -> FsmSnapshot:
        snap = self.snapshot()
        state_set(STATE_KEY, json.dumps(snap.as_dict()))
        if curve:
            with session() as s:
                s.add(PnlCurve(
                    ts=utcnow(), session_date=self.session_date,
                    realized=snap.realized, unrealized=snap.unrealized,
                    day_pnl=snap.day_pnl, peak=snap.peak, floor=snap.floor,
                    state=snap.state,
                ))
        return snap

    def _emit(self, ev: EngineEvent) -> FsmSnapshot:
        snap = self._persist()
        state_changed = (self.engine.state != self._last_state
                         or abs(self.engine.floor - self._last_floor) > 1e-9)
        if state_changed:
            log.warning("FSM change", extra={
                "from": self._last_state.value, "to": self.engine.state.value,
                "floor": snap.floor, "day_pnl": snap.day_pnl,
            })
            self._last_state = self.engine.state
            self._last_floor = self.engine.floor
            if self._on_state_change:
                try:
                    self._on_state_change(snap)
                except Exception as exc:  # pragma: no cover
                    log.warning("state-change hook failed", extra={"error": str(exc)[:160]})
        if (ev.lock or ev.square_off) and self._on_engine_event:
            try:
                self._on_engine_event(ev, snap)
            except Exception as exc:  # pragma: no cover
                log.error("engine-event hook failed", extra={"error": str(exc)[:200]})
        return snap

    # ------------------------------------------------------------------ inputs
    def on_pnl_tick(self, unrealized: float) -> FsmSnapshot:
        with self._lock:
            self._unrealized = float(unrealized)
            ev = self.engine.on_pnl_tick(self._unrealized)
            return self._emit(ev)

    def on_clock(self, when: datetime | None = None) -> FsmSnapshot:
        with self._lock:
            ev = self.engine.on_clock(when or now_ist())
            # A restart after the cutoff must lock the day WITHOUT demanding a live
            # square-off: the session already ended, and firing market orders hours
            # later is worse than doing nothing.
            if ev.square_off and self._booted_after_cutoff:
                log.warning("post-cutoff boot: locking without square-off",
                            extra={"note": ev.note})
                ev = EngineEvent(square_off=False, lock=True, note=ev.note)
            return self._emit(ev)

    def on_trade_opened(self) -> FsmSnapshot:
        with self._lock:
            self.engine.on_trade_opened()
            return self._persist(curve=False)

    def on_trade_closed(self, net_pnl: float,
                        remaining_unrealized: float = 0.0) -> FsmSnapshot:
        """`net_pnl` must be NET of costs — the FSM trades on money actually kept.

        `remaining_unrealized` is the mark of positions STILL open. Forcing it to 0
        re-priced dayPnL as realized-only for a moment, which could trip a ratcheted
        floor that was never actually breached and square off surviving legs.
        """
        with self._lock:
            ev = self.engine.on_trade_closed(TradeResult(pnl=float(net_pnl)))
            snap = self._emit(ev)
            self._update_week(snap)
            # Re-apply the surviving mark so dayPnL reflects reality again.
            self._unrealized = float(remaining_unrealized)
            if abs(self._unrealized) > 1e-9:
                snap = self._emit(self.engine.on_pnl_tick(self._unrealized))
            return snap

    def refund_trade_slot(self) -> None:
        """Give back a budget slot when an entry never filled (§8 edge handling)."""
        with self._lock:
            if self.engine.trades_taken > 0:
                self.engine.trades_taken -= 1
                log.info("trade slot refunded",
                         extra={"trades_taken": self.engine.trades_taken})
            self._persist(curve=False)

    def can_enter(self, *, confidence: int | None = None, is_expiry_day: bool = False,
                  proposed_risk: float | None = None, when: datetime | None = None):
        with self._lock:
            return self.engine.can_enter(
                when or now_ist(), confidence=confidence,
                is_expiry_day=is_expiry_day, proposed_risk=proposed_risk,
            )

    def set_kill_switch(self, on: bool) -> None:
        with self._lock:
            self.engine.kill_switch_on = bool(on)

    # ------------------------------------------------------------------ weekly governor
    def _load_week(self) -> WeekState:
        raw = state_get(WEEK_KEY, "")
        wk = week_start()
        if raw:
            try:
                d = json.loads(raw)
                if d.get("week") == wk:
                    return WeekState(week=wk, pnl=float(d.get("pnl", 0.0)),
                                     locked=bool(d.get("locked", False)),
                                     red_days=int(d.get("red_days", 0)),
                                     days={k: float(v) for k, v in (d.get("days") or {}).items()})
            except (json.JSONDecodeError, TypeError, ValueError):
                log.warning("week state unreadable; starting a fresh week")
        return WeekState(week=wk)

    def _save_week(self, w: WeekState) -> None:
        state_set(WEEK_KEY, json.dumps(asdict(w)))

    def _update_week(self, snap: FsmSnapshot) -> WeekState:
        w = self._load_week()
        w.days[self.session_date] = snap.realized
        w.pnl = round(sum(w.days.values()), 2)

        if not w.locked and w.pnl <= -self.week_loss_limit:
            w.locked = True
            log.warning("WEEK LOCKED", extra={"week_pnl": w.pnl,
                                              "limit": -self.week_loss_limit})
            with session() as s:
                s.add(Violation(ts=utcnow(), session_date=self.session_date,
                                kind="WEEK_LOCK",
                                detail=f"week P&L {w.pnl:.0f} breached {-self.week_loss_limit:.0f}"))
        self._save_week(w)
        return w

    @property
    def week_locked(self) -> bool:
        return self._load_week().locked

    def close_session(self) -> dict[str, object]:
        """End-of-day roll: record red/green streak, decide if tomorrow is paper-only."""
        with self._lock:
            snap = self.snapshot()
            red = snap.realized < 0
            try:
                streak = int(state_get(RED_DAYS_KEY, "0"))
            except ValueError:
                streak = 0
            streak = streak + 1 if red else 0
            state_set(RED_DAYS_KEY, str(streak))

            force_paper = streak >= self.red_days_to_paper
            self.forced_paper = force_paper
            # Persist it: build_broker() reads this at next boot, otherwise the rule
            # was pure theatre — announced to the trader, then ignored.
            state_set(FORCE_PAPER_KEY, "1" if force_paper else "")
            if force_paper:
                log.warning("THREE RED DAYS — next session forced to PAPER",
                            extra={"streak": streak})

            w = self._update_week(snap)
            log.info("session closed", extra={
                "date": self.session_date, "realized": snap.realized,
                "state": snap.state, "red_streak": streak, "week_pnl": w.pnl,
            })
            return {"snapshot": snap.as_dict(), "red_streak": streak,
                    "force_paper_next": force_paper, "week": asdict(w)}

    # ------------------------------------------------------------------ day lifecycle
    def reset_day(self) -> FsmSnapshot:
        with self._lock:
            self.engine.reset_day()
            self._unrealized = 0.0
            self.session_date = session_date()
            self._last_state = self.engine.state
            self._last_floor = self.engine.floor
            log.info("day reset", extra={"session_date": self.session_date})
            return self._persist()

    def rearm(self) -> FsmSnapshot:
        """Rebuild today's FSM after a restart. Build plan Phase 2 acceptance.

        Replays today's CLOSED trades in order through the real engine, then applies
        the last known unrealized mark. Replaying rather than restoring raw fields
        means the state machine itself decides the state, so recovery cannot invent
        a combination the engine would never produce.
        """
        with self._lock:
            today = session_date()
            self.session_date = today
            self.engine.reset_day()
            self._unrealized = 0.0

            with session() as s:
                closed = list(
                    s.query(Trade)
                    .filter(Trade.session_date == today, Trade.status == "CLOSED")
                    .order_by(Trade.closed_at.asc(), Trade.opened_at.asc())
                    .all()
                )
                open_trades = list(
                    s.query(Trade)
                    .filter(Trade.session_date == today, Trade.status == "OPEN")
                    .all()
                )
                last_curve = (
                    s.query(PnlCurve)
                    .filter(PnlCurve.session_date == today)
                    .order_by(PnlCurve.ts.desc())
                    .first()
                )
                closed_pnls = [float(t.pnl) for t in closed]
                open_count = len(open_trades)
                last_unrealized = float(last_curve.unrealized) if last_curve else 0.0

            for pnl in closed_pnls:
                self.engine.on_trade_opened()
                self.engine.on_trade_closed(TradeResult(pnl=pnl))

            # Open trades already consumed budget; count them without closing.
            for _ in range(open_count):
                self.engine.on_trade_opened()

            if open_count and last_unrealized:
                self._unrealized = last_unrealized
                self.engine.on_pnl_tick(last_unrealized)

            self._last_state = self.engine.state
            self._last_floor = self.engine.floor
            snap = self._persist(curve=False)
            log.warning("FSM re-armed after restart", extra={
                "date": today, "closed": len(closed_pnls), "open": open_count,
                "state": snap.state, "day_pnl": snap.day_pnl, "floor": snap.floor,
                "trades_taken": snap.trades_taken,
            })
            return snap

    def is_new_session(self) -> bool:
        return session_date() != self.session_date

    @property
    def is_locked(self) -> bool:
        return self.engine.state == DayState.LOCKED

    def floor_distance(self) -> float:
        """Rupees between dayPnL and the floor. Drives FLOOR_WARNING pushes."""
        return round(self.engine.day_pnl - self.engine.floor, 2)

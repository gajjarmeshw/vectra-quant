"""
SENTINEL Risk Engine — deterministic core.

Implements the Daily State Machine (design doc §4.2, user-amended):

  States: NORMAL -> EARNED -> PROTECT -> TRAIL -> LOCKED

  Locks (any one fires -> LOCKED, square-off, no entries until next session):
    - dayPnL <= -loss_limit            (default -1050)
    - 2 consecutive losing trades
    - floor breach in EARNED/PROTECT/TRAIL
    - session hard cutoff (square-off time)

  Floors:
    EARNED  : entered when realized >= 45% of target -> 4th trade unlocked,
              floor = 0.70 * current dayPnL (user's "keep 70" rule),
              ratchets up as dayPnL makes new highs, never down.
    PROTECT : entered at dayPnL >= target, floor = max(prev, 0.5 * target)
    TRAIL   : entered at dayPnL >= 1.3 * target,
              floor = max(prev, 0.75 * peak)  (25% giveback), ratchets.

  NO LLM INPUT TOUCHES THIS MODULE. Config is frozen at construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Optional


class DayState(str, Enum):
    NORMAL = "NORMAL"
    EARNED = "EARNED"
    PROTECT = "PROTECT"
    TRAIL = "TRAIL"
    LOCKED = "LOCKED"


class RejectReason(str, Enum):
    LOCKED = "day is LOCKED"
    TRADE_CAP = "trade cap reached"
    OUTSIDE_WINDOW = "outside entry window"
    EXPIRY_CUTOFF = "past expiry-day entry cutoff"
    RISK_EXCEEDED = "trade risk exceeds per-trade limit"
    CONFIDENCE = "confidence below required gate"
    KILL_SWITCH = "master kill switch is OFF"
    WEEK_LOCKED = "week is locked due to max loss limit"
    EXPOSURE_CAP = "exposure cap exceeded"
    DRAWDOWN_LADDER_HALT = "drawdown ladder halted new entries"


@dataclass(frozen=True)
class RiskConfig:
    capital: float = 15_000
    target: float = 2_500                 # T
    loss_limit: float = 1_050             # lock at dayPnL <= -loss_limit
    risk_per_trade: float = 1_200         # r
    base_trades: int = 3
    bonus_at_pct_target: float = 0.45     # realized >= 45% T -> 4th trade
    consecutive_loss_stop: int = 2
    t1_keep_pct: float = 0.70             # "hits 60 keep 70"
    protect_at: float = 1.00              # of target
    protect_floor_pct: float = 0.50       # lock 50% of T
    trail_at: float = 1.30                # of target
    trail_giveback: float = 0.25          # keep 75% of peak
    risk_scale_protected: float = 0.50    # r/2 in PROTECT/TRAIL
    min_confidence: int = 70
    min_confidence_protected: int = 80
    entry_open: time = time(9, 20)
    entry_close: time = time(15, 0)
    squareoff_at: time = time(15, 10)
    expiry_entry_close: time = time(14, 30)
    expiry_risk_scale: float = 0.50
    max_underlying_alloc_pct: float = 0.10
    max_strategy_alloc_pct: float = 0.30
    drawdown_ladder: bool = True
    default_sl_premium_pts: dict = field(
        default_factory=lambda: {"NIFTY": 12.0, "SENSEX": 35.0}
    )


@dataclass
class TradeResult:
    pnl: float                             # realized rupees, +/-


@dataclass
class Decision:
    allowed: bool
    reason: Optional[RejectReason] = None
    max_risk: float = 0.0                  # effective r for this entry
    min_confidence: int = 70


@dataclass
class EngineEvent:
    """Emitted side-effects the caller must act on."""
    square_off: bool = False
    lock: bool = False
    note: str = ""


class RiskEngine:
    def __init__(self, cfg: RiskConfig, kill_switch_on: bool = True):
        self.cfg = cfg
        self.kill_switch_on = kill_switch_on   # True = system ENABLED
        self.reset_day()

    # ------------------------------------------------ day lifecycle
    def reset_day(self) -> None:
        self.state = DayState.NORMAL
        self.day_pnl: float = 0.0             # realized + unrealized (marked)
        self.realized: float = 0.0
        self.peak: float = 0.0
        self.floor: float = -self.cfg.loss_limit
        self.trades_taken: int = 0
        self.consecutive_losses: int = 0
        self.bonus_unlocked: bool = False
        self.lock_reason: str = ""

    # ------------------------------------------------ properties
    @property
    def trade_cap(self) -> int:
        return self.cfg.base_trades + (1 if self.bonus_unlocked else 0)

    @property
    def protected(self) -> bool:
        return self.state in (DayState.PROTECT, DayState.TRAIL)

    # ------------------------------------------------ entry gate
    def can_enter(
        self,
        now: datetime,
        confidence: Optional[int] = None,     # None for manual trades
        is_expiry_day: bool = False,
        proposed_risk: Optional[float] = None,
        active_trades: list = None,
        week_locked: bool = False,
        proposed_instrument: str = "",
        proposed_strategy: str = "",
    ) -> Decision:
        c = self.cfg
        if not self.kill_switch_on:
            return Decision(False, RejectReason.KILL_SWITCH)
        if self.state == DayState.LOCKED:
            return Decision(False, RejectReason.LOCKED)
        if week_locked:
            return Decision(False, RejectReason.WEEK_LOCKED)
        if self.trades_taken >= self.trade_cap:
            return Decision(False, RejectReason.TRADE_CAP)

        t = now.time()
        if t < c.entry_open or t >= c.entry_close:
            return Decision(False, RejectReason.OUTSIDE_WINDOW)
        if is_expiry_day and t >= c.expiry_entry_close:
            return Decision(False, RejectReason.EXPIRY_CUTOFF)

        risk_scale = 1.0
        if self.protected:
            risk_scale *= c.risk_scale_protected
        if is_expiry_day:
            risk_scale *= c.expiry_risk_scale

        # Drawdown Ladder: 50% budget hit -> halve size, 75% -> no new entries
        if c.drawdown_ladder and self.day_pnl < 0:
            dd_ratio = abs(self.day_pnl) / c.loss_limit if c.loss_limit > 0 else 0
            if dd_ratio >= 0.75:
                return Decision(False, RejectReason.DRAWDOWN_LADDER_HALT)
            elif dd_ratio >= 0.50:
                risk_scale *= 0.5

        # Exposure Caps
        if active_trades:
            underlying_cost = 0.0
            strategy_cost = 0.0
            # Rough estimation of new trade cost (risk * ~4 for a standard 25% SL, but we'll use capital * max_pct)
            # Actually, active_trades contains Trade objects. We sum their entry_price * qty
            for t in active_trades:
                if getattr(t, 'status', 'OPEN') != 'OPEN':
                    continue
                cost = getattr(t, 'entry_price', 0) * getattr(t, 'qty', 0)
                if proposed_instrument and getattr(t, 'instrument', '') == proposed_instrument:
                    underlying_cost += cost
                # strategy name usually in suggestion, we check if t has strategy_name or from suggestion
                # (For now, we check the cap just against currently open cost vs allowed)
                # This prevents adding new trades if already over cap.
            
            if underlying_cost >= c.capital * c.max_underlying_alloc_pct:
                return Decision(False, RejectReason.EXPOSURE_CAP)
            # Full strategy check would require strategy_name on Trade row. We skip strategy cap for now or enforce broadly.

        max_risk = c.risk_per_trade * risk_scale

        min_conf = (
            c.min_confidence_protected if self.protected else c.min_confidence
        )
        if confidence is not None and confidence < min_conf:
            return Decision(False, RejectReason.CONFIDENCE,
                            max_risk=max_risk, min_confidence=min_conf)
        if proposed_risk is not None and proposed_risk > max_risk + 1e-9:
            return Decision(False, RejectReason.RISK_EXCEEDED,
                            max_risk=max_risk, min_confidence=min_conf)

        return Decision(True, max_risk=max_risk, min_confidence=min_conf)

    # ------------------------------------------------ event: trade opened
    def on_trade_opened(self) -> None:
        self.trades_taken += 1

    # ------------------------------------------------ event: trade closed
    def on_trade_closed(self, result: TradeResult) -> EngineEvent:
        self.realized += result.pnl
        if result.pnl < 0:
            self.consecutive_losses += 1
        elif result.pnl > 0:
            self.consecutive_losses = 0
        ev = self._refresh(mark_unrealized=0.0)
        if (self.state != DayState.LOCKED
                and self.consecutive_losses >= self.cfg.consecutive_loss_stop):
            return self._lock(f"{self.cfg.consecutive_loss_stop} consecutive losses")
        return ev

    # ------------------------------------------------ event: P&L tick
    def on_pnl_tick(self, unrealized: float) -> EngineEvent:
        """Call on every tick / few seconds with current open-position P&L."""
        return self._refresh(mark_unrealized=unrealized)

    # ------------------------------------------------ event: clock
    def on_clock(self, now: datetime) -> EngineEvent:
        if (self.state != DayState.LOCKED
                and now.time() >= self.cfg.squareoff_at):
            return self._lock("session square-off time")
        return EngineEvent()

    # ------------------------------------------------ internals
    def _refresh(self, mark_unrealized: float) -> EngineEvent:
        c = self.cfg
        self.day_pnl = self.realized + mark_unrealized
        self.peak = max(self.peak, self.day_pnl)

        if self.state == DayState.LOCKED:
            return EngineEvent()

        # ---- state promotions (one-way ladder)
        if self.state == DayState.NORMAL and self.realized >= c.bonus_at_pct_target * c.target:
            self.state = DayState.EARNED
            self.bonus_unlocked = True
            self.floor = max(self.floor, c.t1_keep_pct * self.day_pnl)
        if self.state in (DayState.NORMAL, DayState.EARNED) and self.day_pnl >= c.protect_at * c.target:
            self.state = DayState.PROTECT
            if not self.bonus_unlocked:          # target can jump past 45% gate
                self.bonus_unlocked = True
            self.floor = max(self.floor, c.protect_floor_pct * c.target)
        if self.state in (DayState.NORMAL, DayState.EARNED, DayState.PROTECT) \
                and self.day_pnl >= c.trail_at * c.target:
            self.state = DayState.TRAIL

        # ---- floor ratchets
        if self.state == DayState.EARNED:
            self.floor = max(self.floor, c.t1_keep_pct * self.day_pnl)
        if self.state == DayState.TRAIL:
            self.floor = max(self.floor, (1 - c.trail_giveback) * self.peak)

        # ---- breach checks
        if self.day_pnl <= -c.loss_limit:
            return self._lock(f"daily loss limit -{c.loss_limit:.0f} hit")
        if self.state != DayState.NORMAL and self.day_pnl <= self.floor:
            return self._lock(
                f"floor {self.floor:.0f} breached in {self.state.value}")
        return EngineEvent()

    def _lock(self, reason: str) -> EngineEvent:
        self.state = DayState.LOCKED
        self.lock_reason = reason
        return EngineEvent(square_off=True, lock=True, note=reason)

    # ------------------------------------------------ guardian helper
    def default_stop_premium(self, instrument: str, entry_premium: float) -> float:
        """SL premium for a manual trade that arrived without a stop."""
        pts = self.cfg.default_sl_premium_pts[instrument.upper()]
        return max(0.05, round(entry_premium - pts, 2))


# ---------------------------------------------------- position sizing
def size_position(
    max_risk: float,
    sl_points_premium: float,
    lot_size: int,
    premium: float,
    capital: float,
) -> dict:
    """
    User rule: no premium cap under current capital; always allow >= 1 lot.
    Risk-based lots where possible, else 1 lot flagged 'over_risk'.
    """
    risk_per_lot = sl_points_premium * lot_size
    lots = int(max_risk // risk_per_lot) if risk_per_lot > 0 else 1
    over_risk = lots == 0
    lots = max(1, lots)
    cost = premium * lot_size * lots
    affordable = cost <= capital
    return {
        "lots": lots if affordable else max(1, int(capital // (premium * lot_size))),
        "risk": risk_per_lot * lots,
        "cost": cost,
        "over_risk": over_risk,          # 1-lot risk exceeds max_risk (user accepted)
        "affordable": affordable,
    }


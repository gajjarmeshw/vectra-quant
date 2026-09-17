"""Trade structure for the cross-sectional breadth signal: a single-leg
ATM option buy in the signal direction (CE on bullish breadth, PE on
bearish), exited on a stop/target premium move or a max hold time.

This is a deliberate design choice, not a disclosed structure -- breadth
is our own signal with no prior spec to match. Kept as simple as possible
(one leg, defined stop/target, short max hold) because a brand-new,
uncalibrated signal should carry the least structural risk while it's
being evaluated, not the most.
"""
from __future__ import annotations

from dataclasses import dataclass

from vectra_quant.orderflow.breadth_signal import SignalDirection
from vectra_quant.orderflow.config import BreadthCfg


@dataclass(frozen=True)
class BreadthLeg:
    action: str   # always "BUY"
    right: str    # "CE" or "PE"
    strike: float
    lots: int


def build_breadth_structure(direction: SignalDirection, spot: float, cfg: BreadthCfg) -> list[BreadthLeg]:
    if direction == SignalDirection.SKIP:
        return []
    atm = round(spot / cfg.strike_step) * cfg.strike_step
    right = "CE" if direction == SignalDirection.BULLISH else "PE"
    return [BreadthLeg(action="BUY", right=right, strike=atm, lots=cfg.lots)]


def exit_reason(entry_premium: float, current_premium: float, entry_ts_minutes_ago: float, cfg: BreadthCfg) -> str | None:
    """Returns a reason string once an exit condition is met, else None.
    Premium-based exits take priority over the time-based one."""
    if entry_premium <= 0:
        return None
    change = (current_premium - entry_premium) / entry_premium
    if change <= -cfg.stop_loss_pct_premium:
        return "STOP_LOSS"
    if change >= cfg.target_pct_premium:
        return "TARGET"
    if entry_ts_minutes_ago >= cfg.max_hold_minutes:
        return "MAX_HOLD_TIME"
    return None

"""hedged121 ("Nifty Thunderbolt") structure builder and calendar rules.

1x2 ratio backspread: BUY 2 lots 100pts out (placed first, fill-gated),
then SELL 1 lot at the money -- net long gamma. Matches disclosure
section 8 exactly for the structure; the calendar vetoes (Tuesday skip,
high-VIX skip) match section 5.2 / 14.
"""
from __future__ import annotations

from dataclasses import dataclass

from vectra_quant.orderflow.config import ThunderboltCfg
from vectra_quant.orderflow.thunderbolt_signal import SignalDirection

TUESDAY = 1  # datetime.weekday(): Monday=0 ... Sunday=6


@dataclass(frozen=True)
class ThunderboltLeg:
    action: str    # "BUY" or "SELL"
    right: str     # "CE" or "PE"
    strike: float
    lots: int
    order: int     # 1 = placed first (the two longs), 2 = placed second (the short)


def should_skip_day(weekday: int, prior_session_vix: float | None, cfg: ThunderboltCfg) -> tuple[bool, str]:
    """Returns (skip?, reason). Weekends aren't modeled here -- the caller
    only invokes this on real trading-session dates."""
    if cfg.skip_tuesday and weekday == TUESDAY:
        return True, "TUESDAY_SKIP"
    if prior_session_vix is not None and prior_session_vix >= cfg.vix_skip_at_or_above:
        return True, f"HIGH_VIX_SKIP (prior_vix={prior_session_vix})"
    return False, ""


def build_thunderbolt_structure(direction: SignalDirection, spot: float, cfg: ThunderboltCfg) -> list[ThunderboltLeg]:
    if direction == SignalDirection.SKIP:
        return []

    atm = round(spot / cfg.strike_step) * cfg.strike_step
    if direction == SignalDirection.BULLISH:
        return [
            ThunderboltLeg(action="BUY", right="CE", strike=atm + cfg.long_offset_pts, lots=cfg.long_lots, order=1),
            ThunderboltLeg(action="SELL", right="CE", strike=atm, lots=cfg.short_lots, order=2),
        ]
    return [
        ThunderboltLeg(action="BUY", right="PE", strike=atm - cfg.long_offset_pts, lots=cfg.long_lots, order=1),
        ThunderboltLeg(action="SELL", right="PE", strike=atm, lots=cfg.short_lots, order=2),
    ]


def net_debit(long_premium: float, short_premium: float, cfg: ThunderboltCfg, lot_size: int) -> float:
    """d = 2 x long premium - short premium, per unit lot_size (can be
    negative -- "on some days the structure is near-zero-cost or a small
    credit", per the disclosure)."""
    return (cfg.long_lots * long_premium - cfg.short_lots * short_premium) * lot_size


def max_loss(net_debit_value: float, cfg: ThunderboltCfg, lot_size: int) -> float:
    """(spread width x lot size) + net debit, per the disclosure's own
    recorded-max-loss definition -- the payoff's valley at the long strike.
    `net_debit_value` is already scaled by lot_size (see `net_debit()`), and
    can be negative on a net-credit day, which correctly reduces this below
    the raw spread-width cap."""
    return (cfg.long_offset_pts * lot_size) + net_debit_value

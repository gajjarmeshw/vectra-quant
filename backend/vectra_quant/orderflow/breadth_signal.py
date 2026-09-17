"""Cross-sectional order-flow breadth: aggregate per-stock top-of-book
imbalance across many NIFTY constituents into one market-wide reading, used
as a directional proxy for the index.

A genuinely different signal family from Thunderbolt (hedged121), not a
reuse of it: Thunderbolt reads ONE instrument's own order book. Breadth
reads how many, and how strongly, MANY stocks lean the same way at once --
the idea being that broad-based buying/selling pressure across constituents
shows up before it's fully reflected in the index itself.

Unlike Thunderbolt, this is genuinely backtestable: real historical
multi-stock depth data exists for one session (shared by Arjun,
2026-09-17) -- see scripts/backtest_breadth_arjun_data.py. One session is
still far too little to claim any edge; this only tests whether the signal
shows structure worth pursuing with more data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from vectra_quant.orderflow.charts import interval_imbalance
from vectra_quant.orderflow.thunderbolt_signal import SignalDirection, TriggerEvent, detect_crossing


@dataclass(frozen=True)
class BreadthReading:
    mean_imbalance: float   # continuous: average of per-stock (bid-ask)/(bid+ask), level-0 qty
    pct_bullish: float      # fraction of stocks at/above bullish_threshold
    pct_bearish: float      # fraction of stocks at/below bearish_threshold
    n_stocks: int


def per_stock_imbalance(bid_top_qty: float, ask_top_qty: float) -> float:
    """One stock's top-of-book imbalance at one instant. Top-of-book (not
    deeper resting size) because that's what's actually walkable in the
    next few seconds, which is what a breadth read is meant to anticipate."""
    return interval_imbalance(bid_top_qty, ask_top_qty)


def compute_breadth(
    per_stock_imbalances: dict[str, float],
    bullish_threshold: float = 0.1,
    bearish_threshold: float = -0.1,
) -> BreadthReading:
    """Aggregate one snapshot's per-stock imbalance readings into a single
    market-wide breadth reading."""
    values = list(per_stock_imbalances.values())
    n = len(values)
    if n == 0:
        return BreadthReading(mean_imbalance=0.0, pct_bullish=0.0, pct_bearish=0.0, n_stocks=0)
    mean_imb = sum(values) / n
    bullish = sum(1 for v in values if v >= bullish_threshold) / n
    bearish = sum(1 for v in values if v <= bearish_threshold) / n
    return BreadthReading(mean_imbalance=mean_imb, pct_bullish=bullish, pct_bearish=bearish, n_stocks=n)


def detect_breadth_crossing(
    series: list[tuple[datetime, float]], theta_cross: float, after: time = time(0, 0),
) -> TriggerEvent | None:
    """First transition of the mean-breadth series through +-theta_cross.
    Thin wrapper: the crossing/reversal logic is instrument-agnostic, so
    this reuses Thunderbolt's own `detect_crossing` rather than
    duplicating it -- the two signal families differ in what series they
    feed in and why, not in how a threshold crossing is detected."""
    return detect_crossing(series, theta_cross, after)


__all__ = [
    "BreadthReading",
    "SignalDirection",
    "compute_breadth",
    "detect_breadth_crossing",
    "per_stock_imbalance",
]

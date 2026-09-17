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

import math
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
    stderr: float           # cross-sectional standard error of the mean
    z_score: float          # mean / stderr -- the coverage-invariant reading


# Below this many stocks a z-score is not meaningful either, so the reading is
# reported as 0 rather than as a confident-looking large number off two names.
_MIN_N_FOR_Z = 5

# Floor on the cross-sectional sigma before it becomes a standard error.
# Without it, unusually tight agreement across stocks divides by ~0 and
# manufactures an enormous z from a tiny mean -- and perfect agreement (every
# stock at the same imbalance) divides by exactly 0. Perfect agreement is the
# strongest reading there is, not a null one, so the floor has to keep it
# large rather than send it to zero. 0.15 is well below the dispersion
# actually observed across NIFTY100 top-of-book imbalances, so it only binds
# in the degenerate case it exists for.
_MIN_SIGMA = 0.15


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
    market-wide breadth reading.

    `z_score`, not `mean_imbalance`, is what the trigger should threshold.
    Averaging n roughly-independent series shrinks the spread by ~sqrt(n), so a
    fixed cut-off on the raw mean is a completely different event at different
    coverage. Measured on the one real 100-stock capture we have, a fixed
    |mean| >= 0.10 fires in 16.5% of buckets at 100 stocks and 51.0% at 20 -- a
    3x swing driven purely by how many stocks happened to be quoting. Coverage
    is thinnest early in the session and the strategy takes the *first*
    crossing of the day, so that bias pointed straight at entering early on a
    thin, noisy sample. Dividing by the standard error removes it.
    """
    values = list(per_stock_imbalances.values())
    n = len(values)
    if n == 0:
        return BreadthReading(
            mean_imbalance=0.0, pct_bullish=0.0, pct_bearish=0.0, n_stocks=0,
            stderr=0.0, z_score=0.0,
        )
    mean_imb = sum(values) / n
    bullish = sum(1 for v in values if v >= bullish_threshold) / n
    bearish = sum(1 for v in values if v <= bearish_threshold) / n

    if n < _MIN_N_FOR_Z:
        stderr = 0.0
        z = 0.0
    else:
        var = sum((v - mean_imb) ** 2 for v in values) / (n - 1)
        sigma = max(math.sqrt(var), _MIN_SIGMA)
        stderr = sigma / math.sqrt(n)
        z = mean_imb / stderr

    return BreadthReading(
        mean_imbalance=mean_imb, pct_bullish=bullish, pct_bearish=bearish, n_stocks=n,
        stderr=stderr, z_score=z,
    )


def detect_breadth_crossing(
    series: list[tuple[datetime, float]], theta_cross: float, after: time = time(0, 0),
) -> TriggerEvent | None:
    """First transition of the breadth z-score series through +-theta_cross.
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

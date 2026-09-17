"""Chart 1 / Chart 2 order-flow signal math -- pure functions, no I/O.

Everything here operates on already-recorded (timestamp, value) series, so
it is fully unit-testable without a live connection. What it can NOT be is
backtested against history: the inputs (big-order records, per-interval
imbalance) only exist once the recorder has been running, because Dhan
does not serve historical depth.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Direction(Enum):
    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"


class Decision(Enum):
    WITH_FLOW = "WITH_FLOW"
    AGAINST_FLOW = "AGAINST_FLOW"
    SKIP = "SKIP"


class FinalBias(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SKIP = "SKIP"


# --------------------------------------------------------------- "big" order filter

def rolling_percentile_threshold(historical_sizes: list[float], percentile: float, min_samples: int) -> float | None:
    """The qty threshold above which a depth level counts as "big".

    Returns None until `min_samples` historical level sizes have been
    collected for this instrument -- the spec requires a minimum sample
    before qualifying anything, so an early, noisy threshold never fires.
    `percentile` is 0-100 (e.g. 95 for "top 5%").
    """
    if len(historical_sizes) < min_samples:
        return None
    sorted_sizes = sorted(historical_sizes)
    idx = min(len(sorted_sizes) - 1, int(round((percentile / 100.0) * (len(sorted_sizes) - 1))))
    return sorted_sizes[idx]


def is_big_order(qty: float, threshold: float | None) -> bool:
    return threshold is not None and qty > threshold


# --------------------------------------------------------------- Chart 1

@dataclass(frozen=True)
class BigOrderRecord:
    timestamp: datetime
    side: str  # "BID" or "ASK"
    qty: float


def compute_chart1(records: list[BigOrderRecord]) -> list[tuple[datetime, float]]:
    """cumulativeNet(t) = cumulativeBid(t) - cumulativeAsk(t), reset daily.

    Records sharing an identical timestamp are merged (their bid/ask
    quantities summed) before accumulating, per the spec.
    """
    by_ts: dict[datetime, dict[str, float]] = {}
    for r in records:
        slot = by_ts.setdefault(r.timestamp, {"BID": 0.0, "ASK": 0.0})
        slot[r.side] += r.qty

    cum_bid = 0.0
    cum_ask = 0.0
    series: list[tuple[datetime, float]] = []
    for ts in sorted(by_ts):
        cum_bid += by_ts[ts]["BID"]
        cum_ask += by_ts[ts]["ASK"]
        series.append((ts, cum_bid - cum_ask))
    return series


# --------------------------------------------------------------- Chart 2

def interval_imbalance(bid_qty: float, ask_qty: float) -> float:
    denom = bid_qty + ask_qty
    if denom == 0:
        return 0.0
    return (bid_qty - ask_qty) / denom


def compute_chart2(interval_imbalances: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Chart2(t) = running sum of ir(i) over all intervals up to t."""
    running = 0.0
    series: list[tuple[datetime, float]] = []
    for ts, ir in sorted(interval_imbalances, key=lambda x: x[0]):
        running += ir
        series.append((ts, running))
    return series


# --------------------------------------------------------------- reading a chart

def value_read(series: list[tuple[datetime, float]], cutoff: datetime) -> float | None:
    """Last value at or before `cutoff`, or None if the series has no such point."""
    timestamps = [t for t, _ in series]
    idx = bisect_right(timestamps, cutoff) - 1
    if idx < 0:
        return None
    return series[idx][1]


def signed_area_read(series: list[tuple[datetime, float]], cutoff: datetime) -> float:
    """Trapezoidal signed integral from the series' first point to `cutoff`.

    The final partial segment is linearly interpolated onto the cutoff when
    the next recorded point lies beyond it, per the spec.
    """
    pts = sorted(series, key=lambda x: x[0])
    pts = [(t, v) for t, v in pts if t <= cutoff] + [p for p in pts if p[0] > cutoff][:1]
    if len(pts) < 2:
        return 0.0

    area = 0.0
    for (t_a, y_a), (t_b, y_b) in zip(pts, pts[1:]):
        if t_b <= cutoff:
            dt = (t_b - t_a).total_seconds()
            area += 0.5 * (y_a + y_b) * dt
        else:
            # linear interpolation of the final partial segment onto cutoff
            span = (t_b - t_a).total_seconds()
            if span <= 0:
                break
            frac = (cutoff - t_a).total_seconds() / span
            y_end = y_a + (y_b - y_a) * frac
            dt = (cutoff - t_a).total_seconds()
            area += 0.5 * (y_a + y_end) * dt
            break
    return area


def sign_to_direction(x: float, eps: float) -> Direction:
    if abs(x) < eps:
        return Direction.NEUTRAL
    return Direction.BUY if x > 0 else Direction.SELL


# --------------------------------------------------------------- resolution + VIX filter

DEFAULT_RESOLUTION_TABLE: dict[tuple[Direction, Direction], Decision] = {
    (Direction.BUY, Direction.BUY): Decision.WITH_FLOW,
    (Direction.SELL, Direction.SELL): Decision.WITH_FLOW,
}


def resolve_direction(
    chart1_dir: Direction, chart2_dir: Direction,
    table: dict[tuple[Direction, Direction], Decision] | None = None,
) -> Decision:
    """Default: charts agree -> WITH_FLOW; anything else (disagree or either
    neutral) -> SKIP. The full table is meant to be calibrated -- pass a
    different `table` to A/B test resolution rules without touching this
    function."""
    t = table if table is not None else DEFAULT_RESOLUTION_TABLE
    return t.get((chart1_dir, chart2_dir), Decision.SKIP)


def decision_to_bullish_bearish(decision: Decision, chart1_dir: Direction, chart2_dir: Direction) -> FinalBias:
    if decision == Decision.SKIP:
        return FinalBias.SKIP
    agreed_dir = chart1_dir if chart1_dir == chart2_dir else None
    if decision == Decision.WITH_FLOW and agreed_dir is not None:
        return FinalBias.BULLISH if agreed_dir == Direction.BUY else FinalBias.BEARISH
    if decision == Decision.AGAINST_FLOW and agreed_dir is not None:
        return FinalBias.BEARISH if agreed_dir == Direction.BUY else FinalBias.BULLISH
    return FinalBias.SKIP


def apply_low_vix_contrarian_filter(
    base: FinalBias, vix: float | None, theta_vix: float, is_weak_read: bool, enabled: bool = True,
) -> FinalBias:
    """Flip BULLISH -> BEARISH only when ALL hold: base is BULLISH, VIX is
    known and below theta_vix, and the read is weak. BEARISH is never
    flipped. Missing VIX skips the filter entirely (never blocks a trade)."""
    if not enabled or base != FinalBias.BULLISH or vix is None:
        return base
    if vix < theta_vix and is_weak_read:
        return FinalBias.BEARISH
    return base

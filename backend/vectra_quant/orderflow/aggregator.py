"""Turns a stream of raw depth updates into Chart 1 / Chart 2 inputs.

Deliberately pure/synchronous and I/O-free so it's fully unit-testable —
`recorder.py` is the thin async wrapper that feeds real WebSocket updates
into one of these per session.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from vectra_quant.orderflow.charts import (
    BigOrderRecord,
    compute_chart1,
    compute_chart2,
    interval_imbalance,
    is_big_order,
    rolling_percentile_threshold,
)


@dataclass
class SessionAggregator:
    percentile: float
    min_samples: int
    interval_seconds: int
    historical_sizes_maxlen: int = 20_000

    _historical_sizes: deque[float] = field(default_factory=lambda: deque(maxlen=20_000), init=False)
    _big_records: list[BigOrderRecord] = field(default_factory=list, init=False)
    _interval_start: datetime | None = field(default=None, init=False)
    _interval_bid: float = field(default=0.0, init=False)
    _interval_ask: float = field(default=0.0, init=False)
    _completed_intervals: list[tuple[datetime, float]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._historical_sizes = deque(maxlen=self.historical_sizes_maxlen)

    def _threshold(self) -> float | None:
        return rolling_percentile_threshold(list(self._historical_sizes), self.percentile, self.min_samples)

    def observe(self, ts: datetime, side: str, levels: list[tuple[float, float, int]]) -> None:
        """`levels` is a list of (price, qty, orders) for one bid or ask update."""
        threshold = self._threshold()
        total_qty = 0.0
        for _price, qty, _orders in levels:
            self._historical_sizes.append(qty)
            total_qty += qty
            if is_big_order(qty, threshold):
                self._big_records.append(BigOrderRecord(timestamp=ts, side=side, qty=qty))

        self._roll_interval_if_needed(ts)
        if side == "BID":
            self._interval_bid += total_qty
        else:
            self._interval_ask += total_qty
        if self._interval_start is None:
            self._interval_start = ts

    def _roll_interval_if_needed(self, ts: datetime) -> None:
        if self._interval_start is None:
            return
        elapsed = (ts - self._interval_start).total_seconds()
        while elapsed >= self.interval_seconds:
            interval_end = self._interval_start + timedelta(seconds=self.interval_seconds)
            ir = interval_imbalance(self._interval_bid, self._interval_ask)
            self._completed_intervals.append((interval_end, ir))
            self._interval_start = interval_end
            self._interval_bid = 0.0
            self._interval_ask = 0.0
            elapsed = (ts - self._interval_start).total_seconds()

    def reset_daily(self) -> None:
        """Chart 1's big-order store resets at midnight per the spec; call
        this at the start of each session. Historical sizes (for the
        percentile filter) are NOT reset -- the filter needs its own
        accumulated sample across sessions to stay meaningful."""
        self._big_records.clear()
        self._completed_intervals.clear()
        self._interval_start = None
        self._interval_bid = 0.0
        self._interval_ask = 0.0

    def chart1_series(self) -> list[tuple[datetime, float]]:
        return compute_chart1(self._big_records)

    def raw_imbalance_series(self) -> list[tuple[datetime, float]]:
        """The un-summed per-interval ir(i) readings -- Thunderbolt's signal
        input (the instantaneous series itself), as opposed to Chart2's
        running cumulative sum of the same intervals."""
        pending = list(self._completed_intervals)
        if self._interval_start is not None and (self._interval_bid or self._interval_ask):
            pending.append((self._interval_start, interval_imbalance(self._interval_bid, self._interval_ask)))
        return pending

    def chart2_series(self) -> list[tuple[datetime, float]]:
        # include the in-progress (not-yet-rolled) interval so a same-minute
        # decision isn't blind to the current partial interval
        pending = list(self._completed_intervals)
        if self._interval_start is not None and (self._interval_bid or self._interval_ask):
            pending.append((self._interval_start, interval_imbalance(self._interval_bid, self._interval_ask)))
        return compute_chart2(pending)

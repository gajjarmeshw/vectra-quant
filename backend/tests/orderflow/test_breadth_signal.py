from __future__ import annotations

from datetime import datetime, time

import pytest

from vectra_quant.orderflow.breadth_signal import (
    SignalDirection,
    compute_breadth,
    detect_breadth_crossing,
    per_stock_imbalance,
)


def t(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 17, h, m, s)


def test_per_stock_imbalance_matches_interval_imbalance_formula():
    assert per_stock_imbalance(bid_top_qty=300, ask_top_qty=100) == 0.5
    assert per_stock_imbalance(bid_top_qty=100, ask_top_qty=300) == -0.5
    assert per_stock_imbalance(bid_top_qty=0, ask_top_qty=0) == 0.0


def test_compute_breadth_empty_input():
    r = compute_breadth({})
    assert r.n_stocks == 0
    assert r.mean_imbalance == 0.0


def test_compute_breadth_aggregates_across_stocks():
    imbalances = {"A": 0.8, "B": 0.6, "C": -0.9, "D": 0.05}
    r = compute_breadth(imbalances, bullish_threshold=0.5, bearish_threshold=-0.5)
    assert r.n_stocks == 4
    assert r.mean_imbalance == pytest.approx((0.8 + 0.6 - 0.9 + 0.05) / 4)
    assert r.pct_bullish == 0.5   # A, B
    assert r.pct_bearish == 0.25  # C


def test_detect_breadth_crossing_fires_on_threshold_transition():
    series = [
        (t(9, 30), 0.02),
        (t(9, 31), 0.05),
        (t(9, 32), 0.12),  # crosses +0.10
    ]
    trigger = detect_breadth_crossing(series, theta_cross=0.10, after=time(9, 0))
    assert trigger is not None
    assert trigger.direction == SignalDirection.BULLISH
    assert trigger.ts == t(9, 32)


def test_detect_breadth_crossing_none_when_no_transition():
    series = [(t(9, 30), 0.02), (t(9, 31), 0.03), (t(9, 32), 0.04)]
    assert detect_breadth_crossing(series, theta_cross=0.10) is None

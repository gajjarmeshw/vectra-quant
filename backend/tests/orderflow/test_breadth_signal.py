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


# --------------------------------------------------------------- z-score
#
# The trigger used to threshold the raw cross-sectional mean. Averaging n
# roughly-independent series shrinks the spread by ~sqrt(n), so a fixed cut-off
# meant something completely different at different coverage: measured on the
# one real 100-stock capture, |mean| >= 0.10 fired in 16.5% of buckets at 100
# stocks and 51.0% at 20. Coverage is thinnest at the open and the strategy
# takes the first crossing of the day, so it was biased toward entering early
# on a thin sample.

import random

from vectra_quant.orderflow.breadth_signal import _MIN_N_FOR_Z, _MIN_SIGMA


def test_z_score_is_far_less_coverage_sensitive_than_the_raw_mean():
    """Pure noise, no real lean. The raw mean crosses a fixed threshold much
    more often with few stocks; the z-score should not."""
    rng = random.Random(7)

    def fire_rates(n, buckets=3000):
        raw = z = 0
        for _ in range(buckets):
            vals = {f"s{i}": rng.gauss(0.0, 0.5) for i in range(n)}
            r = compute_breadth(vals)
            raw += abs(r.mean_imbalance) >= 0.10
            z += abs(r.z_score) >= 2.0
        return raw / buckets, z / buckets

    raw_20, z_20 = fire_rates(20)
    raw_100, z_100 = fire_rates(100)

    # The old behaviour: a big coverage-driven swing on identical noise.
    assert raw_20 > raw_100 * 2

    # The new one: close to flat, and near the ~5% a 2-sigma cut implies.
    assert z_20 < raw_20 / 3
    assert abs(z_20 - z_100) < 0.04
    assert z_100 < 0.12


def test_perfect_agreement_is_the_strongest_reading_not_a_null_one():
    """Every stock leaning identically has zero cross-sectional variance.
    Dividing by it naively yields 0 or inf; neither is right -- unanimity is
    the most conviction a breadth reading can carry."""
    r = compute_breadth({f"s{i}": 0.8 for i in range(25)})
    assert r.z_score > 10
    assert r.stderr > 0

    flat = compute_breadth({f"s{i}": 0.0 for i in range(25)})
    assert flat.z_score == 0.0


def test_sigma_floor_binds_only_when_dispersion_is_degenerate():
    tight = compute_breadth({f"s{i}": 0.5 + (0.001 if i % 2 else -0.001) for i in range(36)})
    assert tight.stderr == pytest.approx(_MIN_SIGMA / 6.0, rel=1e-6)   # sqrt(36) = 6

    spread = compute_breadth({f"s{i}": (1.0 if i % 2 else -1.0) for i in range(36)})
    assert spread.stderr > _MIN_SIGMA / 6.0


def test_too_few_stocks_reports_zero_rather_than_false_confidence():
    for n in range(1, _MIN_N_FOR_Z):
        r = compute_breadth({f"s{i}": 0.9 for i in range(n)})
        assert r.z_score == 0.0
        assert r.n_stocks == n


def test_empty_snapshot_is_inert():
    r = compute_breadth({})
    assert (r.mean_imbalance, r.z_score, r.stderr, r.n_stocks) == (0.0, 0.0, 0.0, 0)

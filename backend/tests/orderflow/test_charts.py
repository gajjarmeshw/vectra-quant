"""Unit tests for the pure Chart1/Chart2 signal math (charts.py).

These validate the math is implemented as specified; they say nothing
about whether the underlying signal has any real predictive edge, which
can only be judged from live-recorded data (see orderflow/__init__.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from vectra_quant.orderflow.charts import (
    BigOrderRecord,
    Decision,
    Direction,
    FinalBias,
    apply_low_vix_contrarian_filter,
    compute_chart1,
    compute_chart2,
    decision_to_bullish_bearish,
    interval_imbalance,
    is_big_order,
    resolve_direction,
    rolling_percentile_threshold,
    sign_to_direction,
    signed_area_read,
    value_read,
)

T0 = datetime(2026, 9, 16, 9, 15, 0)


def t(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


# --------------------------------------------------------------- big-order filter

def test_percentile_threshold_none_before_min_samples():
    assert rolling_percentile_threshold([10, 20, 30], percentile=95, min_samples=10) is None


def test_percentile_threshold_available_after_min_samples():
    sizes = list(range(1, 101))  # 1..100
    thresh = rolling_percentile_threshold(sizes, percentile=95, min_samples=50)
    assert thresh is not None
    assert 90 <= thresh <= 100


def test_is_big_order_respects_threshold_and_none():
    assert is_big_order(150, threshold=100) is True
    assert is_big_order(50, threshold=100) is False
    assert is_big_order(50, threshold=None) is False  # not qualified yet -> never "big"


# --------------------------------------------------------------- Chart 1

def test_chart1_merges_same_timestamp_and_accumulates():
    records = [
        BigOrderRecord(t(0), "BID", 100),
        BigOrderRecord(t(0), "ASK", 40),   # same instant as above -> merged
        BigOrderRecord(t(1), "BID", 20),
    ]
    series = compute_chart1(records)
    assert series[0] == (t(0), 60.0)         # cumBid=100, cumAsk=40 -> net 60
    assert series[1] == (t(1), 80.0)         # cumBid=120, cumAsk=40 -> net 80


# --------------------------------------------------------------- Chart 2

def test_interval_imbalance_zero_denominator():
    assert interval_imbalance(0, 0) == 0.0


def test_interval_imbalance_sign_and_bounds():
    assert interval_imbalance(100, 0) == pytest.approx(1.0)
    assert interval_imbalance(0, 100) == pytest.approx(-1.0)
    assert interval_imbalance(60, 40) == pytest.approx(0.2)


def test_chart2_running_sum():
    imbalances = [(t(0), 0.5), (t(1), -0.2), (t(2), 0.3)]
    series = compute_chart2(imbalances)
    assert [v for _, v in series] == pytest.approx([0.5, 0.3, 0.6])


# --------------------------------------------------------------- reads

def test_value_read_last_at_or_before_cutoff():
    series = [(t(0), 1.0), (t(10), 2.0), (t(20), 3.0)]
    assert value_read(series, t(15)) == 2.0
    assert value_read(series, t(20)) == 3.0
    assert value_read(series, t(-5)) is None


def test_signed_area_read_simple_trapezoid_no_interpolation():
    # Two flat points 10s apart at value 2.0 -> area = 2.0 * 10 = 20
    series = [(t(0), 2.0), (t(10), 2.0)]
    assert signed_area_read(series, t(10)) == pytest.approx(20.0)


def test_signed_area_read_interpolates_final_partial_segment():
    # Points at t=0 (y=0) and t=10 (y=10); cutoff at t=5 should interpolate
    # y_end=5, area = 0.5*(0+5)*5 = 12.5
    series = [(t(0), 0.0), (t(10), 10.0)]
    assert signed_area_read(series, t(5)) == pytest.approx(12.5)


def test_signed_area_read_empty_or_single_point_is_zero():
    assert signed_area_read([], t(0)) == 0.0
    assert signed_area_read([(t(0), 5.0)], t(10)) == 0.0


def test_sign_to_direction_eps_band():
    assert sign_to_direction(0.001, eps=0.01) == Direction.NEUTRAL
    assert sign_to_direction(1.0, eps=0.01) == Direction.BUY
    assert sign_to_direction(-1.0, eps=0.01) == Direction.SELL


# --------------------------------------------------------------- resolution + VIX filter

def test_resolve_direction_default_table_agree_is_with_flow():
    assert resolve_direction(Direction.BUY, Direction.BUY) == Decision.WITH_FLOW
    assert resolve_direction(Direction.SELL, Direction.SELL) == Decision.WITH_FLOW


def test_resolve_direction_default_table_disagree_or_neutral_is_skip():
    assert resolve_direction(Direction.BUY, Direction.SELL) == Decision.SKIP
    assert resolve_direction(Direction.BUY, Direction.NEUTRAL) == Decision.SKIP
    assert resolve_direction(Direction.NEUTRAL, Direction.NEUTRAL) == Decision.SKIP


def test_decision_to_bullish_bearish_with_flow():
    assert decision_to_bullish_bearish(Decision.WITH_FLOW, Direction.BUY, Direction.BUY) == FinalBias.BULLISH
    assert decision_to_bullish_bearish(Decision.WITH_FLOW, Direction.SELL, Direction.SELL) == FinalBias.BEARISH
    assert decision_to_bullish_bearish(Decision.SKIP, Direction.BUY, Direction.BUY) == FinalBias.SKIP


def test_vix_filter_flips_weak_bullish_under_threshold():
    result = apply_low_vix_contrarian_filter(FinalBias.BULLISH, vix=12.0, theta_vix=15.0, is_weak_read=True)
    assert result == FinalBias.BEARISH


def test_vix_filter_never_flips_bearish():
    result = apply_low_vix_contrarian_filter(FinalBias.BEARISH, vix=5.0, theta_vix=15.0, is_weak_read=True)
    assert result == FinalBias.BEARISH


def test_vix_filter_skipped_when_vix_missing():
    result = apply_low_vix_contrarian_filter(FinalBias.BULLISH, vix=None, theta_vix=15.0, is_weak_read=True)
    assert result == FinalBias.BULLISH


def test_vix_filter_does_not_flip_strong_bullish_read():
    result = apply_low_vix_contrarian_filter(FinalBias.BULLISH, vix=10.0, theta_vix=15.0, is_weak_read=False)
    assert result == FinalBias.BULLISH


def test_vix_filter_does_not_flip_above_threshold():
    result = apply_low_vix_contrarian_filter(FinalBias.BULLISH, vix=20.0, theta_vix=15.0, is_weak_read=True)
    assert result == FinalBias.BULLISH


def test_vix_filter_disabled_toggle():
    result = apply_low_vix_contrarian_filter(FinalBias.BULLISH, vix=5.0, theta_vix=15.0, is_weak_read=True, enabled=False)
    assert result == FinalBias.BULLISH

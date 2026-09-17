from __future__ import annotations

from datetime import datetime, timedelta

from vectra_quant.orderflow.aggregator import SessionAggregator

T0 = datetime(2026, 9, 16, 9, 15, 0)


def test_below_min_samples_never_flags_big_orders():
    agg = SessionAggregator(percentile=95, min_samples=1000, interval_seconds=60)
    agg.observe(T0, "BID", [(100.0, 99999, 1)])  # huge qty, but filter not warmed up yet
    assert agg.chart1_series() == []


def test_big_order_recorded_once_warmed_up():
    agg = SessionAggregator(percentile=50, min_samples=3, interval_seconds=60)
    # warm up with small sizes
    agg.observe(T0, "BID", [(100.0, 10, 1)])
    agg.observe(T0, "BID", [(100.0, 20, 1)])
    agg.observe(T0, "BID", [(100.0, 30, 1)])
    # now a clearly larger order should register on Chart 1
    agg.observe(T0 + timedelta(seconds=1), "BID", [(100.0, 500, 1)])
    series = agg.chart1_series()
    assert len(series) >= 1
    assert series[-1][1] > 0  # net bid pressure


def test_chart2_rolls_intervals_and_accumulates():
    agg = SessionAggregator(percentile=95, min_samples=1000, interval_seconds=60)
    agg.observe(T0, "BID", [(100.0, 60, 1)])
    agg.observe(T0, "ASK", [(101.0, 40, 1)])
    # advance past the 60s interval boundary
    agg.observe(T0 + timedelta(seconds=61), "BID", [(100.0, 10, 1)])
    agg.observe(T0 + timedelta(seconds=61), "ASK", [(101.0, 90, 1)])

    series = agg.chart2_series()
    assert len(series) >= 1
    first_ir = (60 - 40) / (60 + 40)
    assert series[0][1] == first_ir


def test_reset_daily_clears_charts_but_keeps_percentile_history():
    agg = SessionAggregator(percentile=50, min_samples=2, interval_seconds=60)
    agg.observe(T0, "BID", [(100.0, 10, 1)])
    agg.observe(T0, "BID", [(100.0, 20, 1)])
    agg.observe(T0, "BID", [(100.0, 500, 1)])  # big, once warmed up (2 prior samples)
    assert agg.chart1_series() != []

    agg.reset_daily()
    assert agg.chart1_series() == []
    assert agg.chart2_series() == []
    # percentile history survives -> a new big order still registers immediately
    agg.observe(T0 + timedelta(days=1), "BID", [(100.0, 500, 1)])
    assert agg.chart1_series() != []


def test_pending_partial_interval_included_in_chart2():
    agg = SessionAggregator(percentile=95, min_samples=1000, interval_seconds=60)
    agg.observe(T0, "BID", [(100.0, 60, 1)])
    agg.observe(T0, "ASK", [(101.0, 40, 1)])
    # no interval boundary crossed yet -- should still show up as a pending point
    series = agg.chart2_series()
    assert len(series) == 1

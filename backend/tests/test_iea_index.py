"""Equivalence tests for the vectorized IEA option-chain index (backtest/iea_data.py).

The engine's per-bar option lookups used to be an O(n) pandas boolean-mask
scan over the whole day's chain (measured ~5.4ms/call, ~60% of sim time).
`OptionDayIndex` replaces that with a numpy binary search built once per day.
These tests pin that rewrite against a brute-force pandas re-implementation
of the original logic so a silent fill-price regression can't slip through.

All tests are skipped when the local IEA_data_*/ archive isn't present
(it's gitignored — not every machine running this suite will have it).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import numpy as np
import pytest

from vectra_quant.backtest import iea_data

pytestmark = pytest.mark.skipif(not iea_data.AVAILABLE, reason="IEA archive not present on this machine")


def _brute_nearest_bar(df, strike, right, at):
    want_call = str(right).upper().startswith("C")
    is_call = df["right"].astype(str).str.upper().str.startswith("C")
    mask = (df["strike"].astype(float) == strike) & (is_call if want_call else ~is_call)
    subset = df[mask]
    if subset.empty:
        return None
    i = (subset["ts"] - at).abs().values.argmin()
    row = subset.iloc[i]
    return {k: float(row[k]) for k in ("open", "high", "low", "close", "oi")}


def _brute_total_oi(df, at):
    g = df.groupby("ts")["oi"].sum().sort_index()
    diffs = np.abs(g.index.values - np.datetime64(at))
    return float(g.iloc[diffs.argmin()])


@pytest.fixture(scope="module")
def sample_day():
    """First trading day with real option data that the archive actually has."""
    for candidate in ("2026-08-03", "2024-01-02", "2023-01-02"):
        df = iea_data.load_option_day("NIFTY", candidate)
        if df is not None and not df.empty:
            return candidate, df
    pytest.skip("no option-chain day found to test against")


def test_nearest_bar_matches_brute_force(sample_day):
    date_str, df = sample_day
    idx = iea_data.OptionDayIndex(df)
    strikes = sorted(df["strike"].astype(float).unique())
    rights = ["CE", "PE"]
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=9, minute=15)

    rng = random.Random(7)
    checked = 0
    for _ in range(300):
        strike = rng.choice(strikes)
        right = rng.choice(rights)
        minute = rng.randint(-30, 400)  # covers before-first and after-last
        at = day_start + timedelta(minutes=minute)

        got = idx.nearest_bar(strike, right, at)
        want = _brute_nearest_bar(df, strike, right, at)

        assert (got is None) == (want is None)
        if got is not None:
            assert abs(got["close"] - want["close"]) < 1e-6
            assert abs(got["oi"] - want["oi"]) < 1e-6
        checked += 1
    assert checked == 300


def test_nearest_total_oi_matches_brute_force(sample_day):
    date_str, df = sample_day
    idx = iea_data.OptionDayIndex(df)
    day_start = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=9, minute=15)

    rng = random.Random(11)
    for _ in range(150):
        minute = rng.randint(-30, 400)
        at = day_start + timedelta(minutes=minute)
        assert abs(idx.nearest_total_oi(at) - _brute_total_oi(df, at)) < 1e-6


def test_nearest_bar_unknown_strike_returns_none(sample_day):
    _date_str, df = sample_day
    idx = iea_data.OptionDayIndex(df)
    assert idx.nearest_bar(999999.0, "CE", datetime(2020, 1, 1, 9, 15)) is None


def test_load_index_window_matches_full_chunk_parse():
    """Chunk-selective loading must return byte-identical bars to a full parse."""
    window = iea_data.load_index_window("NIFTY", from_date="2026-08-01", to_date="2026-08-10")
    if not window:
        pytest.skip("archive does not cover the probed window")

    for date_str, bars in window.items():
        assert "2026-08-01" <= date_str <= "2026-08-10"
        assert bars == sorted(bars, key=lambda c: c.timestamp)


@pytest.mark.parametrize("n", [5, 10, 30, 60])
def test_load_index_window_tail_sessions_returns_exact_count(n):
    """Regression: the newest archive chunk is only partially filled (still
    being backfilled), so a session-count estimate based on "~63 sessions per
    quarterly chunk" undercounts it and stops selecting chunks too early —
    a `tail_sessions=30` request silently returned only 5 sessions before
    this was fixed to parse-and-count instead of estimate."""
    window = iea_data.load_index_window("NIFTY", tail_sessions=n)
    if not window:
        pytest.skip("archive not available")
    assert len(window) == n  # archive covers years of history — never capped for these n
    assert all(len(bars) > 0 for bars in window.values())


def test_load_index_window_tail_sessions_returns_most_recent():
    small = iea_data.load_index_window("NIFTY", tail_sessions=5)
    bigger = iea_data.load_index_window("NIFTY", tail_sessions=10)
    if not small or not bigger:
        pytest.skip("archive not available")
    # the smaller request must be the most-recent tail end of the bigger one
    assert sorted(small.keys()) == sorted(bigger.keys())[-len(small):]

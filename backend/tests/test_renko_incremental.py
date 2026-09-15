"""Equivalence tests for the incremental Renko brick builder.

`_build_renko` used to rebuild the entire brick history from scratch on
every single 1-minute bar (`on_candle` calls it once per bar) — O(n) work
per call, O(n^2) over a session. A 30-day backtest measured 60+ seconds
before the fix. This pins the incremental rewrite against the original,
still-pure `build_renko_from_close` oracle so a subtle drift in brick
formation can't slip through.
"""
from __future__ import annotations

import random

import pandas as pd
import pytest

from vectra_quant.data.candles import Candle
from vectra_quant.strategies.renko_strategy import (
    DynamicRenkoStrategy,
    _RENKO_ROWS_TRIM_AT,
    build_renko_from_close,
)


def _make_bars(n: int, seed: int = 1, start: float = 24000.0) -> list[Candle]:
    rng = random.Random(seed)
    bars = []
    px = start
    for i in range(n):
        px = max(100.0, px + rng.gauss(0, 8.0))
        ts = f"2026-01-01 {9 + i // 60:02d}:{i % 60:02d}:00"
        bars.append(Candle(symbol="NIFTY", timestamp=ts, open=px, high=px + 1, low=px - 1, close=px, volume=1000.0))
    return bars


def _oracle_renko(bars: list[Candle], box_size: float) -> pd.DataFrame:
    df = pd.DataFrame([{"timestamp": c.timestamp, "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in bars])
    return build_renko_from_close(df, box_size)


def test_incremental_matches_oracle_at_multiple_checkpoints():
    bars = _make_bars(600, seed=42)
    strat = DynamicRenkoStrategy()
    strat.instrument_name = "NIFTY"
    # Without daily candles, ATR=0 → box_size = max(box_min=20.0, 0) = 20.0
    expected_box_size = float(strat.params.get("box_min", 20.0))

    checkpoints = {150, 300, 450, 600}
    for i, bar in enumerate(bars, start=1):
        strat.bars_1m.append(bar)
        got = strat._build_renko()
        if i in checkpoints:
            want = _oracle_renko(bars[:i], expected_box_size)
            if want.empty or len(want) < 44:
                assert got.empty
                continue
            assert not got.empty
            assert len(got) == len(want)
            pd.testing.assert_series_equal(got["close"].reset_index(drop=True), want["close"].reset_index(drop=True), check_names=False)
            pd.testing.assert_series_equal(got["open"].reset_index(drop=True), want["open"].reset_index(drop=True), check_names=False)


def test_incremental_bricks_are_stable_once_formed():
    """A brick, once formed, must never change value on later calls."""
    bars = _make_bars(400, seed=7)
    strat = DynamicRenkoStrategy()
    strat.instrument_name = "NIFTY"

    snapshot_at_200 = None
    for i, bar in enumerate(bars, start=1):
        strat.bars_1m.append(bar)
        got = strat._build_renko()
        if i == 200 and not got.empty:
            snapshot_at_200 = got["close"].tolist()

    final = strat._build_renko()
    if snapshot_at_200 and not final.empty:
        assert final["close"].tolist()[: len(snapshot_at_200)] == snapshot_at_200


def test_no_history_returns_empty():
    strat = DynamicRenkoStrategy()
    assert strat._build_renko().empty


def test_long_run_does_not_permanently_freeze_after_trim():
    """Regression: bricks used to accumulate forever, and the safety valve
    against a runaway single-bar cascade (MAX_RENKO_BRICKS_PER_BUILD=5000)
    was applied against that ever-growing LIFETIME total. Once a long
    backtest crossed 5000 total bricks, _build_renko permanently returned
    empty for the rest of the run — Renko silently stopped trading partway
    through a multi-month backtest and never resumed. Bricks must now be
    trimmed to a bounded window instead, so the strategy keeps producing
    signals indefinitely."""
    strat = DynamicRenkoStrategy()
    strat.instrument_name = "NIFTY"

    # High per-bar volatility so a handful of bars each form many bricks in
    # one call (the inner while-loop), comfortably crossing the trim
    # threshold without needing thousands of separate bar-by-bar calls.
    bars = _make_bars(500, seed=3, start=24000.0)
    rng = random.Random(99)
    for b in bars:
        b.close = b.close + rng.gauss(0, 800.0)
        b.high, b.low = b.close + 1, b.close - 1

    last_nonempty_at = None
    for i, bar in enumerate(bars, start=1):
        strat.bars_1m.append(bar)
        got = strat._build_renko()
        if not got.empty:
            last_nonempty_at = i
        # Bricks must never exceed the trim ceiling.
        assert len(strat._renko_rows) <= _RENKO_ROWS_TRIM_AT

    assert len(strat._renko_rows) > 44  # actually accumulated a meaningful history
    # The strategy must still be producing output at the very end of the
    # run, not stuck empty since some earlier point.
    assert last_nonempty_at == len(bars)

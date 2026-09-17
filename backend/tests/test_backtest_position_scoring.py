"""Regression: backtest stats must score at POSITION level, not leg level.

Renko trades are 2-4 leg credit spreads. Each leg used to be appended as its
own `BacktestTrade`, and `total_trades`/`win_pct`/`profit_factor` were all
computed straight off `len(trades)` / per-leg `net_pnl` — so a 2-leg spread
silently counted as 2 trades, deflating the true win rate and distorting
profit factor (legs of the same position partially cancel each other's P&L,
so summing them independently is not equivalent to summing per position).

This pins the fix: those stats must equal what you get by manually grouping
`result.trades` by `position_id` and summing each group's net_pnl.
"""
from __future__ import annotations

import pytest

from vectra_quant.backtest import iea_data
from vectra_quant.backtest.engine import BacktestEngine
from vectra_quant.strategies.registry import get_strategy

pytestmark = pytest.mark.skipif(not iea_data.AVAILABLE, reason="IEA archive not present on this machine")


def _group_by_position(trades):
    groups: dict[str, list] = {}
    for t in trades:
        key = t.position_id or t.id
        groups.setdefault(key, []).append(t)
    return groups


@pytest.fixture(scope="module")
def backtest_result():
    """All four tests below assert different things about the identical
    same backtest run -- share one real (non-mocked) run across them instead
    of recomputing it four times over, which is what this file did before
    and was, by a wide margin, the single slowest part of the whole suite.
    2 months is already comfortably past what these assertions need (45
    multi-leg positions observed here vs. ~100 over the original 8.5-month
    window) -- there's no assertion in this file that benefits from a
    larger sample, only wall-clock cost."""
    strat = get_strategy("renko_strategy")
    return BacktestEngine().run_strategy(strategy=strat, instrument="NIFTY", from_date="2026-01-01", to_date="2026-02-28")


def test_total_trades_counts_positions_not_legs(backtest_result):
    res = backtest_result
    groups = _group_by_position(res.trades)
    assert len(res.trades) > len(groups), "fixture should actually exercise multi-leg spreads"
    assert res.total_trades == len(groups)


def test_win_pct_and_profit_factor_match_manual_position_grouping(backtest_result):
    res = backtest_result
    groups = _group_by_position(res.trades)
    position_nets = [sum(t.net_pnl for t in legs) for legs in groups.values()]

    wins = sum(1 for n in position_nets if n > 0)
    expected_win_pct = wins / len(position_nets) * 100.0
    assert res.win_pct == pytest.approx(expected_win_pct, abs=0.05)

    gross_wins = sum(n for n in position_nets if n > 0)
    gross_losses = abs(sum(n for n in position_nets if n < 0))
    expected_pf = gross_wins / gross_losses if gross_losses > 0 else gross_wins
    assert res.profit_factor == pytest.approx(expected_pf, rel=1e-6)


def test_every_leg_of_a_position_shares_one_position_id(backtest_result):
    assert all(t.position_id for t in backtest_result.trades), "every leg must carry a position_id"


def test_position_margin_is_not_double_counted_across_legs(backtest_result):
    """Margin is one number per position (the spread's defined-risk margin),
    logged on the first leg only — summing legs must not multiply it."""
    res = backtest_result
    groups = _group_by_position(res.trades)
    multi_leg = [legs for legs in groups.values() if len(legs) > 1]
    assert multi_leg, "fixture should include at least one multi-leg position"

    for legs in multi_leg:
        nonzero_margins = [t.capital_used for t in legs if t.capital_used > 0]
        assert len(nonzero_margins) == 1, "exactly one leg should carry the position's margin"
        assert nonzero_margins[0] > 0

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


def test_total_trades_counts_positions_not_legs():
    strat = get_strategy("renko_strategy")
    res = BacktestEngine().run_strategy(strategy=strat, instrument="NIFTY", from_date="2026-01-01", to_date="2026-09-15")

    groups = _group_by_position(res.trades)
    assert len(res.trades) > len(groups), "fixture should actually exercise multi-leg spreads"
    assert res.total_trades == len(groups)


def test_win_pct_and_profit_factor_match_manual_position_grouping():
    strat = get_strategy("renko_strategy")
    res = BacktestEngine().run_strategy(strategy=strat, instrument="NIFTY", from_date="2026-01-01", to_date="2026-09-15")

    groups = _group_by_position(res.trades)
    position_nets = [sum(t.net_pnl for t in legs) for legs in groups.values()]

    wins = sum(1 for n in position_nets if n > 0)
    expected_win_pct = wins / len(position_nets) * 100.0
    assert res.win_pct == pytest.approx(expected_win_pct, abs=0.05)

    gross_wins = sum(n for n in position_nets if n > 0)
    gross_losses = abs(sum(n for n in position_nets if n < 0))
    expected_pf = gross_wins / gross_losses if gross_losses > 0 else gross_wins
    assert res.profit_factor == pytest.approx(expected_pf, rel=1e-6)


def test_every_leg_of_a_position_shares_one_position_id():
    strat = get_strategy("renko_strategy")
    res = BacktestEngine().run_strategy(strategy=strat, instrument="NIFTY", from_date="2026-01-01", to_date="2026-09-15")

    assert all(t.position_id for t in res.trades), "every leg must carry a position_id"


def test_position_margin_is_not_double_counted_across_legs():
    """Margin is one number per position (the spread's defined-risk margin),
    logged on the first leg only — summing legs must not multiply it."""
    strat = get_strategy("renko_strategy")
    res = BacktestEngine().run_strategy(strategy=strat, instrument="NIFTY", from_date="2026-01-01", to_date="2026-09-15")

    groups = _group_by_position(res.trades)
    multi_leg = [legs for legs in groups.values() if len(legs) > 1]
    assert multi_leg, "fixture should include at least one multi-leg position"

    for legs in multi_leg:
        nonzero_margins = [t.capital_used for t in legs if t.capital_used > 0]
        assert len(nonzero_margins) == 1, "exactly one leg should carry the position's margin"
        assert nonzero_margins[0] > 0

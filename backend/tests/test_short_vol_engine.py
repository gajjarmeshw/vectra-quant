"""Tests for the intraday short-volatility engine.

The aggregation maths is tested with synthetic trades so it runs anywhere; the
end-to-end run is skipped when the IEA option archive is not present (it is
gitignored and only exists on machines that have downloaded it).
"""
from __future__ import annotations

import pytest

from vectra_quant.backtest import iea_data
from vectra_quant.backtest.short_vol_engine import (
    NIFTY_LOT_SIZE,
    ShortVolEngine,
    ShortVolResult,
    ShortVolTrade,
)
from vectra_quant.strategies.registry import get_strategy, list_strategies


def _trade(date: str, net: float) -> ShortVolTrade:
    """Minimal trade carrying a given net P&L; price fields are not used by the
    aggregation properties under test."""
    return ShortVolTrade(
        date=date, expiry=date, dte=0, strike=23000.0,
        spot_entry=23000.0, spot_exit=23000.0,
        ce_entry=100.0, pe_entry=100.0, ce_exit=50.0, pe_exit=50.0,
        credit=13000.0, buyback=6500.0,
        gross_pnl=net + 160.0, costs=160.0, net_pnl=net, win=net > 0,
        entry_iv=12.0,
    )


def test_registry_exposes_short_vol():
    names = [s["name"] for s in list_strategies()]
    assert "short_vol" in names
    strat = get_strategy("short_vol")
    assert strat.EXECUTION_MODE == "intraday_short_vol"
    # Cost default must stay the MEASURED figure -- an earlier Rs100 guess
    # inverted the strategy's conclusion, so this is worth pinning.
    assert strat.params["cost_per_leg"] == 40.0


def test_result_aggregates_wins_losses_and_pnl():
    r = ShortVolResult(instrument="NIFTY", start_date="2026-01-01", end_date="2026-01-05")
    r.trades = [_trade("2026-01-01", 500.0), _trade("2026-01-02", -200.0),
                _trade("2026-01-03", 300.0)]
    assert r.total_trades == 3
    assert r.wins == 2 and r.losses == 1
    assert r.win_pct == pytest.approx(66.7, abs=0.1)
    assert r.net_pnl == pytest.approx(600.0)
    assert r.profit_factor == pytest.approx(800.0 / 200.0)
    assert r.worst_day == pytest.approx(-200.0)
    assert r.best_day == pytest.approx(500.0)


def test_max_drawdown_is_peak_to_trough_not_worst_trade():
    """A run of losses after a peak must compound into the drawdown; reporting
    only the single worst trade would understate the capital actually at risk."""
    r = ShortVolResult(instrument="NIFTY", start_date="a", end_date="b")
    r.trades = [_trade("d1", 1000.0), _trade("d2", -400.0),
                _trade("d3", -300.0), _trade("d4", 200.0)]
    assert r.max_drawdown == pytest.approx(-700.0)   # not -400 (the worst single day)


def test_empty_result_is_safe():
    r = ShortVolResult(instrument="NIFTY", start_date="a", end_date="b")
    assert r.total_trades == 0
    assert r.win_pct == 0.0
    assert r.profit_factor == 0.0
    assert r.max_drawdown == 0.0
    d = r.as_dict()
    assert d["total_trades"] == 0 and d["net_pnl"] == 0.0


def test_as_dict_carries_the_fields_the_api_maps():
    r = ShortVolResult(instrument="NIFTY", start_date="2026-01-01", end_date="2026-01-02")
    r.trades = [_trade("2026-01-01", 100.0)]
    d = r.as_dict()
    for key in ("instrument", "start_date", "end_date", "total_trades", "wins", "losses",
                "win_pct", "profit_factor", "gross_pnl", "total_costs", "net_pnl",
                "max_drawdown", "worst_day", "best_day", "data_source"):
        assert key in d, f"missing {key} -- routes.py maps this into the FE payload"


@pytest.mark.skipif(not iea_data.AVAILABLE, reason="IEA option archive not present")
def test_engine_runs_against_real_archive():
    result = ShortVolEngine().run(instrument="NIFTY", from_date="2026-06-01", to_date="2026-06-30")
    assert result.total_trades > 0
    for t in result.trades:
        assert t.credit > 0
        # short premium: gross is credit minus buyback, costs are 4 legs
        assert t.gross_pnl == pytest.approx(t.credit - t.buyback, abs=0.01)
        assert t.net_pnl == pytest.approx(t.gross_pnl - t.costs, abs=0.01)
        assert t.win == (t.net_pnl > 0)
        assert t.costs == pytest.approx(4 * 40.0)


@pytest.mark.skipif(not iea_data.AVAILABLE, reason="IEA option archive not present")
def test_min_dte_filter_excludes_expiry_day():
    """DTE=0 measured worst in research (expiry-day gamma); the filter must
    actually drop those sessions when asked."""
    kept = ShortVolEngine().run(instrument="NIFTY", from_date="2026-06-01",
                                to_date="2026-06-30", min_dte=0)
    filtered = ShortVolEngine().run(instrument="NIFTY", from_date="2026-06-01",
                                    to_date="2026-06-30", min_dte=1)
    assert all(t.dte >= 1 for t in filtered.trades)
    assert filtered.total_trades <= kept.total_trades


@pytest.mark.skipif(not iea_data.AVAILABLE, reason="IEA option archive not present")
def test_lots_scale_pnl_linearly():
    one = ShortVolEngine().run(instrument="NIFTY", from_date="2026-06-01", to_date="2026-06-30", lots=1)
    two = ShortVolEngine().run(instrument="NIFTY", from_date="2026-06-01", to_date="2026-06-30", lots=2)
    assert two.total_trades == one.total_trades
    assert two.gross_pnl == pytest.approx(2 * one.gross_pnl, rel=1e-6)
    assert two.total_costs == pytest.approx(2 * one.total_costs, rel=1e-6)

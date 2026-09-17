"""Tests for the futures execution path (backtest/futures_engine.py).

Fast, logic-focused tests for sizing and the cost model (no engine calls);
one real end-to-end smoke test against the archive; one basis-validation
test against the 3 real (live, non-expired) futures contracts the archive
actually has.
"""
from __future__ import annotations

import pytest

from vectra_quant.backtest import iea_data
from vectra_quant.backtest.futures_engine import (
    DEFAULT_MAX_LOTS,
    FuturesBacktestEngine,
    measure_futures_basis,
    size_position,
)
from vectra_quant.brokers.costs import FUTURES_COST_RATES, CostRates, net_pnl
from vectra_quant.strategies.registry import get_strategy

pytestmark = pytest.mark.skipif(not iea_data.AVAILABLE, reason="IEA archive not present on this machine")


# ---------------------------------------------------------------- size_position

def test_size_position_basic_formula():
    # risk 3000, stop 30pts, lot 65 -> 3000/(30*65)=1.53 -> floor 1
    assert size_position(3000, 30, 65) == 1
    # risk 120000, stop 30pts, lot 65 -> 61.5 -> capped at DEFAULT_MAX_LOTS
    assert size_position(120000, 30, 65) == DEFAULT_MAX_LOTS


def test_size_position_respects_custom_cap():
    assert size_position(120000, 30, 65, max_lots=5) == 5


def test_size_position_rejects_degenerate_inputs():
    assert size_position(120000, 0, 65) == 0       # zero stop distance
    assert size_position(0, 30, 65) == 0           # zero risk budget
    assert size_position(120000, 30, 0) == 0       # zero lot size


# ---------------------------------------------------------------- cost model

def test_futures_stt_rate_is_five_times_cheaper_than_options():
    """The rate itself really is 5x cheaper (0.02% vs 0.1%) — the surprising
    finding is that this does NOT translate to 5x cheaper rupee costs,
    because futures notional (index level x qty) dwarfs an option's premium
    turnover. Both facts matter; this test pins the first."""
    options_rates = CostRates()
    assert FUTURES_COST_RATES.stt_sell_pct == pytest.approx(options_rates.stt_sell_pct / 5)


def test_futures_net_pnl_long_and_short_are_mirror_images_before_costs():
    qty = 65
    gross_long, _, _ = net_pnl(23000.0, 23100.0, qty, rates=FUTURES_COST_RATES)
    # a short is (buy_price=exit, sell_price=entry) per the engine's convention
    gross_short, _, _ = net_pnl(23100.0, 23000.0, qty, rates=FUTURES_COST_RATES)
    assert gross_long == pytest.approx(100.0 * qty)
    assert gross_short == pytest.approx(-100.0 * qty)


def test_futures_round_trip_cost_is_a_meaningful_fraction_of_notional():
    """Sanity bound: on ~23000 x 65 notional, round-trip cost should land in
    the few-hundred-rupee range, not near-zero and not thousands."""
    _, costs, _ = net_pnl(23000.0, 23100.0, 65, rates=FUTURES_COST_RATES)
    assert 200.0 < costs < 800.0


# ---------------------------------------------------------------- engine wiring

def test_futures_engine_rejects_pull_mode_strategy():
    class _PullStrat:
        name = "not_push_mode"
        USES_PUSH_SIGNALS = False

    with pytest.raises(ValueError, match="push signals"):
        FuturesBacktestEngine().run_strategy(strategy=_PullStrat(), instrument="NIFTY", from_date="2026-08-01", to_date="2026-08-05")


def test_futures_engine_runs_end_to_end_against_real_archive():
    strat = get_strategy("renko_strategy", {"box_min": 75.0, "box_atr_mult": 0.0})
    res = FuturesBacktestEngine().run_strategy(strategy=strat, instrument="NIFTY", from_date="2026-08-01", to_date="2026-08-31")

    assert res.data_source.startswith("INDEX_PROXY")
    assert res.total_trades == res.wins + res.losses
    for t in res.trades:
        assert t.qty == t.lots * 65
        assert t.direction in ("LONG", "SHORT")


def test_multiday_mode_does_not_permanently_lock_after_first_trade():
    """Regression: day-scoped risk bookkeeping (trade cap, day P&L, LOCKED
    state) was never reset in allow_multiday mode, since resetting the whole
    RiskEngine was conflated with "don't force-close the position". The
    first loss permanently LOCKed the engine, so a full year produced
    exactly 1 trade. Must now reset day-state every session regardless."""
    strat = get_strategy("renko_strategy", {"box_min": 75.0, "box_atr_mult": 0.0})
    res = FuturesBacktestEngine().run_strategy(
        strategy=strat, instrument="NIFTY", from_date="2025-01-01", to_date="2025-12-31", allow_multiday=True)
    assert res.total_trades > 5, "multi-day mode must keep taking trades across the whole year, not stall after one"


def test_futures_engine_lot_size_scales_with_risk_per_trade():
    """The whole point of Phase 3 sizing: bigger capital -> bigger risk
    budget -> more lots (previously this was hardcoded to 1 regardless)."""
    from vectra_quant.risk_engine import RiskConfig

    strat_small = get_strategy("renko_strategy", {"box_min": 75.0, "box_atr_mult": 0.0})
    small_cfg = RiskConfig(capital=15000, risk_per_trade_pct=0.08)  # ~Rs.1200 budget
    res_small = FuturesBacktestEngine(risk_cfg=small_cfg).run_strategy(
        strategy=strat_small, instrument="NIFTY", from_date="2026-08-01", to_date="2026-08-31")

    strat_big = get_strategy("renko_strategy", {"box_min": 75.0, "box_atr_mult": 0.0})
    big_cfg = RiskConfig(capital=500000, risk_per_trade_pct=0.08)  # ~Rs.40,000 budget
    res_big = FuturesBacktestEngine(risk_cfg=big_cfg).run_strategy(
        strategy=strat_big, instrument="NIFTY", from_date="2026-08-01", to_date="2026-08-31")

    if res_small.trades and res_big.trades:
        assert res_big.trades[0].lots >= res_small.trades[0].lots


# ---------------------------------------------------------------- basis validation

def test_measure_futures_basis_against_real_contract():
    """Uses the archive's real (live, non-expired) Sep-2026 contract — the
    only ground truth available for validating the index-proxy model."""
    stats = measure_futures_basis("NIFTY-Sep2026-FUT", "NIFTY", from_date="2026-08-05", to_date="2026-09-14")
    if stats is None:
        pytest.skip("Sep-2026 futures contract not in this archive")

    assert stats.n_minutes > 1000
    # Basis is a cost-of-carry premium, not noise -> should be a stable,
    # consistently-signed, moderate-magnitude number, not wildly erratic.
    assert stats.mean_basis > 0
    assert stats.stdev_basis < abs(stats.mean_basis)
    # The number that actually bounds the proxy model's error for a HELD
    # position must be far smaller than the raw basis level itself.
    assert stats.mean_intraday_drift < stats.mean_basis


def test_measure_futures_basis_returns_none_for_unknown_contract():
    assert measure_futures_basis("NOT-A-REAL-CONTRACT", "NIFTY") is None

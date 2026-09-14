from __future__ import annotations

from datetime import time

import pytest
from sentinel.backtest.engine import BacktestEngine, generate_synthetic_candles
from sentinel.data.regimes import OptionWalls, PriceOIRegime
from sentinel.strategies import (
    BaseStrategy,
    InstitutionalBreakoutStrategy,
    OptionWallTrappedSqueezeStrategy,
    StrategyContext,
    WallMeanReversionStrategy,
    get_strategy,
    list_strategies,
)


def test_strategy_registry():
    available = list_strategies()
    names = [s["name"] for s in available]
    assert "institutional_breakout" in names
    assert "option_wall_squeeze" in names
    assert "wall_mean_reversion" in names

    strat = get_strategy("institutional_breakout", {"sl_points": 20.0})
    assert isinstance(strat, BaseStrategy)
    assert strat.params["sl_points"] == 20.0

    with pytest.raises(ValueError):
        get_strategy("non_existent_strat")


def test_institutional_breakout_signals():
    strat = InstitutionalBreakoutStrategy()

    # Bullish ORH Break with Long Buildup
    ctx_bull = StrategyContext(
        instrument="NIFTY",
        spot=24550.0,
        opening_range=(24500.0, 24400.0),
        regime=PriceOIRegime.LONG_BUILDUP,
        walls=OptionWalls(call_wall=24800.0, put_wall=24300.0),
        current_time=time(10, 0),
    )
    sig_bull = strat.evaluate(ctx_bull)
    assert sig_bull is not None
    assert sig_bull.direction == "CE"
    assert "trapped" in sig_bull.thesis.lower()

    # Filtered out if move is only Short Covering (fades fast)
    ctx_sc = StrategyContext(
        instrument="NIFTY",
        spot=24550.0,
        opening_range=(24500.0, 24400.0),
        regime=PriceOIRegime.SHORT_COVERING,
        walls=OptionWalls(call_wall=24800.0, put_wall=24300.0),
        current_time=time(10, 0),
    )
    assert strat.evaluate(ctx_sc) is None

    # Bearish ORL Breakdown with Short Buildup
    ctx_bear = StrategyContext(
        instrument="NIFTY",
        spot=24350.0,
        opening_range=(24500.0, 24400.0),
        regime=PriceOIRegime.SHORT_BUILDUP,
        walls=OptionWalls(call_wall=24800.0, put_wall=24200.0),
        current_time=time(10, 0),
    )
    sig_bear = strat.evaluate(ctx_bear)
    assert sig_bear is not None
    assert sig_bear.direction == "PE"


def test_option_wall_squeeze_signals():
    strat = OptionWallTrappedSqueezeStrategy()

    # Breached Call Wall -> trapped writers cover delta
    ctx_call_break = StrategyContext(
        instrument="NIFTY",
        spot=24820.0,
        walls=OptionWalls(call_wall=24800.0, put_wall=24400.0),
        current_time=time(10, 30),
    )
    sig_call = strat.evaluate(ctx_call_break)
    assert sig_call is not None
    assert sig_call.direction == "CE"
    assert "trapped institutional call writers" in sig_call.thesis.lower()

    # Breached Put Wall -> trapped writers liquidate
    ctx_put_break = StrategyContext(
        instrument="NIFTY",
        spot=24380.0,
        walls=OptionWalls(call_wall=24800.0, put_wall=24400.0),
        current_time=time(10, 30),
    )
    sig_put = strat.evaluate(ctx_put_break)
    assert sig_put is not None
    assert sig_put.direction == "PE"
    assert "trapped institutional put writers" in sig_put.thesis.lower()


def test_wall_mean_reversion_signals():
    strat = WallMeanReversionStrategy()

    # Testing Call Wall ceiling -> Fade back toward center -> Buy PE
    walls_test_call = OptionWalls(
        call_wall=24800.0,
        put_wall=24400.0,
        dist_call_wall_pct=0.10,
        dist_put_wall_pct=1.5,
    )
    ctx_call_test = StrategyContext(
        instrument="NIFTY",
        spot=24780.0,
        walls=walls_test_call,
        current_time=time(11, 0),
    )
    sig_revert_call = strat.evaluate(ctx_call_test)
    assert sig_revert_call is not None
    assert sig_revert_call.direction == "PE"
    assert "fading trapped breakout buyers" in sig_revert_call.thesis.lower()

    # Testing Put Wall floor -> Bounce back toward center -> Buy CE
    walls_test_put = OptionWalls(
        call_wall=24800.0,
        put_wall=24400.0,
        dist_call_wall_pct=1.5,
        dist_put_wall_pct=0.10,
    )
    ctx_put_test = StrategyContext(
        instrument="NIFTY",
        spot=24420.0,
        walls=walls_test_put,
        current_time=time(11, 0),
    )
    sig_revert_put = strat.evaluate(ctx_put_test)
    assert sig_revert_put is not None
    assert sig_revert_put.direction == "CE"
    assert "fading trapped breakdown sellers" in sig_revert_put.thesis.lower()


def test_dynamic_backtest_engine_execution():
    bte = BacktestEngine()
    candles = generate_synthetic_candles("NIFTY", days=3, seed=42)

    for strat_name in ["institutional_breakout", "option_wall_squeeze", "wall_mean_reversion"]:
        result = bte.run_strategy(strat_name, instrument="NIFTY", days=3, candles_by_day=candles)
        assert result.strategy_name == strat_name
        assert "check_08_real_costs_included" in result.checklist_score
        assert "actual_profit_factor" in result.checklist_score

        wiggle = bte.parameter_wiggle_test(strat_name, instrument="NIFTY", days=3, candles_by_day=candles)
        assert wiggle["strategy"] == strat_name
        assert wiggle["verdict"] in ("PLATEAU", "NEEDLE")

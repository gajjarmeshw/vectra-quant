"""Unit tests for the Institutional Shadowing Framework & 23-Point Algo Checklist.

Tests:
1. Price + OI Regime Matrix classifications (Long/Short Buildup vs Short Covering / Long Unwinding).
2. Option Walls (Call/Put Walls), PCR calculation, and extreme sentiment tags.
3. EventEngine institutional triggers (Wall Test, Wall Break with trapped sellers, PCR Extremes).
4. Backtest Engine honest slippage and Check 17 Parameter Wiggle Test.
"""
from __future__ import annotations

from sentinel.backtest.engine import BacktestEngine, generate_synthetic_candles
from sentinel.data.chain import StrikeData
from sentinel.data.regimes import (
    OptionWalls,
    PriceOIRegime,
    compute_option_walls,
    compute_price_oi_regime,
)
from sentinel.events.engine import EventEngine, EventKind


def test_price_oi_regime_matrix():
    # Long Buildup: Price UP + OI UP
    regime = compute_price_oi_regime(price_change_pct=0.8, oi_change_pct=15.0)
    assert regime == PriceOIRegime.LONG_BUILDUP
    assert regime.is_trend_healthy is True
    assert regime.allows_ce_buy is True
    assert regime.allows_pe_buy is False

    # Short Buildup: Price DOWN + OI UP
    regime = compute_price_oi_regime(price_change_pct=-0.9, oi_change_pct=22.0)
    assert regime == PriceOIRegime.SHORT_BUILDUP
    assert regime.is_trend_healthy is True
    assert regime.allows_ce_buy is False
    assert regime.allows_pe_buy is True

    # Short Covering: Price UP + OI DOWN (Fades fast, do not chase)
    regime = compute_price_oi_regime(price_change_pct=0.6, oi_change_pct=-12.0)
    assert regime == PriceOIRegime.SHORT_COVERING
    assert regime.is_trend_healthy is False
    assert regime.allows_ce_buy is False

    # Long Unwinding: Price DOWN + OI DOWN (Weak drift)
    regime = compute_price_oi_regime(price_change_pct=-0.5, oi_change_pct=-8.0)
    assert regime == PriceOIRegime.LONG_UNWINDING
    assert regime.is_trend_healthy is False
    assert regime.allows_pe_buy is False

    # Neutral: Movements below minimum threshold
    regime = compute_price_oi_regime(price_change_pct=0.01, oi_change_pct=0.02)
    assert regime == PriceOIRegime.NEUTRAL


def test_option_walls_and_pcr():
    chain = [
        StrikeData(strike=24300.0, ce_ltp=300.0, ce_oi=20000.0, pe_ltp=25.0, pe_oi=180000.0),
        StrikeData(strike=24400.0, ce_ltp=220.0, ce_oi=50000.0, pe_ltp=45.0, pe_oi=95000.0),
        StrikeData(strike=24500.0, ce_ltp=150.0, ce_oi=110000.0, pe_ltp=80.0, pe_oi=70000.0),
        StrikeData(strike=24600.0, ce_ltp=90.0, ce_oi=250000.0, pe_ltp=130.0, pe_oi=30000.0),
        StrikeData(strike=24700.0, ce_ltp=50.0, ce_oi=140000.0, pe_ltp=210.0, pe_oi=10000.0),
    ]
    spot = 24510.0
    walls = compute_option_walls(chain, spot_price=spot)

    # Call Wall should be strike with highest Call OI (24600 with 250k OI)
    assert walls.call_wall == 24600.0
    assert walls.call_wall_oi == 250000.0

    # Put Wall should be strike with highest Put OI (24300 with 180k OI)
    assert walls.put_wall == 24300.0
    assert walls.put_wall_oi == 180000.0

    # Expected range
    assert walls.expected_range == (24300.0, 24600.0)

    # PCR calculation
    total_pe_oi = 180000.0 + 95000.0 + 70000.0 + 30000.0 + 10000.0
    total_ce_oi = 20000.0 + 50000.0 + 110000.0 + 250000.0 + 140000.0
    expected_pcr = round(total_pe_oi / total_ce_oi, 2)
    assert walls.pcr == expected_pcr

    # PCR Sentiment check
    low_pcr_chain = [StrikeData(strike=24500.0, ce_oi=200000.0, pe_oi=50000.0)]
    walls_low = compute_option_walls(low_pcr_chain, spot_price=24500.0)
    assert walls_low.pcr_sentiment == "FEAR_OVERSOLD"

    high_pcr_chain = [StrikeData(strike=24500.0, ce_oi=50000.0, pe_oi=200000.0)]
    walls_high = compute_option_walls(high_pcr_chain, spot_price=24500.0)
    assert walls_high.pcr_sentiment == "COMPLACENT_OVERBOUGHT"


def test_event_engine_institutional_detectors():
    engine = EventEngine()
    walls = OptionWalls(
        call_wall=24600.0,
        put_wall=24300.0,
        dist_call_wall_pct=0.15,  # Within 0.2% testing distance
        dist_put_wall_pct=1.0,
    )

    # 1. Option Wall Test trigger
    events = engine.check_option_wall_events("NIFTY", spot=24565.0, walls=walls)
    assert len(events) == 1
    assert events[0].kind == EventKind.OPTION_WALL_TEST
    assert events[0].detail["wall"] == "CALL"
    assert events[0].detail["bias"] == "resistance_fade"

    # Debounce prevents immediate re-fire
    repeat_events = engine.check_option_wall_events("NIFTY", spot=24565.0, walls=walls)
    assert len(repeat_events) == 0

    # 2. Option Wall Break (spot crossing above call wall)
    break_events = engine.check_option_wall_events(
        "NIFTY", spot=24610.0, walls=walls, prev_spot=24590.0,
    )
    assert len(break_events) == 1
    assert break_events[0].kind == EventKind.OPTION_WALL_BREAK
    assert "trapped call writers" in break_events[0].detail["thesis"]

    # 3. PCR Extreme triggers
    pcr_events = engine.check_pcr_extreme("NIFTY", pcr=0.62)
    assert len(pcr_events) == 1
    assert pcr_events[0].kind == EventKind.PCR_EXTREME
    assert pcr_events[0].detail["sentiment"] == "FEAR_OVERSOLD"

    # 4. Regime shift trigger
    regime_events = engine.check_regime_shift("NIFTY", current_regime="LONG_BUILDUP", prev_regime="NEUTRAL")
    assert len(regime_events) == 1
    assert regime_events[0].kind == EventKind.REGIME_SHIFT
    assert regime_events[0].detail["regime"] == "LONG_BUILDUP"


def test_backtest_slippage_and_wiggle():
    bte = BacktestEngine()
    candles = generate_synthetic_candles("NIFTY", days=3, seed=123)

    # Run backtest with slippage enabled
    result = bte.run("NIFTY", days=3, candles_by_day=candles, enable_slippage=True)
    assert result.total_trades >= 0
    assert "check_08_real_costs_included" in result.checklist_score
    assert "check_09_honest_slippage_included" in result.checklist_score
    assert result.checklist_score["check_09_honest_slippage_included"] is True

    # Parameter wiggle test (Check 17: Plateau vs Needle)
    wiggle = bte.parameter_wiggle_test("NIFTY", days=3, candles_by_day=candles)
    assert "verdict" in wiggle
    assert wiggle["verdict"] in ("PLATEAU", "NEEDLE")
    assert "baseline_pf" in wiggle
    assert "spread" in wiggle
    assert isinstance(wiggle["check_17_passed"], bool)

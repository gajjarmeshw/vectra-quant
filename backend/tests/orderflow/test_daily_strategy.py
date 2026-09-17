from __future__ import annotations

from datetime import datetime

from vectra_quant.orderflow.charts import FinalBias
from vectra_quant.orderflow.config import OrderFlowCfg
from vectra_quant.orderflow.daily_strategy import build_daily_structure, decide_daily_direction

CFG = OrderFlowCfg()
CUTOFF = datetime(2026, 9, 16, 14, 30)


def test_agreeing_bullish_charts_produce_bullish_bias():
    chart1 = [(datetime(2026, 9, 16, 9, 15), 100.0)]
    chart2 = [(datetime(2026, 9, 16, 9, 15), 10.0), (datetime(2026, 9, 16, 9, 16), 10.0)]
    decision = decide_daily_direction(chart1, chart2, CUTOFF, vix=None, cfg=CFG)
    assert decision.final_bias == FinalBias.BULLISH
    assert decision.used_fallback is False


def test_agreeing_bearish_charts_produce_bearish_bias():
    chart1 = [(datetime(2026, 9, 16, 9, 15), -100.0)]
    chart2 = [(datetime(2026, 9, 16, 9, 15), -10.0), (datetime(2026, 9, 16, 9, 16), -10.0)]
    decision = decide_daily_direction(chart1, chart2, CUTOFF, vix=None, cfg=CFG)
    assert decision.final_bias == FinalBias.BEARISH


def test_disagreeing_charts_fall_back_to_chart2_sign_when_always_enter():
    chart1 = [(datetime(2026, 9, 16, 9, 15), -100.0)]   # bearish
    chart2 = [(datetime(2026, 9, 16, 9, 15), 10.0), (datetime(2026, 9, 16, 9, 16), 10.0)]  # bullish
    decision = decide_daily_direction(chart1, chart2, CUTOFF, vix=None, cfg=CFG)
    assert decision.used_fallback is True
    assert decision.final_bias == FinalBias.BULLISH  # leaned on chart2's positive sign


def test_exact_tie_uses_configured_fallback_side():
    chart1 = [(datetime(2026, 9, 16, 9, 15), 0.0)]
    chart2 = [(datetime(2026, 9, 16, 9, 15), 0.0)]
    decision = decide_daily_direction(chart1, chart2, CUTOFF, vix=None, cfg=CFG, fallback_side=FinalBias.BEARISH)
    assert decision.used_fallback is True
    assert decision.final_bias == FinalBias.BEARISH


def test_low_vix_flips_weak_bullish_to_bearish():
    chart1 = [(datetime(2026, 9, 16, 9, 15), 100.0)]
    # tiny imbalance over a short span keeps the signed-area magnitude below
    # theta_conv (2.0), so the read counts as "weak" despite agreeing with chart1
    chart2 = [(datetime(2026, 9, 16, 9, 15), 0.01), (datetime(2026, 9, 16, 9, 16), 0.01)]
    decision = decide_daily_direction(chart1, chart2, CUTOFF, vix=10.0, cfg=CFG)
    assert decision.final_bias == FinalBias.BEARISH


def test_bearish_signals_never_flipped_by_vix_filter():
    chart1 = [(datetime(2026, 9, 16, 9, 15), -100.0)]
    chart2 = [(datetime(2026, 9, 16, 9, 15), -0.6), (datetime(2026, 9, 16, 9, 16), -0.6)]
    decision = decide_daily_direction(chart1, chart2, CUTOFF, vix=5.0, cfg=CFG)
    assert decision.final_bias == FinalBias.BEARISH


# --------------------------------------------------------------- structure

def test_bearish_structure_matches_disclosure_exactly():
    legs = build_daily_structure(FinalBias.BEARISH, spot=24623.0, cfg=CFG)
    assert len(legs) == 2
    leg1, leg2 = legs
    assert leg1.order == 1 and leg1.action == "BUY" and leg1.right == "CE" and leg1.strike == 24800.0
    assert leg2.order == 2 and leg2.action == "SELL" and leg2.right == "CE" and leg2.strike == 24600.0


def test_bullish_structure_matches_disclosure_exactly():
    legs = build_daily_structure(FinalBias.BULLISH, spot=24623.0, cfg=CFG)
    leg1, leg2 = legs
    assert leg1.order == 1 and leg1.action == "BUY" and leg1.right == "PE" and leg1.strike == 24400.0
    assert leg2.order == 2 and leg2.action == "SELL" and leg2.right == "PE" and leg2.strike == 24600.0


def test_skip_produces_no_legs():
    assert build_daily_structure(FinalBias.SKIP, spot=24623.0, cfg=CFG) == []


def test_atm_rounds_to_nearest_strike_step():
    legs = build_daily_structure(FinalBias.BEARISH, spot=24624.0, cfg=CFG)  # rounds to 24600, not 24650
    assert legs[1].strike == 24600.0

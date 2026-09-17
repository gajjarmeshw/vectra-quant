from __future__ import annotations

from vectra_quant.orderflow.config import ThunderboltCfg
from vectra_quant.orderflow.thunderbolt_signal import SignalDirection
from vectra_quant.orderflow.thunderbolt_strategy import (
    build_thunderbolt_structure,
    max_loss,
    net_debit,
    should_skip_day,
)

CFG = ThunderboltCfg()


def test_tuesday_is_skipped():
    skip, reason = should_skip_day(weekday=1, prior_session_vix=15.0, cfg=CFG)
    assert skip is True
    assert reason == "TUESDAY_SKIP"


def test_high_vix_is_skipped():
    skip, reason = should_skip_day(weekday=2, prior_session_vix=22.0, cfg=CFG)
    assert skip is True
    assert "HIGH_VIX" in reason


def test_normal_day_not_skipped():
    skip, _ = should_skip_day(weekday=2, prior_session_vix=15.0, cfg=CFG)
    assert skip is False


def test_missing_vix_does_not_skip():
    skip, _ = should_skip_day(weekday=2, prior_session_vix=None, cfg=CFG)
    assert skip is False


def test_bullish_structure_matches_disclosure():
    legs = build_thunderbolt_structure(SignalDirection.BULLISH, spot=24623.0, cfg=CFG)
    assert len(legs) == 2
    long_leg, short_leg = legs
    assert long_leg.order == 1 and long_leg.action == "BUY" and long_leg.right == "CE"
    assert long_leg.strike == 24700.0 and long_leg.lots == 2
    assert short_leg.order == 2 and short_leg.action == "SELL" and short_leg.right == "CE"
    assert short_leg.strike == 24600.0 and short_leg.lots == 1


def test_bearish_structure_matches_disclosure():
    legs = build_thunderbolt_structure(SignalDirection.BEARISH, spot=24623.0, cfg=CFG)
    long_leg, short_leg = legs
    assert long_leg.action == "BUY" and long_leg.right == "PE" and long_leg.strike == 24500.0 and long_leg.lots == 2
    assert short_leg.action == "SELL" and short_leg.right == "PE" and short_leg.strike == 24600.0 and short_leg.lots == 1


def test_skip_direction_produces_no_legs():
    assert build_thunderbolt_structure(SignalDirection.SKIP, spot=24623.0, cfg=CFG) == []


def test_net_debit_positive_when_longs_cost_more():
    d = net_debit(long_premium=50.0, short_premium=60.0, cfg=CFG, lot_size=65)
    # 2*50 - 60 = 40 points * 65 lot size
    assert d == 40.0 * 65


def test_net_debit_can_be_negative_net_credit_day():
    d = net_debit(long_premium=20.0, short_premium=60.0, cfg=CFG, lot_size=65)
    # 2*20 - 60 = -20 points * 65
    assert d == -20.0 * 65


def test_max_loss_matches_disclosure_formula():
    d = net_debit(long_premium=50.0, short_premium=60.0, cfg=CFG, lot_size=65)  # 2600
    loss = max_loss(d, CFG, lot_size=65)
    assert loss == 100.0 * 65 + 2600


def test_max_loss_reduced_on_net_credit_day():
    d = net_debit(long_premium=20.0, short_premium=60.0, cfg=CFG, lot_size=65)  # -1300
    loss = max_loss(d, CFG, lot_size=65)
    assert loss == 100.0 * 65 - 1300

from __future__ import annotations

from datetime import datetime, time

import pytest

from vectra_quant.orderflow.config import ThunderboltCfg
from vectra_quant.orderflow.thunderbolt_signal import (
    Regime,
    SignalDirection,
    ThunderboltInputs,
    apply_reversal_check,
    classify_regime,
    detect_crossing,
    detect_swing_breakout,
    evaluate_thunderbolt,
    liquidity_reversal_inversion,
    medium_regime_inversion,
    opposite_side_gate,
    over_stretch_veto,
    pre_open_lock,
)

CFG = ThunderboltCfg()


def t(h: int, m: int) -> datetime:
    return datetime(2026, 9, 16, h, m)


# --------------------------------------------------------------- regime

def test_classify_regime_low_medium_high():
    cfg = ThunderboltCfg(regime_low_max=0.6, regime_high_min=1.2)
    assert classify_regime([0.1, 0.2, 0.3], cfg) == Regime.LOW
    assert classify_regime([0.8, 0.9], cfg) == Regime.MEDIUM
    assert classify_regime([1.5, 1.6], cfg) == Regime.HIGH


def test_classify_regime_falls_back_conservatively_when_empty():
    cfg = ThunderboltCfg(regime_fallback="HIGH")
    assert classify_regime([], cfg) == Regime.HIGH


def test_classify_regime_uses_only_lookback_window():
    cfg = ThunderboltCfg(regime_low_max=0.6, regime_high_min=1.2, regime_lookback_sessions=2)
    # last 2 values (1.5, 1.6) average HIGH, despite earlier LOW values
    assert classify_regime([0.1, 0.1, 1.5, 1.6], cfg) == Regime.HIGH


# --------------------------------------------------------------- crossing + reversal

def test_detect_crossing_upward():
    series = [(t(9, 16), 0.05), (t(9, 17), 0.10), (t(9, 18), 0.20)]
    trigger = detect_crossing(series, theta_cross=0.15, after=time(9, 16))
    assert trigger is not None
    assert trigger.direction == SignalDirection.BULLISH
    assert trigger.ts == t(9, 18)


def test_detect_crossing_downward():
    series = [(t(9, 16), -0.05), (t(9, 17), -0.10), (t(9, 18), -0.20)]
    trigger = detect_crossing(series, theta_cross=0.15, after=time(9, 16))
    assert trigger.direction == SignalDirection.BEARISH


def test_detect_crossing_ignores_points_before_after_time():
    # a crossing that happens before 9:16 must not fire
    series = [(t(9, 10), 0.05), (t(9, 12), 0.30), (t(9, 20), 0.31)]
    trigger = detect_crossing(series, theta_cross=0.15, after=time(9, 16))
    assert trigger is None


def test_detect_crossing_returns_none_when_no_crossing():
    series = [(t(9, 16), 0.01), (t(9, 17), 0.02), (t(9, 18), 0.03)]
    assert detect_crossing(series, theta_cross=0.15, after=time(9, 16)) is None


def test_reversal_check_flips_when_opposite_dominated():
    from vectra_quant.orderflow.thunderbolt_signal import TriggerEvent
    trigger = TriggerEvent(ts=t(9, 30), direction=SignalDirection.BULLISH, value=0.16, source="CROSSING")
    series_before = [(t(9, 20), -0.5)]  # opposite (bearish) extreme dominates the 0.16 crossing
    flipped_trigger, flipped = apply_reversal_check(trigger, series_before)
    assert flipped is True
    assert flipped_trigger.direction == SignalDirection.BEARISH


def test_reversal_check_no_flip_when_crossing_dominates():
    from vectra_quant.orderflow.thunderbolt_signal import TriggerEvent
    trigger = TriggerEvent(ts=t(9, 30), direction=SignalDirection.BULLISH, value=0.5, source="CROSSING")
    series_before = [(t(9, 20), -0.1)]
    _, flipped = apply_reversal_check(trigger, series_before)
    assert flipped is False


# --------------------------------------------------------------- swing/breakout

def test_swing_breakout_fires_bullish_after_confirmed_retracement():
    cfg = ThunderboltCfg(swing_retracement_pct=0.3, swing_breakout_margin=0.02, swing_min_abs_reading=0.1)
    series = [
        (t(9, 16), 0.10),
        (t(9, 20), 0.30),   # swing high forms
        (t(9, 25), 0.15),   # retraces >=30% of 0.30 -> confirms swing_high=0.30
        (t(9, 30), 0.33),   # breaks 0.30 + 0.02 margin -> breakout
    ]
    trigger = detect_swing_breakout(series, cfg)
    assert trigger is not None
    assert trigger.direction == SignalDirection.BULLISH


def test_swing_breakout_vetoed_when_early_extreme_not_exceeded():
    cfg = ThunderboltCfg(swing_retracement_pct=0.3, swing_breakout_margin=0.02, swing_min_abs_reading=0.1)
    series = [
        (t(9, 16), 0.50),   # huge early extreme, sets a high bar
        (t(9, 20), 0.30),
        (t(9, 25), 0.10),   # retraces, confirms swing_high~0.30
        (t(9, 30), 0.33),   # breaks confirmed high but still well below the session's own 0.50 extreme
    ]
    trigger = detect_swing_breakout(series, cfg)
    assert trigger is None


def test_swing_breakout_none_with_too_short_series():
    assert detect_swing_breakout([(t(9, 16), 0.1)], CFG) is None


# --------------------------------------------------------------- filters

def test_opposite_side_gate_skips_when_signal_does_not_dominate():
    cfg = ThunderboltCfg(opposite_gate_floor=0.1, opposite_gate_ratio=1.5)
    assert opposite_side_gate(SignalDirection.BULLISH, signal_value=0.2, opposite_strength=0.3, cfg=cfg) is True


def test_opposite_side_gate_passes_when_opposite_below_floor():
    cfg = ThunderboltCfg(opposite_gate_floor=0.1, opposite_gate_ratio=1.5)
    assert opposite_side_gate(SignalDirection.BULLISH, signal_value=0.2, opposite_strength=0.05, cfg=cfg) is False


def test_opposite_side_gate_passes_when_signal_dominates():
    cfg = ThunderboltCfg(opposite_gate_floor=0.1, opposite_gate_ratio=1.5)
    assert opposite_side_gate(SignalDirection.BULLISH, signal_value=0.5, opposite_strength=0.2, cfg=cfg) is False


def test_pre_open_lock_blocks_opposite_direction_only():
    cfg = ThunderboltCfg(pre_open_lock_threshold=0.3)
    # strong pre-open BID (bullish-leaning) extreme locks out BEARISH signals
    assert pre_open_lock(SignalDirection.BEARISH, pre_open_bid_extreme=0.4, pre_open_ask_extreme=0.0, cfg=cfg) is True
    assert pre_open_lock(SignalDirection.BULLISH, pre_open_bid_extreme=0.4, pre_open_ask_extreme=0.0, cfg=cfg) is False


def test_pre_open_lock_both_sides_locks_entirely():
    cfg = ThunderboltCfg(pre_open_lock_threshold=0.3)
    assert pre_open_lock(SignalDirection.BULLISH, pre_open_bid_extreme=0.4, pre_open_ask_extreme=0.4, cfg=cfg) is True
    assert pre_open_lock(SignalDirection.BEARISH, pre_open_bid_extreme=0.4, pre_open_ask_extreme=0.4, cfg=cfg) is True


def test_liquidity_reversal_inversion_fires_when_early_side_still_dominant():
    cfg = ThunderboltCfg(liquidity_reversal_early_threshold=0.3, liquidity_reversal_push_threshold=0.15)
    assert liquidity_reversal_inversion(SignalDirection.BULLISH, early_extreme=0.5, opposite_push=0.2, cfg=cfg) is True


def test_liquidity_reversal_inversion_does_not_fire_if_opposite_push_overtakes():
    cfg = ThunderboltCfg(liquidity_reversal_early_threshold=0.3, liquidity_reversal_push_threshold=0.15)
    assert liquidity_reversal_inversion(SignalDirection.BULLISH, early_extreme=0.5, opposite_push=0.6, cfg=cfg) is False


def test_medium_regime_inversion_only_in_medium_regime():
    cfg = ThunderboltCfg()
    assert medium_regime_inversion(Regime.HIGH, value_at_crossing=0.5, value_now=0.1, cfg=cfg) is False
    assert medium_regime_inversion(Regime.MEDIUM, value_at_crossing=0.5, value_now=0.1, cfg=cfg) is True  # faded 80%
    assert medium_regime_inversion(Regime.MEDIUM, value_at_crossing=0.5, value_now=0.5, cfg=cfg) is False  # no fade


def test_over_stretch_veto():
    cfg = ThunderboltCfg(over_stretch_bound=0.6)
    assert over_stretch_veto(0.65, cfg) is True
    assert over_stretch_veto(-0.65, cfg) is True
    assert over_stretch_veto(0.4, cfg) is False


# --------------------------------------------------------------- full pipeline

def test_evaluate_thunderbolt_clean_bullish_signal():
    cfg = ThunderboltCfg(regime_high_min=1.2, theta_cross=0.15, over_stretch_bound=0.6)
    series = [(t(9, 16), 0.02), (t(9, 20), 0.10), (t(9, 25), 0.20)]
    inputs = ThunderboltInputs(series=series, recent_realized_vols=[1.5, 1.6])
    trace = evaluate_thunderbolt(inputs, cfg)
    assert trace.regime == Regime.HIGH
    assert trace.final == SignalDirection.BULLISH
    assert trace.over_stretch_skip is False


def test_evaluate_thunderbolt_over_stretch_vetoes_extreme_crossing():
    cfg = ThunderboltCfg(regime_high_min=1.2, theta_cross=0.15, over_stretch_bound=0.5)
    series = [(t(9, 16), 0.02), (t(9, 20), 0.10), (t(9, 25), 0.90)]  # jumps straight past the stretch bound
    inputs = ThunderboltInputs(series=series, recent_realized_vols=[1.5])
    trace = evaluate_thunderbolt(inputs, cfg)
    assert trace.final == SignalDirection.SKIP
    assert trace.over_stretch_skip is True


def test_evaluate_thunderbolt_no_trigger_skips():
    cfg = ThunderboltCfg(regime_high_min=1.2, theta_cross=0.5)
    series = [(t(9, 16), 0.02), (t(9, 20), 0.05), (t(9, 25), 0.08)]
    inputs = ThunderboltInputs(series=series, recent_realized_vols=[1.5])
    trace = evaluate_thunderbolt(inputs, cfg)
    assert trace.final == SignalDirection.SKIP
    assert trace.trigger is None


def test_evaluate_thunderbolt_low_regime_uses_swing_detector():
    cfg = ThunderboltCfg(regime_low_max=0.6, swing_retracement_pct=0.3, swing_breakout_margin=0.02, swing_min_abs_reading=0.1)
    series = [(t(9, 16), 0.10), (t(9, 20), 0.30), (t(9, 25), 0.15), (t(9, 30), 0.33)]
    inputs = ThunderboltInputs(series=series, recent_realized_vols=[0.1, 0.2])
    trace = evaluate_thunderbolt(inputs, cfg)
    assert trace.regime == Regime.LOW
    assert trace.trigger is not None
    assert trace.trigger.source == "SWING_BREAKOUT"

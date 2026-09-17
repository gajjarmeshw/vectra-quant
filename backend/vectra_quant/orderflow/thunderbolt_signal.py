"""Pure signal logic for hedged121 ("Nifty Thunderbolt") -- an intraday 1x2
ratio backspread driven by the INSTANTANEOUS order-book imbalance series
(not the cumulative Chart1/Chart2 of the weekly hedged131 strategy).

Every threshold and every filter's exact definition is undisclosed
("proprietary calibration") in the white-box disclosure -- what follows is
a best-effort, clearly-labeled RECONSTRUCTION of the described behavior
shape (crossing + reversal-check; swing/retracement breakout; a 5-stage
filter chain; over-stretch veto), not a replication of the real production
rules. Treat every numeric threshold in `config.ThunderboltCfg` as a guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum

from vectra_quant.orderflow.config import ThunderboltCfg


class Regime(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SignalDirection(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SKIP = "SKIP"


@dataclass(frozen=True)
class TriggerEvent:
    ts: datetime
    direction: SignalDirection
    value: float
    source: str   # "CROSSING" or "SWING_BREAKOUT"


@dataclass
class DecisionTrace:
    """Audit trail -- which stage produced/changed the decision, per the
    disclosure's own "explainability artifacts" intent (section 13)."""
    regime: Regime
    trigger: TriggerEvent | None = None
    reversal_flip: bool = False
    opposite_gate_skip: bool = False
    pre_open_lock_skip: bool = False
    liquidity_reversal_flip: bool = False
    medium_regime_flip: bool = False
    over_stretch_skip: bool = False
    final: SignalDirection = SignalDirection.SKIP


# --------------------------------------------------------------- regime classification

def classify_regime(recent_realized_vols: list[float], cfg: ThunderboltCfg) -> Regime:
    """Average of the last `regime_lookback_sessions` realized-vol readings.
    Missing/empty data falls back conservatively per `cfg.regime_fallback`."""
    if not recent_realized_vols:
        return Regime[cfg.regime_fallback]
    window = recent_realized_vols[-cfg.regime_lookback_sessions:]
    avg = sum(window) / len(window)
    if avg < cfg.regime_low_max:
        return Regime.LOW
    if avg >= cfg.regime_high_min:
        return Regime.HIGH
    return Regime.MEDIUM


# --------------------------------------------------------------- MEDIUM/HIGH: crossing + reversal

def detect_crossing(series: list[tuple[datetime, float]], theta_cross: float, after: time) -> TriggerEvent | None:
    """First transition of the raw series through +-theta_cross at/after `after`."""
    for (t_prev, v_prev), (t_curr, v_curr) in zip(series, series[1:]):
        if t_curr.time() < after:
            continue
        if v_prev < theta_cross <= v_curr:
            return TriggerEvent(ts=t_curr, direction=SignalDirection.BULLISH, value=v_curr, source="CROSSING")
        if v_prev > -theta_cross >= v_curr:
            return TriggerEvent(ts=t_curr, direction=SignalDirection.BEARISH, value=v_curr, source="CROSSING")
    return None


def apply_reversal_check(trigger: TriggerEvent, series_before: list[tuple[datetime, float]]) -> tuple[TriggerEvent, bool]:
    """If the OPPOSITE side's strongest reading before the crossing exceeds
    the crossing's own magnitude, read the crossing as that other move's
    exhaustion and flip direction. Returns (possibly-flipped trigger, flipped?)."""
    if not series_before:
        return trigger, False
    if trigger.direction == SignalDirection.BULLISH:
        opposite_extreme = min((v for _, v in series_before), default=0.0)  # most negative
    else:
        opposite_extreme = max((v for _, v in series_before), default=0.0)  # most positive

    if abs(opposite_extreme) > abs(trigger.value):
        flipped_dir = SignalDirection.BEARISH if trigger.direction == SignalDirection.BULLISH else SignalDirection.BULLISH
        return TriggerEvent(ts=trigger.ts, direction=flipped_dir, value=trigger.value, source=trigger.source), True
    return trigger, False


# --------------------------------------------------------------- LOW: swing/breakout

def detect_swing_breakout(series: list[tuple[datetime, float]], cfg: ThunderboltCfg) -> TriggerEvent | None:
    """Confirm swing extremes once retraced by `swing_retracement_pct` of
    their own magnitude, then fire on a breakout past the last confirmed
    extreme by `swing_breakout_margin` -- vetoed if the session's own
    early high-water mark already exceeds the breakout reading (a move
    that merely re-tests the open's own extreme is not a breakout)."""
    if len(series) < 2:
        return None

    swing_high = series[0][1]
    swing_low = series[0][1]
    confirmed_high: float | None = None
    confirmed_low: float | None = None
    early_extreme = abs(series[0][1])

    for ts, val in series[1:]:
        if val > swing_high:
            swing_high = val
        if val < swing_low:
            swing_low = val

        if swing_high > 0 and (swing_high - val) >= cfg.swing_retracement_pct * abs(swing_high):
            confirmed_high = swing_high
        if swing_low < 0 and (val - swing_low) >= cfg.swing_retracement_pct * abs(swing_low):
            confirmed_low = swing_low

        if (confirmed_high is not None and val >= confirmed_high + cfg.swing_breakout_margin
                and abs(val) >= cfg.swing_min_abs_reading and val >= early_extreme):
            return TriggerEvent(ts=ts, direction=SignalDirection.BULLISH, value=val, source="SWING_BREAKOUT")
        if (confirmed_low is not None and val <= confirmed_low - cfg.swing_breakout_margin
                and abs(val) >= cfg.swing_min_abs_reading and abs(val) >= early_extreme):
            return TriggerEvent(ts=ts, direction=SignalDirection.BEARISH, value=val, source="SWING_BREAKOUT")

        early_extreme = max(early_extreme, abs(val))

    return None


# --------------------------------------------------------------- filter chain

def opposite_side_gate(direction: SignalDirection, signal_value: float, opposite_strength: float, cfg: ThunderboltCfg) -> bool:
    """True = SKIP. Opposite strength must clear the floor to matter at all,
    then the signal must dominate it by `opposite_gate_ratio`."""
    if opposite_strength < cfg.opposite_gate_floor:
        return False
    return abs(signal_value) < cfg.opposite_gate_ratio * opposite_strength


def pre_open_lock(direction: SignalDirection, pre_open_bid_extreme: float, pre_open_ask_extreme: float, cfg: ThunderboltCfg) -> bool:
    """True = SKIP. An extreme pre-open reading on one side locks out
    signals against it; extremes on both sides lock the day entirely."""
    bid_locked = pre_open_bid_extreme >= cfg.pre_open_lock_threshold
    ask_locked = pre_open_ask_extreme >= cfg.pre_open_lock_threshold
    if bid_locked and ask_locked:
        return True
    if bid_locked and direction == SignalDirection.BEARISH:
        return True
    if ask_locked and direction == SignalDirection.BULLISH:
        return True
    return False


def liquidity_reversal_inversion(direction: SignalDirection, early_extreme: float, opposite_push: float, cfg: ThunderboltCfg) -> bool:
    """True = INVERT. Heavy early one-way liquidity answered by a
    meaningful (but still subordinate) opposite-side push after the open."""
    return (early_extreme >= cfg.liquidity_reversal_early_threshold
            and cfg.liquidity_reversal_push_threshold <= opposite_push < early_extreme)


def medium_regime_inversion(regime: Regime, value_at_crossing: float, value_now: float, cfg: ThunderboltCfg) -> bool:
    """True = INVERT. Placeholder reconstruction: only in MEDIUM regime, a
    reading that has already faded meaningfully since its own crossing is
    read as exhausting rather than starting a move. The disclosure names
    this filter but not its definition -- this is a best guess at the
    described shape, not the real rule."""
    if regime != Regime.MEDIUM or value_at_crossing == 0:
        return False
    faded_fraction = 1.0 - (abs(value_now) / abs(value_at_crossing))
    return faded_fraction >= 0.5


def over_stretch_veto(value: float, cfg: ThunderboltCfg) -> bool:
    """True = SKIP. Final gate: a reading already at/beyond the stretch
    bound is read as a crescendo, not a start."""
    return abs(value) >= cfg.over_stretch_bound


@dataclass
class ThunderboltInputs:
    series: list[tuple[datetime, float]]          # raw instantaneous ir(i), whole session so far
    recent_realized_vols: list[float]
    pre_open_bid_extreme: float = 0.0
    pre_open_ask_extreme: float = 0.0


def evaluate_thunderbolt(inputs: ThunderboltInputs, cfg: ThunderboltCfg) -> DecisionTrace:
    regime = classify_regime(inputs.recent_realized_vols, cfg)
    trace = DecisionTrace(regime=regime)

    if regime == Regime.LOW:
        trigger = detect_swing_breakout(inputs.series, cfg)
    else:
        trigger = detect_crossing(inputs.series, cfg.theta_cross, cfg.signal_window_start)
        if trigger is not None:
            series_before = [p for p in inputs.series if p[0] < trigger.ts]
            trigger, flipped = apply_reversal_check(trigger, series_before)
            trace.reversal_flip = flipped

    trace.trigger = trigger
    if trigger is None:
        trace.final = SignalDirection.SKIP
        return trace

    direction = trigger.direction
    series_before_signal = [p for p in inputs.series if p[0] < trigger.ts]
    same_side = [v for _, v in series_before_signal if (v > 0) == (direction == SignalDirection.BULLISH)]
    opposite_side = [v for _, v in series_before_signal if (v > 0) != (direction == SignalDirection.BULLISH)]
    opposite_strength = max((abs(v) for v in opposite_side), default=0.0)
    early_extreme = max((abs(v) for _, v in series_before_signal), default=0.0)

    if opposite_side_gate(direction, trigger.value, opposite_strength, cfg):
        trace.opposite_gate_skip = True
        trace.final = SignalDirection.SKIP
        return trace

    if pre_open_lock(direction, inputs.pre_open_bid_extreme, inputs.pre_open_ask_extreme, cfg):
        trace.pre_open_lock_skip = True
        trace.final = SignalDirection.SKIP
        return trace

    if liquidity_reversal_inversion(direction, early_extreme, opposite_strength, cfg):
        trace.liquidity_reversal_flip = True
        direction = SignalDirection.BEARISH if direction == SignalDirection.BULLISH else SignalDirection.BULLISH

    latest_value = inputs.series[-1][1] if inputs.series else trigger.value
    if medium_regime_inversion(regime, trigger.value, latest_value, cfg):
        trace.medium_regime_flip = True
        direction = SignalDirection.BEARISH if direction == SignalDirection.BULLISH else SignalDirection.BULLISH

    if over_stretch_veto(trigger.value, cfg):
        trace.over_stretch_skip = True
        trace.final = SignalDirection.SKIP
        return trace

    trace.final = direction
    return trace

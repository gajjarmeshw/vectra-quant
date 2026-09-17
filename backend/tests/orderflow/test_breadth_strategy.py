from __future__ import annotations

from vectra_quant.orderflow.breadth_signal import SignalDirection
from vectra_quant.orderflow.breadth_strategy import build_breadth_structure, exit_reason
from vectra_quant.orderflow.config import BreadthCfg

CFG = BreadthCfg()


def test_skip_direction_produces_no_legs():
    assert build_breadth_structure(SignalDirection.SKIP, spot=23280, cfg=CFG) == []


def test_bullish_buys_atm_call():
    legs = build_breadth_structure(SignalDirection.BULLISH, spot=23280, cfg=CFG)
    assert len(legs) == 1
    assert legs[0].action == "BUY"
    assert legs[0].right == "CE"
    assert legs[0].strike == 23300  # nearest 50-step to 23280


def test_bearish_buys_atm_put():
    legs = build_breadth_structure(SignalDirection.BEARISH, spot=23280, cfg=CFG)
    assert legs[0].right == "PE"
    assert legs[0].strike == 23300


def test_exit_reason_stop_loss():
    cfg = BreadthCfg(stop_loss_pct_premium=0.3, target_pct_premium=0.5, max_hold_minutes=15)
    assert exit_reason(entry_premium=100, current_premium=69, entry_ts_minutes_ago=1, cfg=cfg) == "STOP_LOSS"


def test_exit_reason_target():
    cfg = BreadthCfg(stop_loss_pct_premium=0.3, target_pct_premium=0.5, max_hold_minutes=15)
    assert exit_reason(entry_premium=100, current_premium=151, entry_ts_minutes_ago=1, cfg=cfg) == "TARGET"


def test_exit_reason_max_hold_time():
    cfg = BreadthCfg(stop_loss_pct_premium=0.3, target_pct_premium=0.5, max_hold_minutes=15)
    assert exit_reason(entry_premium=100, current_premium=105, entry_ts_minutes_ago=15, cfg=cfg) == "MAX_HOLD_TIME"


def test_exit_reason_none_when_nothing_triggered():
    cfg = BreadthCfg(stop_loss_pct_premium=0.3, target_pct_premium=0.5, max_hold_minutes=15)
    assert exit_reason(entry_premium=100, current_premium=105, entry_ts_minutes_ago=5, cfg=cfg) is None

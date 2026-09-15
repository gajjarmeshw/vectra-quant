"""Executable spec for the VECTRA_QUANT risk engine (design doc §4.2, user-amended)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime

from vectra_quant.risk_engine import (
    DayState,
    RejectReason,
    RiskConfig,
    RiskEngine,
    TradeResult,
    size_position,
)

CFG = RiskConfig(             # explicit values these tests were written for
    capital=15_000,
    target_pct=2_500 / 15_000,          # 16.66%
    loss_limit_pct=1_050 / 15_000,      # 7.00%
    risk_per_trade_pct=1_200 / 15_000,  # 8.00%
    base_trades=3,
)
T = lambda h, m: datetime(2026, 8, 4, h, m)


def engine():
    return RiskEngine(CFG)


# ---------------------------------------------------------------- windows
def test_entry_window():
    e = engine()
    assert not e.can_enter(T(9, 19)).allowed            # before 09:20
    assert e.can_enter(T(9, 20)).allowed
    assert e.can_enter(T(14, 59)).allowed
    assert e.can_enter(T(15, 0)).reason == RejectReason.OUTSIDE_WINDOW

def test_expiry_cutoff_and_half_risk():
    e = engine()
    d = e.can_enter(T(14, 0), is_expiry_day=True)
    assert d.allowed and d.max_risk == 600              # r/2 on expiry
    assert e.can_enter(T(14, 30), is_expiry_day=True).reason == RejectReason.EXPIRY_CUTOFF

def test_squareoff_clock_locks():
    e = engine()
    ev = e.on_clock(T(15, 10))
    assert ev.square_off and e.state == DayState.LOCKED


# ---------------------------------------------------------------- caps & locks
def test_trade_cap_base_3():
    e = engine()
    for _ in range(3):
        assert e.can_enter(T(10, 0)).allowed
        e.on_trade_opened()
        e.on_trade_closed(TradeResult(+100))
    assert e.can_enter(T(11, 0)).reason == RejectReason.TRADE_CAP

def test_two_consecutive_losses_lock():
    e = engine()
    e.on_trade_opened(); e.on_trade_closed(TradeResult(-300))
    assert e.state != DayState.LOCKED
    e.on_trade_opened(); ev = e.on_trade_closed(TradeResult(-300))
    assert ev.lock and e.state == DayState.LOCKED
    assert "consecutive" in e.lock_reason

def test_win_resets_loss_streak():
    e = engine()
    e.on_trade_opened(); e.on_trade_closed(TradeResult(-300))
    e.on_trade_opened(); e.on_trade_closed(TradeResult(+200))
    e.on_trade_opened(); e.on_trade_closed(TradeResult(-300))
    assert e.state != DayState.LOCKED                    # streak was broken

def test_daily_loss_limit_lock_at_1050():
    e = engine()
    e.on_trade_opened()
    ev = e.on_pnl_tick(-1050)                            # unrealized counts
    assert ev.square_off and e.state == DayState.LOCKED

def test_kill_switch_blocks_everything():
    e = RiskEngine(CFG, kill_switch_on=False)
    assert e.can_enter(T(10, 0)).reason == RejectReason.KILL_SWITCH


# ---------------------------------------------------------------- EARNED (45% -> 4th trade, keep-70 floor)
def test_bonus_trade_and_keep70_floor():
    e = engine()
    e.on_trade_opened()
    e.on_trade_closed(TradeResult(+1500))                # 60% of T
    assert e.state == DayState.EARNED
    assert e.trade_cap == 4                              # 3 + earned 1
    assert e.floor == 1500 * 0.70                        # keep 70 -> 1050

def test_earned_floor_ratchets_with_new_highs():
    e = engine()
    e.on_trade_opened(); e.on_trade_closed(TradeResult(+1200))   # 48% -> EARNED
    f1 = e.floor
    e.on_trade_opened(); e.on_pnl_tick(+600)             # dayPnL 1800
    assert e.floor == max(f1, 0.70 * 1800)

def test_earned_floor_breach_locks_green():
    e = engine()
    e.on_trade_opened(); e.on_trade_closed(TradeResult(+1500))   # floor 1050
    e.on_trade_opened()
    ev = e.on_pnl_tick(-450)                             # dayPnL 1050 == floor
    assert ev.lock and e.state == DayState.LOCKED
    assert e.day_pnl == 1050                             # day ends green, banked


# ---------------------------------------------------------------- PROTECT / TRAIL
def test_protect_at_target():
    e = engine()
    e.on_trade_opened(); e.on_trade_closed(TradeResult(+2500))
    assert e.state == DayState.PROTECT
    assert e.floor >= 0.5 * CFG.target                   # >= 1250
    d = e.can_enter(T(11, 0), confidence=85)
    assert d.allowed and d.max_risk == 600               # r/2
    assert e.can_enter(T(11, 0), confidence=75).reason == RejectReason.CONFIDENCE

def test_trail_25pct_giveback_ratchet():
    e = engine()
    e.on_trade_opened(); e.on_trade_closed(TradeResult(+2500))   # PROTECT
    e.on_trade_opened()
    e.on_pnl_tick(+750)                                  # dayPnL 3250 = 1.3T
    assert e.state == DayState.TRAIL
    assert e.floor == 0.75 * 3250
    e.on_pnl_tick(+1500)                                 # peak 4000
    assert e.floor == 0.75 * 4000                        # ratcheted to 3000
    ev = e.on_pnl_tick(+500)                             # dayPnL 3000 <= floor
    assert ev.lock and e.day_pnl == 3000                 # kept 75% of peak

def test_worked_example_from_doc_user_numbers():
    """T=2500: +1500 -> EARNED f=1050; run to 2500 -> PROTECT; 3250 -> TRAIL;
    peak 4000 -> floor 3000; pullback locks at 3000."""
    e = engine()
    e.on_trade_opened(); e.on_trade_closed(TradeResult(+1500))
    assert (e.state, e.floor) == (DayState.EARNED, 1050)
    e.on_trade_opened(); e.on_pnl_tick(+1000)            # 2500
    assert e.state == DayState.PROTECT
    e.on_pnl_tick(+1750)                                 # 3250
    assert e.state == DayState.TRAIL
    e.on_pnl_tick(+2500)                                 # 4000 peak
    ev = e.on_pnl_tick(+1400)                            # 2900 < 3000 floor
    assert ev.lock and e.state == DayState.LOCKED


# ---------------------------------------------------------------- guardian & sizing
def test_guardian_default_stops():
    e = engine()
    assert e.default_stop_premium("NIFTY", 141.0) == 129.0    # -12 pts
    assert e.default_stop_premium("SENSEX", 185.0) == 150.0   # -35 pts

def test_sizing_never_hardcodes_lots_and_allows_min_1():
    s = size_position(max_risk=1200, sl_points_premium=12, lot_size=65,
                      premium=141, capital=15000)
    assert s["lots"] == 1 and s["risk"] == 780 and not s["over_risk"]
    # 1-lot risk exceeds r -> user rule: still allow, but flag it
    s2 = size_position(max_risk=600, sl_points_premium=12, lot_size=65,
                       premium=141, capital=15000)
    assert s2["lots"] == 1 and s2["over_risk"]
    # bigger capital, same engine: multi-lot emerges naturally
    s3 = size_position(max_risk=6000, sl_points_premium=12, lot_size=65,
                       premium=141, capital=150000)
    assert s3["lots"] == 7


def test_no_state_leaks_between_days():
    e = engine()
    e.on_trade_opened(); e.on_trade_closed(TradeResult(-600))
    e.on_trade_opened(); e.on_trade_closed(TradeResult(-600))
    assert e.state == DayState.LOCKED
    e.reset_day()
    assert e.state == DayState.NORMAL and e.trades_taken == 0
    assert e.floor == -CFG.loss_limit


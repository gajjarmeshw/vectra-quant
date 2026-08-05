"""Orchestrator: FSM wiring, crash recovery, weekly governor.

The crash-recovery test is the important one. A restart that silently refunds the
loss limit is the worst bug this system could have.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sentinel.core.orchestrator import Orchestrator
from sentinel.core.session_clock import session_date
from sentinel.db import Trade, Violation, session, utcnow
from sentinel.risk_engine import DayState


def _closed_trade(pnl: float, *, when=None, sdate: str | None = None) -> None:
    with session() as s:
        s.add(Trade(
            id=str(uuid.uuid4()),
            opened_at=when or utcnow(),
            closed_at=when or utcnow(),
            session_date=sdate or session_date(),
            origin="ai", trading_symbol="NIFTY2681124250CE",
            lots=1, lot_size=65, qty=65,
            entry_price=141.0, exit_price=141.0 + pnl / 65,
            pnl=pnl, status="CLOSED",
        ))


def _open_trade(sdate: str | None = None) -> str:
    tid = str(uuid.uuid4())
    with session() as s:
        s.add(Trade(
            id=tid, opened_at=utcnow(), session_date=sdate or session_date(),
            origin="ai", trading_symbol="NIFTY2681124250CE",
            lots=1, lot_size=65, qty=65, entry_price=141.0, status="OPEN",
        ))
    return tid


# ---------------------------------------------------------------- basic wiring

def test_pnl_tick_drives_state_and_persists_curve(cfg):
    o = Orchestrator(cfg)
    o.on_trade_opened()
    snap = o.on_pnl_tick(500.0)
    assert snap.day_pnl == 500.0
    from sentinel.db import PnlCurve
    with session() as s:
        assert s.query(PnlCurve).count() >= 1


def test_net_pnl_feeds_fsm_not_gross(cfg):
    """FSM must trade on money kept, so callers pass net. 1500 net -> EARNED."""
    o = Orchestrator(cfg)
    o.on_trade_opened()
    snap = o.on_trade_closed(1500.0)
    assert snap.state == DayState.EARNED.value
    assert snap.floor == 1500 * 0.70


def test_loss_limit_lock_emits_square_off(cfg):
    events = []
    o = Orchestrator(cfg, on_engine_event=lambda ev, sn: events.append(ev))
    o.on_trade_opened()
    snap = o.on_pnl_tick(-1050.0)
    assert snap.state == DayState.LOCKED.value
    assert events and events[0].square_off


def test_state_change_hook_fires_once_per_change(cfg):
    changes = []
    o = Orchestrator(cfg, on_state_change=lambda sn: changes.append(sn.state))
    o.on_trade_opened()
    o.on_pnl_tick(100.0)
    o.on_pnl_tick(120.0)          # no state or floor change
    o.on_trade_closed(1500.0)     # -> EARNED
    assert changes.count(DayState.EARNED.value) == 1


def test_expiry_day_does_not_reduce_risk(cfg):
    """D-009: expiry only moves the entry cutoff."""
    o = Orchestrator(cfg)
    normal = o.can_enter(confidence=75, when=datetime(2026, 8, 6, 11, 0))
    expiry = o.can_enter(confidence=75, is_expiry_day=True,
                         when=datetime(2026, 8, 6, 11, 0))
    assert normal.max_risk == expiry.max_risk == cfg.risk_per_trade


def test_expiry_cutoff_still_applies(cfg):
    from sentinel.risk_engine import RejectReason
    o = Orchestrator(cfg)
    d = o.can_enter(confidence=75, is_expiry_day=True,
                    when=datetime(2026, 8, 6, 14, 30))
    assert d.reason == RejectReason.EXPIRY_CUTOFF


# ---------------------------------------------------------------- crash recovery

def test_rearm_reproduces_state_exactly(cfg):
    live = Orchestrator(cfg)
    live.on_trade_opened(); live.on_trade_closed(1500.0)     # EARNED, floor 1050
    live.on_trade_opened(); live.on_trade_closed(-300.0)
    before = live.snapshot()

    _closed_trade(1500.0)
    _closed_trade(-300.0)

    rebooted = Orchestrator(cfg)
    after = rebooted.rearm()

    assert after.state == before.state
    assert after.day_pnl == before.day_pnl
    assert after.floor == before.floor
    assert after.trades_taken == before.trades_taken
    assert after.consecutive_losses == before.consecutive_losses
    assert after.bonus_unlocked == before.bonus_unlocked


def test_rearm_does_not_refund_loss_limit(cfg):
    """A reboot must not hand back a blown day."""
    _closed_trade(-600.0)
    _closed_trade(-600.0)          # two losses -> LOCKED
    o = Orchestrator(cfg)
    snap = o.rearm()
    assert snap.state == DayState.LOCKED.value
    assert snap.day_pnl == -1200.0
    assert not o.can_enter(confidence=99).allowed


def test_rearm_counts_open_trades_against_budget(cfg):
    _closed_trade(100.0)
    _open_trade()
    o = Orchestrator(cfg)
    snap = o.rearm()
    assert snap.trades_taken == 2


def test_rearm_ignores_other_days(cfg):
    _closed_trade(-900.0, sdate="2026-07-01")
    o = Orchestrator(cfg)
    snap = o.rearm()
    assert snap.day_pnl == 0.0
    assert snap.state == DayState.NORMAL.value


def test_rearm_restores_unrealized_for_open_position(cfg):
    live = Orchestrator(cfg)
    live.on_trade_opened()
    live.on_pnl_tick(-400.0)
    _open_trade()
    rebooted = Orchestrator(cfg)
    snap = rebooted.rearm()
    assert snap.unrealized == -400.0
    assert snap.day_pnl == -400.0


# ---------------------------------------------------------------- weekly governor

def test_week_lock_at_2_5x_loss_limit(cfg):
    """Week P&L is keyed by session_date, so distinct days must be simulated.
    Re-running the same date overwrites that day's entry, which is the correct
    idempotent behaviour for a live process closing the same day twice."""
    o = Orchestrator(cfg, week_loss_limit_mult=2.5)
    assert not o.week_locked
    # 2.5 x 1050 = 2625
    for day, pnl in (("2026-08-03", -1000.0), ("2026-08-04", -1000.0)):
        o.reset_day()
        o.session_date = day
        o.on_trade_opened(); o.on_trade_closed(pnl)
        assert not o.week_locked, day

    o.reset_day()
    o.session_date = "2026-08-05"
    o.on_trade_opened(); o.on_trade_closed(-700.0)   # cumulative -2700
    assert o.week_locked
    with session() as s:
        assert s.query(Violation).filter(Violation.kind == "WEEK_LOCK").count() == 1


def test_same_day_closed_twice_does_not_double_count_week(cfg):
    o = Orchestrator(cfg, week_loss_limit_mult=2.5)
    o.session_date = "2026-08-05"
    o.on_trade_opened(); o.on_trade_closed(-1000.0)
    o.close_session()
    o.close_session()                                 # replayed, e.g. after a restart
    assert not o.week_locked


def test_three_red_days_forces_paper(cfg):
    for _ in range(2):
        o = Orchestrator(cfg)
        o.on_trade_opened(); o.on_trade_closed(-100.0)
        out = o.close_session()
        assert not out["force_paper_next"]
    o = Orchestrator(cfg)
    o.on_trade_opened(); o.on_trade_closed(-100.0)
    out = o.close_session()
    assert out["force_paper_next"] and out["red_streak"] == 3


def test_green_day_resets_red_streak(cfg):
    o = Orchestrator(cfg)
    o.on_trade_opened(); o.on_trade_closed(-100.0)
    o.close_session()
    o2 = Orchestrator(cfg)
    o2.on_trade_opened(); o2.on_trade_closed(+100.0)
    out = o2.close_session()
    assert out["red_streak"] == 0


def test_floor_distance_drives_warning(cfg):
    o = Orchestrator(cfg)
    o.on_trade_opened()
    o.on_trade_closed(1500.0)       # floor 1050, dayPnL 1500
    assert o.floor_distance() == 450.0


def test_kill_switch_toggle_reaches_engine(cfg):
    o = Orchestrator(cfg)
    assert o.can_enter(confidence=99, when=datetime(2026, 8, 6, 11, 0)).allowed
    o.set_kill_switch(False)
    assert not o.can_enter(confidence=99, when=datetime(2026, 8, 6, 11, 0)).allowed


def test_reset_day_clears_state(cfg):
    o = Orchestrator(cfg)
    o.on_trade_opened(); o.on_trade_closed(-600.0)
    o.on_trade_opened(); o.on_trade_closed(-600.0)
    assert o.is_locked
    snap = o.reset_day()
    assert snap.state == DayState.NORMAL.value
    assert snap.trades_taken == 0
    assert snap.floor == -cfg.loss_limit


def test_squareoff_clock_locks(cfg):
    o = Orchestrator(cfg)
    snap = o.on_clock(datetime(2026, 8, 6, 15, 10))
    assert snap.state == DayState.LOCKED.value
    assert "square-off" in snap.lock_reason

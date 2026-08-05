"""Kill switch: persistence, typed re-enable, zero side effects while OFF."""
from sentinel.core.killswitch import KillSwitch
from sentinel.db import Violation, session


def test_defaults_on_fresh_db():
    assert KillSwitch().is_on


def test_off_persists_across_instances():
    KillSwitch().turn_off("testing")
    assert KillSwitch().is_off, "a switch that forgets it was off is not a switch"


def test_reenable_requires_exact_phrase():
    ks = KillSwitch()
    ks.turn_off("testing")
    for bad in ("enable", "yes", "", "ENABLED", "ENABLE NOW"):
        assert not ks.turn_on(bad)["ok"], bad
        assert ks.is_off
    assert ks.turn_on("ENABLE")["ok"]
    assert ks.is_on


def test_surrounding_whitespace_tolerated():
    """A mobile keyboard's trailing space is a typo, not a sign of confusion.
    Case and wording are still strict — only whitespace is forgiven."""
    ks = KillSwitch()
    ks.turn_off("testing")
    assert ks.turn_on("  ENABLE ")["ok"]
    assert ks.is_on


def test_guard_entry_blocks_and_logs_violation():
    ks = KillSwitch()
    ks.turn_off("testing")
    assert ks.guard_entry("2026-08-06") is False
    with session() as s:
        rows = s.query(Violation).filter(Violation.kind == "ENTRY_WHILE_KILLED").all()
        assert len(rows) == 1


def test_guard_entry_allows_when_on():
    ks = KillSwitch()
    assert ks.guard_entry("2026-08-06") is True
    with session() as s:
        assert s.query(Violation).count() == 0


def test_history_records_flips():
    ks = KillSwitch()
    ks.turn_off("reason A")
    ks.turn_on("ENABLE", "reason B")
    hist = ks.history()
    assert [h["action"] for h in hist[-2:]] == ["OFF", "ON"]
    assert hist[-2]["reason"] == "reason A"


def test_engine_refuses_entry_when_switch_off(cfg):
    """The engine's own gate, independent of the DB switch."""
    from datetime import datetime

    from sentinel.risk_engine import RejectReason, RiskEngine
    e = RiskEngine(cfg, kill_switch_on=False)
    d = e.can_enter(datetime(2026, 8, 6, 11, 0), confidence=95)
    assert not d.allowed and d.reason == RejectReason.KILL_SWITCH

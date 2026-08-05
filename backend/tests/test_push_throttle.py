"""Push repeat-suppression.

FLOOR_WARNING is sent from the P&L tick (every 2s). Un-throttled, hovering Rs.200 from
the floor for five minutes fired ~150 notifications at the phone. A suggestion must
never be suppressed, though — that one is time-critical and each is distinct.
"""
import pytest
from sentinel.api.push import DEFAULT_REPEAT_COOLDOWN_S, REPEAT_COOLDOWN_S, Pusher


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """A Pusher with one fake subscription; records payloads instead of sending."""
    from sentinel import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "push.db")
    monkeypatch.setattr(db_mod, "_engine", None, raising=False)
    monkeypatch.setattr(db_mod, "_SessionLocal", None, raising=False)
    db_mod.init_db()

    p = Pusher(public_key="pub", private_key="priv", subject="mailto:x@y.z")
    p.subscribe("https://push.example/abc", "p256", "auth")

    calls = []
    monkeypatch.setattr("sentinel.api.push.webpush",
                        lambda **kw: calls.append(kw["data"]))
    return p, calls


def test_identical_floor_warning_is_sent_once(sent):
    p, calls = sent
    for _ in range(150):                       # five minutes of 2s ticks
        p.floor_warning(1250, 1050, 200)
    assert len(calls) == 1
    assert p.suppressed == 149


def test_a_changed_message_still_gets_through(sent):
    p, calls = sent
    p.floor_warning(1250, 1050, 200)
    p.floor_warning(1150, 1050, 100)           # closer to the floor: new information
    assert len(calls) == 2


def test_suggestions_are_never_throttled(sent):
    p, calls = sent
    assert REPEAT_COOLDOWN_S["SUGGESTION"] == 0
    for _ in range(3):
        p.send("SUGGESTION", "identical text on purpose")
    assert len(calls) == 3


def test_cooldown_expiry_allows_a_resend(sent, monkeypatch):
    p, calls = sent
    clock = [1000.0]
    monkeypatch.setattr("sentinel.api.push.time.monotonic", lambda: clock[0])

    p.send("SYSTEM", "feed degraded")
    p.send("SYSTEM", "feed degraded")
    assert len(calls) == 1

    clock[0] += REPEAT_COOLDOWN_S["SYSTEM"] + 1
    p.send("SYSTEM", "feed degraded")
    assert len(calls) == 2


def test_unknown_kind_gets_the_default_window(sent, monkeypatch):
    p, calls = sent
    clock = [0.0]
    monkeypatch.setattr("sentinel.api.push.time.monotonic", lambda: clock[0])
    p.send("SUGGESTION", "x")                  # known, uncapped
    p.send("WEIRD", "y")
    p.send("WEIRD", "y")
    assert len(calls) == 2
    clock[0] += DEFAULT_REPEAT_COOLDOWN_S + 1
    p.send("WEIRD", "y")
    assert len(calls) == 3

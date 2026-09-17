"""Lifecycle gates, approve re-validation, and regressions for review-pass fixes.

The three regression tests at the bottom cover defects found by review, each of
which would have cost real money:
  - a time stop dumping every position instead of its own
  - booking an exit at 0.0 and feeding a fabricated loss into the FSM
  - an approval that skipped re-validation at tap time
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from freezegun import freeze_time
from vectra_quant.brokers.base import (
    Instrument,
    InstrumentMaster,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
)
from vectra_quant.core.guardian import Guardian
from vectra_quant.core.killswitch import KillSwitch
from vectra_quant.core.lifecycle import Lifecycle
from vectra_quant.core.orchestrator import Orchestrator
from vectra_quant.db import Trade, session, utcnow

SYM = "SENSEX2680678800CE"
OTHER = "NIFTY2681124600PE"
INST = Instrument(trading_symbol=SYM, exchange="BSE", segment="FNO", lot_size=20,
                  instrument_type="CE", name="SENSEX", strike=78800.0,
                  expiry="2026-08-06", exchange_token="1")
INST2 = Instrument(trading_symbol=OTHER, exchange="NSE", segment="FNO", lot_size=65,
                   instrument_type="PE", name="NIFTY", strike=24600.0,
                   expiry="2026-08-11", exchange_token="2")


class FakeBroker:
    name = "fake"

    def __init__(self):
        self.positions: list[Position] = []
        self.orders: list[Order] = []
        self.placed = []
        self.squareoff_calls = 0
        self.quote_price = 300.0
        self.fill_entries = True
        self.partial_fill_qty = None
        self.master = InstrumentMaster([INST, INST2], datetime.now(UTC))

    def get_positions(self, **_):
        return list(self.positions)

    def get_orders(self, **_):
        return list(self.orders)

    def get_instruments(self, **_):
        return self.master

    def get_quote(self, keys, **_):
        return {k: Quote(trading_symbol=k, last_price=self.quote_price) for k in keys}

    def place_order(self, req):
        self.placed.append(req)
        oid = f"O{len(self.placed)}"
        entry = req.order_type is OrderType.LIMIT
        if entry and not self.fill_entries:
            filled, status = 0, OrderStatus.OPEN
        elif entry and self.partial_fill_qty is not None:
            filled, status = self.partial_fill_qty, OrderStatus.FILLED
        else:
            filled, status = req.quantity, OrderStatus.FILLED
        self.orders.append(Order(
            order_id=oid, trading_symbol=req.trading_symbol, side=req.side,
            quantity=req.quantity, filled_quantity=filled, status=status,
            order_type=req.order_type, trigger_price=req.trigger_price,
            average_price=(self.quote_price if filled else 0.0),
            client_id=req.client_id, exchange=req.exchange, segment=req.segment,
            created_at=datetime.now(UTC),
        ))
        return oid

    def cancel_order(self, oid, **_):
        for o in self.orders:
            if o.order_id == oid:
                o.status = OrderStatus.CANCELLED
                return

    def square_off_all(self):
        self.squareoff_calls += 1
        self.positions = []
        return ["SQALL"]


class FakeInstruments:
    def __init__(self, master):
        self.master = master

    def get(self, sym):
        return self.master.get(sym)

    def current_expiry(self, name):
        return self.master.nearest_expiry(name, "2026-08-05")

    def expiring_today(self, names):
        return []

    def resolve_offset(self, name, spot, label, side):
        return INST if name == "SENSEX" else INST2


class FakeChain:
    def candidates(self, name, side):
        return []


@pytest.fixture(autouse=True)
def _mid_session():
    """11:00 IST on a Wednesday — inside the 09:20-15:00 entry window."""
    with freeze_time("2026-08-05 05:30:00"):   # 11:00 IST
        yield


@pytest.fixture
def stack(cfg):
    broker = FakeBroker()
    orch = Orchestrator(cfg)
    guardian = Guardian(broker, orch.engine, instrument_of=lambda s: broker.master.get(s))
    settings = _Settings(cfg)
    lc = Lifecycle(broker=broker, orchestrator=orch, guardian=guardian,
                   instruments=FakeInstruments(broker.master), chain=FakeChain(),
                   settings=settings, killswitch=KillSwitch())
    return lc, broker, orch


class _Settings:
    """Minimal stand-in for the real Settings object."""

    def __init__(self, cfg):
        self.risk = cfg
        self.capital = cfg.capital

        class S:
            max_position_cost = 14_000.0
            min_lots = 1
            max_lots = {"SENSEX": 4, "NIFTY": 1}

            def lots_ceiling(self, instrument):
                return int(self.max_lots.get(instrument.upper(), 0))
        self.sizing = S()

        class Instr:
            primary = "SENSEX"
            secondary = "NIFTY"
        self.instruments = Instr()
        self.raw = {"sizing": {"preferred_cost_min": 7000, "preferred_cost_max": 12000},
                    "strike_selection": {"max_atm_offset": 2}}


def _payload(**over):
    p = {
        "action": "SUGGEST", "instrument": "SENSEX", "direction": "CE",
        "strike_offset": "ATM", "entry_zone": {"low": 320, "high": 340},
        "stop_loss_premium": 290, "target_premium": 400, "time_stop_minutes": 45,
        "confidence": 80, "thesis": "t", "invalidation": "i", "risk_reward": 1.85,
    }
    p.update(over)
    return p


def _open_trade(broker, symbol=SYM, minutes_ago=0, time_stop=0):
    from datetime import timedelta
    tid = str(uuid.uuid4())
    sid = str(uuid.uuid4()) if time_stop else None
    with session() as s:
        if sid:
            from vectra_quant.db import Suggestion
            s.add(Suggestion(id=sid, ts=utcnow(), action="SUGGEST",
                             time_stop_minutes=time_stop, trading_symbol=symbol))
        s.add(Trade(
            id=tid, opened_at=utcnow() - timedelta(minutes=minutes_ago),
            session_date="2026-08-05", origin="ai", suggestion_id=sid,
            trading_symbol=symbol, exchange="BSE", segment="FNO",
            lots=1, lot_size=20, qty=20, entry_price=300.0, status="OPEN",
        ))
    inst = broker.master.get(symbol)
    broker.positions.append(Position(
        trading_symbol=symbol, quantity=20 if symbol == SYM else 65,
        average_price=300.0, last_price=310.0, lot_size=inst.lot_size,
        exchange=inst.exchange, segment="FNO"))
    return tid


# ---------------------------------------------------------------- gates

def test_gates_reject_low_confidence(stack):
    lc, _b, _o = stack
    gate, q = lc.apply_gates(_payload(confidence=69), spot=78800, is_expiry_day=False)
    assert not gate.passed and "69" in gate.reason


def test_gates_reject_thin_rr(stack):
    lc, _b, _o = stack
    # entry mid 330, stop 320 -> risk 10; target 340 -> reward 10 => RR 1.0
    gate, q = lc.apply_gates(
        _payload(entry_zone={"low": 320, "high": 340}, stop_loss_premium=320,
                 target_premium=340), spot=78800, is_expiry_day=False)
    assert not gate.passed and "RR" in gate.reason


def test_gates_reject_when_locked(stack):
    lc, _b, orch = stack
    orch.on_trade_opened()
    orch.on_pnl_tick(-6000.0)  # loss_limit at capital=120000
    gate, _q = lc.apply_gates(_payload(), spot=78800, is_expiry_day=False)
    assert not gate.passed


def test_gates_reject_over_cost_cap(stack):
    """SENSEX lot 20 @ premium 800 = 16,000 > 14,000 cap."""
    lc, _b, _o = stack
    gate, _q = lc.apply_gates(
        _payload(entry_zone={"low": 790, "high": 810}, stop_loss_premium=760,
                 target_premium=880, risk_reward=1.75),
        spot=78800, is_expiry_day=False)
    assert not gate.passed and "cap" in gate.reason


def test_gate_pass_produces_sized_suggestion(stack):
    lc, _b, _o = stack
    gate, q = lc.apply_gates(_payload(), spot=78800, is_expiry_day=False)
    assert gate.passed and q is not None
    assert q.lots >= 1 and q.qty == q.lots * 20
    assert q.cost <= 14_000


# ---------------------------------------------------------------- approve

def test_approve_revalidates_at_tap_time(stack):
    """The 90s TTL is only safe because can_enter runs again on the tap."""
    lc, broker, orch = stack
    gate, q = lc.apply_gates(_payload(), spot=78800, is_expiry_day=False)
    lc.enqueue(q)
    # Day blows up between card and tap.
    orch.on_trade_opened()
    orch.on_pnl_tick(-6000.0)  # loss_limit at capital=120000
    out = lc.approve(q.id)
    assert not out["ok"] and "tap time" in out["error"]
    assert broker.placed == [], "no order may reach the broker after a lock"


def test_approve_places_entry_and_stop(stack):
    lc, broker, _o = stack
    gate, q = lc.apply_gates(_payload(), spot=78800, is_expiry_day=False)
    lc.enqueue(q)
    out = lc.approve(q.id)
    assert out["ok"]
    kinds = [(r.side, r.order_type) for r in broker.placed]
    assert (Side.BUY, OrderType.LIMIT) in kinds
    assert (Side.SELL, OrderType.SL_M) in kinds, "a position must never sit without a stop"


def test_approve_twice_is_refused(stack):
    lc, broker, _o = stack
    gate, q = lc.apply_gates(_payload(), spot=78800, is_expiry_day=False)
    lc.enqueue(q)
    first = lc.approve(q.id)
    second = lc.approve(q.id)
    assert first["ok"] and not second["ok"]
    entries = [r for r in broker.placed if r.order_type is OrderType.LIMIT]
    assert len(entries) == 1, "double tap must not double-order (§0.4)"


def test_reject_removes_from_queue(stack):
    lc, _b, _o = stack
    gate, q = lc.apply_gates(_payload(), spot=78800, is_expiry_day=False)
    lc.enqueue(q)
    assert lc.reject(q.id)["ok"]
    assert lc.active() == []
    assert not lc.approve(q.id)["ok"]


# ---------------------------------------------------------------- close-out

def test_close_trade_books_net_of_costs(stack):
    lc, broker, orch = stack
    tid = _open_trade(broker)
    out = lc.close_trade(tid, 340.0, "target")
    assert out["ok"]
    assert out["gross"] == pytest.approx((340.0 - 300.0) * 20)
    assert out["costs"] > 0
    assert out["net"] == pytest.approx(out["gross"] - out["costs"])
    assert orch.snapshot().realized == pytest.approx(out["net"])


# ---------------------------------------------------------------- regressions

def test_time_stop_exits_only_its_own_contract(stack):
    """Regression: check_time_stops used to call square_off_all(), dumping every
    position including manual ones the trader was managing themselves."""
    lc, broker, _o = stack
    timed = _open_trade(broker, symbol=SYM, minutes_ago=90, time_stop=45)
    _open_trade(broker, symbol=OTHER, minutes_ago=1)

    closed = lc.check_time_stops()
    assert closed == [timed]
    assert broker.squareoff_calls == 0, "must not square off everything"
    exits = [r for r in broker.placed if r.order_type is OrderType.MARKET]
    assert len(exits) == 1 and exits[0].trading_symbol == SYM

    with session() as s:
        other = s.query(Trade).filter(Trade.trading_symbol == OTHER).one()
        assert other.status == "OPEN", "the untimed trade must be untouched"


def test_exit_price_never_zero(stack):
    """Regression: a 0.0 last price booked a fabricated 100% loss into the FSM."""
    lc, broker, _o = stack
    tid = _open_trade(broker)
    for p in broker.positions:
        p.last_price = 0.0          # broker reports nothing
    broker.quote_price = 305.0      # REST still answers
    price = lc._exit_price(SYM, 300.0)
    assert price == 305.0

    out = lc.close_trade(tid, price, "engine_lock")
    assert out["gross"] == pytest.approx((305.0 - 300.0) * 20)
    assert out["net"] > -1000, "must not look like a total wipeout"


def test_exit_price_falls_back_to_entry_when_blind(stack):
    lc, broker, _o = stack
    _open_trade(broker)
    for p in broker.positions:
        p.last_price = 0.0
    broker.quote_price = 0.0
    assert lc._exit_price(SYM, 300.0) == 300.0


def test_kill_switch_blocks_approval(stack):
    lc, broker, _o = stack
    gate, q = lc.apply_gates(_payload(), spot=78800, is_expiry_day=False)
    lc.enqueue(q)
    lc.killswitch.turn_off("drill")
    out = lc.approve(q.id)
    assert not out["ok"] and "kill switch" in out["error"]
    assert broker.placed == []


def test_sensex_allows_up_to_four_lots(stack):
    """User rule: SENSEX may go to 4 lots (80 qty), not 1.

    Needs a tight stop to fit inside r=1200: 4 x 20 x 15 = 1200 exactly.
    """
    lc, _b, _o = stack
    gate, q = lc.apply_gates(
        _payload(entry_zone={"low": 60, "high": 65}, stop_loss_premium=47.5,
                 target_premium=100, risk_reward=2.7),
        spot=78800, is_expiry_day=False)
    assert gate.passed, gate.reason
    assert q.lots == 4 and q.qty == 80, f"got {q.lots} lots / {q.qty} qty"
    assert q.cost <= 14_000


def test_lot_ceiling_caps_below_what_risk_would_allow(stack):
    """A very tight stop could justify more than 4 lots; the ceiling still binds."""
    lc, _b, _o = stack
    gate, q = lc.apply_gates(
        _payload(entry_zone={"low": 20, "high": 22}, stop_loss_premium=19,
                 target_premium=30, risk_reward=4.0),
        spot=78800, is_expiry_day=False)
    assert gate.passed, gate.reason
    assert q.lots == 4, f"ceiling should cap at 4, got {q.lots}"


def test_cost_cap_still_beats_lot_ceiling(stack):
    """4 lots at premium 330 = 26,400 > 14,000, so cost wins and lots drop."""
    lc, _b, _o = stack
    gate, q = lc.apply_gates(
        _payload(entry_zone={"low": 325, "high": 335}, stop_loss_premium=315,
                 target_premium=365, risk_reward=2.0),
        spot=78800, is_expiry_day=False)
    assert gate.passed, gate.reason
    assert q.lots == 2 and q.cost <= 14_000


# ================================================================ review regressions

def test_broker_side_exit_is_booked_into_the_fsm(stack):
    """#1 — an SL fill used to land as CLOSED/pnl=0, so a full stop-out read Rs.0.

    That refunded the trade budget, made the -1050 daily lock unreachable on realized
    losses, and stopped the 2-consecutive-loss rule from ever firing.
    """
    lc, broker, orch = stack
    tid = _open_trade(broker)
    broker.positions = []                       # broker flat: the stop filled
    broker.orders.append(Order(
        order_id="SL_FILL", trading_symbol=SYM, side=Side.SELL, quantity=20,
        filled_quantity=20, status=OrderStatus.FILLED, order_type=OrderType.SL_M,
        average_price=270.0, trigger_price=270.0, segment="FNO",
    ))
    settled = lc.settle_broker_exits()
    assert settled == [tid]
    assert orch.snapshot().realized < 0, "the loss must reach the FSM"
    with session() as s:
        t = s.get(Trade, tid)
        assert t.status == "CLOSED" and t.pnl < 0 and t.exit_reason == "stop_loss"


def test_unfilled_entry_is_cancelled_and_slot_refunded(stack):
    """#2 — an unfilled LIMIT used to be booked OPEN at the limit price with a
    full-size stop resting, producing phantom P&L and an orphan naked SELL."""
    lc, broker, orch = stack
    lc.fill_wait_s = 0
    broker.fill_entries = False                  # the LIMIT never fills
    gate, q = lc.apply_gates(_payload(), spot=78800, is_expiry_day=False)
    lc.enqueue(q)
    out = lc.approve(q.id)
    assert not out["ok"] and "did not fill" in out["error"]
    assert orch.snapshot().trades_taken == 0, "trade slot must be refunded"
    assert not any(r.order_type is OrderType.SL_M for r in broker.placed), \
        "no stop may rest against an unfilled entry"
    with session() as s:
        assert s.query(Trade).count() == 0


def test_partial_fill_stop_mirrors_filled_quantity(stack):
    """#2 — the stop must cover what actually filled, not what was requested."""
    lc, broker, _o = stack
    lc.fill_wait_s = 0
    broker.partial_fill_qty = 20                 # 40 requested, 20 filled
    gate, q = lc.apply_gates(
        _payload(entry_zone={"low": 60, "high": 65}, stop_loss_premium=47.5,
                 target_premium=100, risk_reward=2.7),
        spot=78800, is_expiry_day=False)
    lc.enqueue(q)
    out = lc.approve(q.id)
    assert out["ok"]
    sl = next(r for r in broker.placed if r.order_type is OrderType.SL_M)
    assert sl.quantity == 20, f"stop covered {sl.quantity}, should mirror the 20 filled"


def test_substituted_strike_gets_prices_rescaled(stack):
    """#5 — the order used to go out priced for the contract it was NOT buying,
    which could put the stop above the entry so it fired instantly."""
    lc, broker, _o = stack
    from vectra_quant.core.sizing import Candidate

    cheap = Candidate(trading_symbol=OTHER, strike=24600.0, side="CE", premium=65.0,
                      lot_size=65, atm_offset=0, open_interest=5000, volume=2000)
    lc.chain = type("C", (), {"candidates": lambda self, n, s: [cheap]})()
    gate, q = lc.apply_gates(_payload(), spot=78800, is_expiry_day=False)
    assert gate.passed, gate.reason
    assert q.trading_symbol == OTHER
    assert q.stop_loss < q.entry_low < q.entry_high < q.target, \
        f"prices not rescaled: sl={q.stop_loss} entry={q.entry_low}-{q.entry_high} tgt={q.target}"
    assert q.entry_high < 100, "prices must be anchored on the new 65-premium contract"


def test_over_risk_card_can_actually_be_approved(stack):
    """#10 — approve() passed proposed_risk, so every over_risk card was rejected at
    tap time, contradicting 'one lot is always allowed, just flagged'."""
    lc, broker, orch = stack
    lc.fill_wait_s = 0
    orch.on_trade_opened(); orch.on_trade_closed(10000.0)     # -> PROTECT (>= target 9600 at capital=120000)
    # halved risk budget = risk_scale_protected(0.5) x risk_per_trade(capital x
    # risk_per_trade_pct = 120000 x 0.025 = 3000) = 1500. mid=330, sl=250 ->
    # 80pts x lot 20 = 1600 risk, which exceeds it; tgt=460 keeps RR = 130/80 = 1.625 >= 1.5.
    gate, q = lc.apply_gates(
        _payload(confidence=85, entry_zone={"low": 320, "high": 340},
                 stop_loss_premium=250, target_premium=460, risk_reward=1.625),
        spot=78800, is_expiry_day=False)
    assert gate.passed, gate.reason
    assert q.over_risk, "20 x 80pts = 1600 risk exceeds the halved 1500"
    lc.enqueue(q)
    out = lc.approve(q.id)
    assert out["ok"], f"over_risk card must be approvable, got: {out.get('error')}"
    assert out["over_risk"] is True


def test_target_exit_is_tick_driven_and_spares_your_stop(stack):
    """#8 + 'never override what I placed'.

    Target exits with a MARKET order when the premium reaches it. No resting target
    order is created, and a stop the trader placed themselves is left untouched —
    only the stop VECTRA_QUANT rested is cancelled.
    """
    lc, broker, orch = stack
    tid = _open_trade(broker)
    with session() as s:
        tr = s.get(Trade, tid)
        tr.target_price = 400.0
        tr.sl_order_id = "OUR_SL"

    broker.orders.append(Order(
        order_id="OUR_SL", trading_symbol=SYM, side=Side.SELL, quantity=20,
        filled_quantity=0, status=OrderStatus.OPEN, order_type=OrderType.SL_M,
        trigger_price=270.0, segment="FNO",
    ))
    broker.orders.append(Order(
        order_id="THEIR_OCO_SL", trading_symbol=SYM, side=Side.SELL, quantity=20,
        filled_quantity=0, status=OrderStatus.OPEN, order_type=OrderType.SL_M,
        trigger_price=280.0, client_id="FNO_OCO_V2_NRMLOCO_SL_ORDER-xyz", segment="FNO",
    ))

    for p_ in broker.positions:
        p_.last_price = 410.0                    # target hit
    broker.quote_price = 410.0

    exited = lc.check_targets()
    assert exited == [tid]

    ours = next(o for o in broker.orders if o.order_id == "OUR_SL")
    theirs = next(o for o in broker.orders if o.order_id == "THEIR_OCO_SL")
    assert ours.status is OrderStatus.CANCELLED, "our own stop should be pulled"
    assert theirs.status is OrderStatus.OPEN, "the trader's stop must NEVER be touched"

    assert not any(r.order_type in (OrderType.SL, OrderType.SL_M) and r.side is Side.SELL
                   and r.trigger_price == 400.0 for r in broker.placed), \
        "no resting target order may be created"
    assert orch.snapshot().realized > 0


def test_target_not_triggered_below_target(stack):
    lc, broker, _o = stack
    tid = _open_trade(broker)
    with session() as s:
        s.get(Trade, tid).target_price = 400.0
    for p_ in broker.positions:
        p_.last_price = 350.0
    broker.quote_price = 350.0
    assert lc.check_targets() == []

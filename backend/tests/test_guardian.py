"""Guardian: manual-trade detection (both paths), SL attach, violations, square-off.

This is the layer that protects trades the system did not initiate, so the tests
lean on the failure directions: never double-count, never leave a position naked
without shouting, never invent a stop distance.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from vectra_quant.brokers.base import (
    Instrument,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from vectra_quant.core.guardian import Guardian
from vectra_quant.db import Trade, Violation, session
from vectra_quant.risk_engine import DayState, RiskEngine

SYM = "NIFTY2681124250CE"
INST = Instrument(trading_symbol=SYM, exchange="NSE", segment="FNO", lot_size=65,
                  instrument_type="CE", name="NIFTY", strike=24250.0,
                  expiry="2026-08-11", exchange_token="1")


class FakeBroker:
    name = "fake"

    def __init__(self):
        self.orders: list[Order] = []
        self.positions: list[Position] = []
        self.placed: list = []
        self.fail_place = False
        self.squareoff_calls = 0
        self.flat_after_squareoff = True

    def get_orders(self, **_):
        return list(self.orders)

    def get_positions(self, **_):
        return list(self.positions)

    def place_order(self, req):
        if self.fail_place:
            from vectra_quant.brokers.base import BrokerError
            raise BrokerError("broker rejected: RMS block")
        self.placed.append(req)
        oid = f"OID{len(self.placed)}"
        self.orders.append(Order(
            order_id=oid, trading_symbol=req.trading_symbol, side=req.side,
            quantity=req.quantity, filled_quantity=0, status=OrderStatus.OPEN,
            order_type=req.order_type, trigger_price=req.trigger_price,
            client_id=req.client_id, exchange=req.exchange, segment=req.segment,
        ))
        return oid

    def cancel_order(self, oid, **_):
        for o in self.orders:
            if o.order_id == oid:
                o.status = OrderStatus.CANCELLED
                return
        from vectra_quant.brokers.base import BrokerError
        raise BrokerError(f"unknown order {oid}")

    def square_off_all(self):
        self.squareoff_calls += 1
        if self.flat_after_squareoff:
            self.positions = []
        return ["SQ1"]


def manual_fill(qty: int = 65, price: float = 141.0, oid: str = "MANUAL1",
                side: Side = Side.BUY) -> Order:
    return Order(
        order_id=oid, trading_symbol=SYM, side=side, quantity=qty,
        filled_quantity=qty, status=OrderStatus.FILLED, order_type=OrderType.MARKET,
        average_price=price, client_id="", exchange="NSE", segment="FNO",
    )


@pytest.fixture
def setup(cfg):
    broker = FakeBroker()
    engine = RiskEngine(cfg)
    alerts: list[tuple[str, str, dict]] = []
    g = Guardian(
        broker, engine, sl_attach_deadline_s=0, squareoff_verify_s=1,
        on_alert=lambda k, m, d: alerts.append((k, m, d)),
        instrument_of=lambda s: INST if s == SYM else None,
    )
    g.prime()                    # production baselines the book at boot
    # A detected manual fill only counts when it leaves an open position.
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0,
                                 lot_size=65, exchange="NSE", segment="FNO")]
    return g, broker, engine, alerts


# ---------------------------------------------------------------- detection

def test_ws_path_detects_manual_fill(setup):
    g, _b, engine, alerts = setup
    assert g.on_order_event(manual_fill()) is True
    assert engine.trades_taken == 1
    assert any(k == "GUARDIAN" for k, _m, _d in alerts)
    with session() as s:
        t = s.query(Trade).one()
        assert t.origin == "manual" and t.qty == 65 and t.status == "OPEN"


def test_rest_path_detects_manual_fill(setup):
    g, broker, engine, _a = setup
    broker.orders.append(manual_fill())
    found = g.poll_once()
    assert len(found) == 1
    assert engine.trades_taken == 1


def test_both_paths_do_not_double_count(setup):
    """D-001 runs REST and WS together; the same order must count once."""
    g, broker, engine, _a = setup
    order = manual_fill()
    broker.orders.append(order)
    g.on_order_event(order)
    g.poll_once()
    g.poll_once()
    assert engine.trades_taken == 1
    with session() as s:
        assert s.query(Trade).count() == 1


def test_own_orders_are_not_manual(setup):
    g, _b, engine, _a = setup
    cid = str(uuid.uuid4())
    g.register_own(cid, "OWN1")
    own = manual_fill(oid="OWN1")
    own.client_id = cid
    assert g.on_order_event(own) is False
    assert engine.trades_taken == 0


def test_own_order_matched_by_truncated_reference(setup):
    """Groww echoes the 20-char alphanumeric reference, not our raw uuid."""
    g, _b, engine, _a = setup
    cid = "3f1a9c2e-4b5d-4e6f-8a9b-0c1d2e3f4a5b"
    g.register_own(cid)
    echoed = manual_fill(oid="OWN2")
    echoed.client_id = "3f1a9c2e4b5d4e6f8a9b"
    assert g.on_order_event(echoed) is False
    assert engine.trades_taken == 0


def test_resting_order_not_counted_until_filled(setup):
    g, _b, engine, _a = setup
    pending = manual_fill()
    pending.status = OrderStatus.OPEN
    pending.filled_quantity = 0
    assert g.on_order_event(pending) is False
    assert engine.trades_taken == 0


def test_sell_leg_is_not_a_new_trade(setup):
    g, _b, engine, _a = setup
    assert g.on_order_event(manual_fill(side=Side.SELL, oid="EXIT1")) is False
    assert engine.trades_taken == 0


# ---------------------------------------------------------------- violations

def test_manual_trade_while_locked_logs_violation(setup):
    g, _b, engine, _a = setup
    engine.on_trade_opened()
    engine.on_trade_closed.__self__.on_pnl_tick(-1050.0)   # force LOCKED
    assert engine.state == DayState.LOCKED
    g.on_order_event(manual_fill())
    with session() as s:
        kinds = [v.detail for v in s.query(Violation).all()]
    assert any("LOCKED" in d for d in kinds)


def test_manual_trade_beyond_cap_logs_violation(setup):
    g, _b, engine, _a = setup
    for _ in range(3):
        engine.on_trade_opened()
    g.on_order_event(manual_fill())
    with session() as s:
        details = [v.detail for v in s.query(Violation).all()]
    assert any("trade cap" in d for d in details)


# ---------------------------------------------------------------- SL enforcement

def test_attaches_sl_m_below_entry(setup):
    g, broker, _e, alerts = setup
    g.on_order_event(manual_fill(price=141.0))
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0,
                                 lot_size=65, exchange="NSE", segment="FNO")]
    attached = g.enforce_stops()
    assert attached == [SYM]
    req = broker.placed[-1]
    assert req.order_type is OrderType.SL_M
    assert req.side is Side.SELL
    assert req.trigger_price == 129.0          # 141 - 12 pts for NIFTY
    assert req.quantity == 65
    with session() as s:
        t = s.query(Trade).one()
        assert t.sl_order_id and t.sl_price == 129.0
    assert any("SL attached" in m for _k, m, _d in alerts)


def test_does_not_double_attach_when_stop_exists(setup):
    g, broker, _e, _a = setup
    g.on_order_event(manual_fill())
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0)]
    broker.orders.append(Order(
        order_id="EXISTING_SL", trading_symbol=SYM, side=Side.SELL, quantity=65,
        filled_quantity=0, status=OrderStatus.OPEN, order_type=OrderType.SL_M,
        trigger_price=130.0,
    ))
    assert g.enforce_stops() == []
    assert not any(r.order_type is OrderType.SL_M for r in broker.placed)


def test_no_attach_when_position_already_closed(setup):
    g, broker, _e, _a = setup
    g.on_order_event(manual_fill())
    broker.positions = []
    assert g.enforce_stops() == []


def test_unknown_instrument_shouts_instead_of_guessing(setup):
    """§0.2/§0.3: never invent a stop distance."""
    g, broker, _e, alerts = setup
    g._instrument_of = lambda s: Instrument(
        trading_symbol=s, exchange="NSE", segment="FNO", lot_size=25,
        instrument_type="CE", name="BANKNIFTY",
    )
    g.on_order_event(manual_fill())
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0)]
    assert g.enforce_stops() == []
    assert any("UNPROTECTED" in m for _k, m, _d in alerts)


def test_sl_placement_failure_raises_alarm(setup):
    g, broker, _e, alerts = setup
    g.on_order_event(manual_fill())
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0)]
    broker.fail_place = True
    assert g.enforce_stops() == []
    assert any("UNPROTECTED" in m for _k, m, _d in alerts)


def test_deadline_respected_before_attaching(cfg):
    broker = FakeBroker()
    g = Guardian(broker, RiskEngine(cfg), sl_attach_deadline_s=60,
                 instrument_of=lambda s: INST)
    g.on_order_event(manual_fill())
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0)]
    assert g.enforce_stops() == [], "must wait out the 60s grace period"
    g._pending_sl[SYM] = (datetime.now(UTC) - timedelta(seconds=61), 141.0, 65)
    assert g.enforce_stops() == [SYM]


# ---------------------------------------------------------------- square-off

def test_square_off_verifies_flat(setup):
    g, broker, _e, _a = setup
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0)]
    ids, flat = g.square_off_all("loss limit")
    assert ids and flat
    assert broker.squareoff_calls == 1


def test_square_off_retries_then_screams(setup):
    g, broker, _e, alerts = setup
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0)]
    broker.flat_after_squareoff = False
    ids, flat = g.square_off_all("loss limit")
    assert not flat
    assert broker.squareoff_calls == 2, "one retry, per §8"
    assert any("CRITICAL" in m for _k, m, _d in alerts)


def test_adopt_existing_does_not_count_trades(setup):
    g, _b, engine, _a = setup
    g.adopt_existing([Position(trading_symbol=SYM, quantity=65, average_price=141.0)])
    assert engine.trades_taken == 0, "a restart must not re-bill the budget"


# ---------------------------------------------------------------- history regression

def test_prime_baselines_existing_orders(cfg):
    """Regression: a mid-day restart used to re-adopt the whole order book.

    Observed live — 29 historical fills became 29 "new" manual trades, blew the
    3-trade cap, produced 9 phantom consecutive losses, locked the session and
    booked -Rs.7,153 of pure round-trip costs into the FSM.
    """
    broker = FakeBroker()
    for i in range(29):
        broker.orders.append(manual_fill(oid=f"HIST{i}"))
    engine = RiskEngine(cfg)
    g = Guardian(broker, engine, sl_attach_deadline_s=0,
                 instrument_of=lambda s: INST)

    baselined = g.prime()
    assert baselined == 29

    assert g.poll_once() == []
    assert engine.trades_taken == 0, "history must not consume the trade budget"
    assert engine.state != DayState.LOCKED
    with session() as s:
        assert s.query(Trade).count() == 0
        assert s.query(Violation).count() == 0


def test_unprimed_guardian_acts_on_nothing(cfg):
    """Fail closed: before priming, the book is untrusted."""
    broker = FakeBroker()
    broker.orders.append(manual_fill())
    engine = RiskEngine(cfg)
    g = Guardian(broker, engine, instrument_of=lambda s: INST)
    assert g.poll_once() == []
    assert engine.trades_taken == 0


def test_fill_with_no_open_position_is_ignored(cfg):
    """A completed round trip is not exposure."""
    broker = FakeBroker()
    engine = RiskEngine(cfg)
    g = Guardian(broker, engine, instrument_of=lambda s: INST)
    g.prime()
    broker.positions = []                      # broker says flat
    assert g.on_order_event(manual_fill(oid="ROUNDTRIP")) is False
    assert engine.trades_taken == 0


def test_new_fill_after_prime_is_still_caught(cfg):
    """The fix must not blind the guardian to genuinely new trades."""
    broker = FakeBroker()
    broker.orders.append(manual_fill(oid="OLD"))
    engine = RiskEngine(cfg)
    g = Guardian(broker, engine, sl_attach_deadline_s=0,
                 instrument_of=lambda s: INST)
    g.prime()
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0,
                                 lot_size=65, exchange="NSE", segment="FNO")]
    broker.orders.append(manual_fill(oid="NEW_AFTER_BOOT"))
    found = g.poll_once()
    assert [o.order_id for o in found] == ["NEW_AFTER_BOOT"]
    assert engine.trades_taken == 1


# ---------------------------------------------------------------- yield to own SL

def test_guardian_withdraws_its_stop_when_yours_appears(setup):
    """"Don't override my SL": guardian steps aside so only ONE stop is live.

    Without this, guardian's stop and the trader's OCO stop both fire, selling the
    position twice and flipping it from long to SHORT with no protection.
    """
    g, broker, _e, alerts = setup
    g.on_order_event(manual_fill(price=141.0))
    g.enforce_stops()
    assert any(r.order_type is OrderType.SL_M for r in broker.placed)
    guardian_oid = broker.orders[-1].order_id

    # the trader's own stop lands a couple of minutes later
    broker.orders.append(Order(
        order_id="MY_OCO_SL", trading_symbol=SYM, side=Side.SELL, quantity=65,
        filled_quantity=0, status=OrderStatus.OPEN, order_type=OrderType.SL_M,
        trigger_price=133.0, client_id="FNO_OCO_V2_NRMLOCO_SL_ORDER-abc",
        segment="FNO",
    ))

    withdrawn = g.yield_to_own_stops()
    assert withdrawn == [SYM]
    mine = next(o for o in broker.orders if o.order_id == guardian_oid)
    assert mine.status is OrderStatus.CANCELLED, "guardian's stop must be cancelled"
    theirs = next(o for o in broker.orders if o.order_id == "MY_OCO_SL")
    assert theirs.status is OrderStatus.OPEN, "the trader's stop must survive"
    assert any("cancelled its backup stop" in m for _k, m, _d in alerts)


def test_no_withdrawal_when_only_guardian_stop_exists(setup):
    g, broker, _e, _a = setup
    g.on_order_event(manual_fill())
    g.enforce_stops()
    assert g.yield_to_own_stops() == [], "nothing to yield to"
    assert not any(o.status is OrderStatus.CANCELLED for o in broker.orders)


def test_withdrawal_can_be_disabled(cfg):
    broker = FakeBroker()
    g = Guardian(broker, RiskEngine(cfg), sl_attach_deadline_s=0,
                 yield_to_manual_sl=False, instrument_of=lambda s: INST)
    g.prime()
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0,
                                 lot_size=65, exchange="NSE", segment="FNO")]
    g.on_order_event(manual_fill())
    g.enforce_stops()
    broker.orders.append(Order(
        order_id="MY_SL", trading_symbol=SYM, side=Side.SELL, quantity=65,
        filled_quantity=0, status=OrderStatus.OPEN, order_type=OrderType.SL_M,
        trigger_price=133.0, segment="FNO",
    ))
    assert g.yield_to_own_stops() == []


# ================================================================ review regressions

def test_stop_coverage_is_quantity_aware(setup):
    """#9 — protection was a per-symbol boolean, so a 20-qty stop counted as
    covering 80 qty and the added lots stayed naked."""
    g, broker, _e, _a = setup
    broker.positions = [Position(trading_symbol=SYM, quantity=80, average_price=141.0,
                                 lot_size=65, exchange="NSE", segment="FNO")]
    broker.orders.append(Order(
        order_id="EXISTING_20", trading_symbol=SYM, side=Side.SELL, quantity=20,
        filled_quantity=0, status=OrderStatus.OPEN, order_type=OrderType.SL_M,
        trigger_price=130.0, segment="FNO",
    ))
    assert g.protected_quantity(SYM, broker.get_orders()) == 20
    g.on_order_event(manual_fill(qty=80))
    attached = g.enforce_stops()
    assert attached == [SYM]
    sl = next(r for r in broker.placed if r.order_type is OrderType.SL_M)
    assert sl.quantity == 60, f"should cover only the uncovered 60, got {sl.quantity}"


def test_fully_covered_position_gets_no_extra_stop(setup):
    g, broker, _e, _a = setup
    broker.positions = [Position(trading_symbol=SYM, quantity=65, average_price=141.0,
                                 lot_size=65, exchange="NSE", segment="FNO")]
    broker.orders.append(Order(
        order_id="FULL", trading_symbol=SYM, side=Side.SELL, quantity=65,
        filled_quantity=0, status=OrderStatus.OPEN, order_type=OrderType.SL_M,
        trigger_price=130.0, segment="FNO",
    ))
    g.on_order_event(manual_fill())
    assert g.enforce_stops() == []

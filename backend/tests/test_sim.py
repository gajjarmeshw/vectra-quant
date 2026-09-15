"""SimAdapter: slippage, costs, idempotency, stop triggering, square-off."""
from __future__ import annotations

import uuid

import pytest
from vectra_quant.brokers.base import (
    BrokerError,
    Instrument,
    InstrumentMaster,
    OrderRequest,
    OrderStatus,
    OrderType,
    Quote,
    Side,
)
from vectra_quant.brokers.sim import SimAdapter

SYM = "NIFTY2681124250CE"


class FakeData:
    """Minimal data source: fixed instrument master, mutable last price."""

    def __init__(self, price: float = 141.0):
        self.price = price
        from datetime import UTC, datetime
        self._master = InstrumentMaster([
            Instrument(trading_symbol=SYM, exchange="NSE", segment="FNO", lot_size=65,
                       instrument_type="CE", name="NIFTY", strike=24250.0,
                       expiry="2026-08-11", exchange_token="1")
        ], datetime.now(UTC))

    def get_instruments(self, **_):
        return self._master

    def get_quote(self, keys, **_):
        return {k: Quote(trading_symbol=k, last_price=self.price) for k in keys}

    def get_candles(self, *_a, **_k):
        return []

    def stream_ticks(self, *_a, **_k):
        raise NotImplementedError


def _buy(qty: int = 65, order_type: OrderType = OrderType.MARKET, **kw) -> OrderRequest:
    return OrderRequest(
        trading_symbol=SYM, exchange="NSE", segment="FNO", side=Side.BUY,
        quantity=qty, order_type=order_type, client_id=str(uuid.uuid4()), **kw
    )


@pytest.fixture
def sim():
    data = FakeData()
    s = SimAdapter(starting_funds=15_000.0, data_source=data)
    s.get_instruments()
    return s


def test_market_buy_fills_one_tick_adverse(sim):
    oid = sim.place_order(_buy())
    order = next(o for o in sim.get_orders() if o.order_id == oid)
    assert order.status is OrderStatus.FILLED
    assert order.average_price == 141.05, "buy pays one tick more (§9)"


def test_costs_are_deducted_from_funds(sim):
    before = sim.get_funds().available
    sim.place_order(_buy())
    after = sim.get_funds().available
    spent = before - after
    notional = 141.05 * 65
    assert spent > notional, "costs must be charged on top of notional"
    assert sim.costs_paid > 0


def test_idempotency_same_client_id_fills_once(sim):
    req = _buy()
    first = sim.place_order(req)
    second = sim.place_order(req)
    assert first == second
    assert len([o for o in sim.get_orders() if o.status is OrderStatus.FILLED]) == 1
    assert sum(abs(p.quantity) for p in sim.get_positions()) == 65


def test_insufficient_funds_refuses(sim):
    with pytest.raises(BrokerError, match="insufficient"):
        sim.place_order(_buy(qty=65 * 3))       # ~27k against 15k


def test_stop_order_rests_then_triggers(sim):
    sim.place_order(_buy())
    sl = OrderRequest(
        trading_symbol=SYM, exchange="NSE", segment="FNO", side=Side.SELL,
        quantity=65, order_type=OrderType.SL_M, trigger_price=129.0,
        client_id=str(uuid.uuid4()),
    )
    sl_id = sim.place_order(sl)
    resting = next(o for o in sim.get_orders() if o.order_id == sl_id)
    assert resting.status is OrderStatus.OPEN, "stop must not fill on arrival"

    assert sim.trigger_stops() == []             # price still 141
    sim._data.price = 128.0
    fired = sim.trigger_stops()
    assert len(fired) == 1
    assert next(o for o in sim.get_orders() if o.order_id == sl_id).status is OrderStatus.FILLED
    assert all(not p.is_open for p in sim.get_positions())


def test_realized_pnl_on_losing_stop(sim):
    sim.place_order(_buy())
    sim.place_order(OrderRequest(
        trading_symbol=SYM, exchange="NSE", segment="FNO", side=Side.SELL,
        quantity=65, order_type=OrderType.SL_M, trigger_price=129.0,
        client_id=str(uuid.uuid4()),
    ))
    sim._data.price = 128.0
    sim.trigger_stops()
    assert sim.realized < 0


def test_square_off_cancels_resting_stops_first(sim):
    sim.place_order(_buy())
    sl_id = sim.place_order(OrderRequest(
        trading_symbol=SYM, exchange="NSE", segment="FNO", side=Side.SELL,
        quantity=65, order_type=OrderType.SL_M, trigger_price=129.0,
        client_id=str(uuid.uuid4()),
    ))
    sim.square_off_all()
    sl = next(o for o in sim.get_orders() if o.order_id == sl_id)
    assert sl.status is OrderStatus.CANCELLED, "a stop must not fire after we are flat"
    assert all(not p.is_open for p in sim.get_positions())


def test_square_off_when_flat_is_a_noop(sim):
    assert sim.square_off_all() == []


def test_order_without_client_id_refused(sim):
    bad = OrderRequest(trading_symbol=SYM, exchange="NSE", segment="FNO", side=Side.BUY,
                       quantity=65, order_type=OrderType.MARKET, client_id="")
    with pytest.raises(BrokerError, match="client_id"):
        sim.place_order(bad)


def test_mark_updates_unrealized(sim):
    sim.place_order(_buy())
    sim.mark({SYM: 160.0})
    pos = next(p for p in sim.get_positions() if p.is_open)
    assert pos.unrealized_pnl == pytest.approx((160.0 - 141.05) * 65)


def test_cannot_modify_filled_order(sim):
    from vectra_quant.brokers.base import OrderPatch
    oid = sim.place_order(_buy())
    with pytest.raises(BrokerError, match="terminal"):
        sim.modify_order(oid, OrderPatch(price=150.0))


def test_no_blind_market_fill_without_price(sim):
    sim._data.price = 0.0
    with pytest.raises(BrokerError, match="no price"):
        sim.place_order(_buy())

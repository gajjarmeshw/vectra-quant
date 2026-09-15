"""SimAdapter — paper trading behind the same port. Design doc §9.

Same adapter interface as GrowwAdapter, so nothing upstream knows the difference.
Fills at touch price + 1 tick adverse slippage, full Indian cost model applied per
fill, and sim P&L feeds the same FSM.

Market data is real (delegated to a live adapter when one is supplied); only
execution is simulated. That way paper sessions exercise the true data path.
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from vectra_quant.brokers.base import (
    BrokerError,
    Candle,
    Funds,
    Instrument,
    InstrumentMaster,
    Order,
    OrderId,
    OrderPatch,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
    Subscription,
)
from vectra_quant.brokers.costs import CostRates, leg_cost
from vectra_quant.logging_setup import get

log = get("brokers.sim")

DEFAULT_TICK = 0.05


class _NullSub(Subscription):
    def close(self) -> None:
        return None


class SimAdapter:
    """Deterministic paper broker. Thread-safe: the scheduler and API both touch it."""

    name = "sim"

    def __init__(
        self,
        *,
        starting_funds: float = 15_000.0,
        data_source: Any | None = None,
        tick_size: float = DEFAULT_TICK,
        rates: CostRates | None = None,
    ):
        self._funds = float(starting_funds)
        self._data = data_source
        self._tick = float(tick_size)
        self._rates = rates or CostRates()
        self._orders: dict[OrderId, Order] = {}
        self._by_ref: dict[str, OrderId] = {}
        self._positions: dict[str, Position] = {}
        self._realized = 0.0
        self._costs_paid = 0.0
        self._lock = threading.RLock()
        self._order_cb: Callable[[Order], None] | None = None
        self._instruments: InstrumentMaster | None = None

    # ------------------------------------------------------------------ helpers
    def _instrument(self, symbol: str) -> Instrument | None:
        if self._instruments is None and self._data is not None:
            try:
                self._instruments = self._data.get_instruments()
            except Exception:  # pragma: no cover
                return None
        return self._instruments.get(symbol) if self._instruments else None

    def _ltp(self, symbol: str) -> float:
        if self._data is not None:
            try:
                q = self._data.get_quote([symbol])
                if symbol in q:
                    return float(q[symbol].last_price)
            except Exception:  # pragma: no cover
                pass
        pos = self._positions.get(symbol)
        return float(pos.last_price or pos.average_price) if pos else 0.0

    def _fill_price(self, req: OrderRequest, market: float) -> float:
        """Touch price + exactly one tick against us (§9)."""
        base = market
        if req.order_type is OrderType.LIMIT and req.price > 0:
            base = req.price
        elif req.order_type in (OrderType.SL, OrderType.SL_M) and req.trigger_price > 0:
            base = req.trigger_price
        adverse = self._tick if req.side is Side.BUY else -self._tick
        return round(max(0.05, base + adverse), 2)

    def _emit(self, order: Order) -> None:
        if self._order_cb:
            try:
                self._order_cb(order)
            except Exception as exc:  # pragma: no cover
                log.warning("sim order callback failed", extra={"error": str(exc)[:160]})

    # ------------------------------------------------------------------ execution
    def place_order(self, o: OrderRequest) -> OrderId:
        if not o.client_id:
            raise BrokerError("sim order without client_id — idempotency impossible (§0.4)")

        with self._lock:
            # Idempotency: same client_id never fills twice.
            if o.client_id in self._by_ref:
                existing = self._by_ref[o.client_id]
                log.warning("sim duplicate suppressed", extra={"client_id": o.client_id})
                return existing

            market = self._ltp(o.trading_symbol)
            if market <= 0 and o.order_type is OrderType.MARKET:
                raise BrokerError(f"no price for {o.trading_symbol} — sim refuses blind fill")

            price = self._fill_price(o, market)
            oid = f"SIM{uuid.uuid4().hex[:16].upper()}"
            inst = self._instrument(o.trading_symbol)
            exchange = o.exchange or (inst.exchange if inst else "NSE")

            # Resting stop orders do not fill on arrival.
            if o.order_type in (OrderType.SL, OrderType.SL_M):
                order = Order(
                    order_id=oid, trading_symbol=o.trading_symbol, side=o.side,
                    quantity=o.quantity, filled_quantity=0, status=OrderStatus.OPEN,
                    order_type=o.order_type, price=o.price, trigger_price=o.trigger_price,
                    client_id=o.client_id, tag=o.tag, exchange=exchange, segment=o.segment,
                    created_at=datetime.now(UTC),
                )
                self._orders[oid] = order
                self._by_ref[o.client_id] = oid
                self._emit(order)
                return oid

            cost = leg_cost(price, o.quantity, o.side.value, exchange=exchange, rates=self._rates)
            notional = price * o.quantity

            if o.side is Side.BUY and notional + cost.total > self._funds + 1e-9:
                raise BrokerError(
                    f"insufficient sim funds: need {notional + cost.total:.2f}, have {self._funds:.2f}"
                )

            order = Order(
                order_id=oid, trading_symbol=o.trading_symbol, side=o.side,
                quantity=o.quantity, filled_quantity=o.quantity, status=OrderStatus.FILLED,
                order_type=o.order_type, price=o.price, trigger_price=o.trigger_price,
                average_price=price, client_id=o.client_id, tag=o.tag,
                exchange=exchange, segment=o.segment, created_at=datetime.now(UTC),
            )
            self._orders[oid] = order
            self._by_ref[o.client_id] = oid
            self._apply_fill(o.trading_symbol, o.side, o.quantity, price, exchange, cost.total)
            self._emit(order)
            log.info("sim fill", extra={
                "symbol": o.trading_symbol, "side": o.side.value, "qty": o.quantity,
                "price": price, "costs": cost.total,
            })
            return oid

    def _apply_fill(self, symbol: str, side: Side, qty: int, price: float,
                    exchange: str, costs: float) -> None:
        signed = qty if side is Side.BUY else -qty
        pos = self._positions.get(symbol)
        self._costs_paid += costs
        self._funds -= costs

        if pos is None or pos.quantity == 0:
            inst = self._instrument(symbol)
            self._positions[symbol] = Position(
                trading_symbol=symbol, quantity=signed, average_price=price,
                last_price=price, lot_size=inst.lot_size if inst else 0,
                exchange=exchange, segment="FNO",
            )
            self._funds -= price * qty if side is Side.BUY else -price * qty
            return

        if (pos.quantity > 0) == (signed > 0):
            total = pos.quantity + signed
            pos.average_price = (pos.average_price * pos.quantity + price * signed) / total
            pos.quantity = total
            self._funds -= price * qty
        else:
            closing = min(abs(signed), abs(pos.quantity))
            gross = (price - pos.average_price) * closing * (1 if pos.quantity > 0 else -1)
            self._realized += gross
            pos.realized_pnl += gross
            self._funds += pos.average_price * closing + gross
            pos.quantity += signed
            if pos.quantity == 0:
                pos.average_price = 0.0
        pos.last_price = price

    def modify_order(self, oid: OrderId, o: OrderPatch, **_: Any) -> None:
        with self._lock:
            order = self._orders.get(oid)
            if order is None:
                raise BrokerError(f"unknown sim order {oid}")
            if order.is_terminal:
                raise BrokerError(f"cannot modify terminal order {oid} ({order.status.value})")
            if o.quantity is not None:
                order.quantity = int(o.quantity)
            if o.price is not None:
                order.price = float(o.price)
            if o.trigger_price is not None:
                order.trigger_price = float(o.trigger_price)
            self._emit(order)

    def cancel_order(self, oid: OrderId, **_: Any) -> None:
        with self._lock:
            order = self._orders.get(oid)
            if order is None:
                raise BrokerError(f"unknown sim order {oid}")
            if order.is_terminal:
                return
            order.status = OrderStatus.CANCELLED
            self._emit(order)

    def square_off_all(self) -> list[OrderId]:
        out: list[OrderId] = []
        with self._lock:
            open_positions = [p for p in self._positions.values() if p.is_open]
            # Cancel resting stops first so they cannot fire after we are flat.
            for order in list(self._orders.values()):
                if order.status is OrderStatus.OPEN and order.order_type in (
                    OrderType.SL, OrderType.SL_M
                ):
                    order.status = OrderStatus.CANCELLED
                    self._emit(order)

        for p in open_positions:
            side = Side.SELL if p.quantity > 0 else Side.BUY
            req = OrderRequest(
                trading_symbol=p.trading_symbol, exchange=p.exchange or "NSE",
                segment=p.segment or "FNO", side=side, quantity=abs(p.quantity),
                order_type=OrderType.MARKET, client_id=f"sq-{uuid.uuid4().hex[:12]}",
                tag="squareoff",
            )
            try:
                out.append(self.place_order(req))
            except BrokerError as exc:
                log.error("sim square-off leg failed",
                          extra={"symbol": p.trading_symbol, "error": str(exc)[:160]})
        return out

    def trigger_stops(self) -> list[Order]:
        """Fire resting SL orders whose trigger has been touched. Called by the tick loop."""
        fired: list[Order] = []
        with self._lock:
            for order in list(self._orders.values()):
                if order.status is not OrderStatus.OPEN:
                    continue
                if order.order_type not in (OrderType.SL, OrderType.SL_M):
                    continue
                ltp = self._ltp(order.trading_symbol)
                if ltp <= 0:
                    continue
                hit = ltp <= order.trigger_price if order.side is Side.SELL \
                    else ltp >= order.trigger_price
                if not hit:
                    continue
                price = round(max(0.05, ltp - self._tick if order.side is Side.SELL
                                  else ltp + self._tick), 2)
                cost = leg_cost(price, order.quantity, order.side.value,
                                exchange=order.exchange or "NSE", rates=self._rates)
                order.status = OrderStatus.FILLED
                order.filled_quantity = order.quantity
                order.average_price = price
                self._apply_fill(order.trading_symbol, order.side, order.quantity,
                                 price, order.exchange or "NSE", cost.total)
                fired.append(order)
                self._emit(order)
                log.info("sim stop fired",
                         extra={"symbol": order.trading_symbol, "price": price})
        return fired

    def mark(self, prices: dict[str, float]) -> None:
        with self._lock:
            for sym, px in prices.items():
                pos = self._positions.get(sym)
                if pos and pos.is_open:
                    pos.last_price = float(px)
                    pos.unrealized_pnl = (pos.last_price - pos.average_price) * pos.quantity

    # ------------------------------------------------------------------ state
    def get_orders(self, **kw: Any) -> list[Order]:
        """Simulated orders PLUS the real broker's, so guardian is not blind.

        In PAPER mode the guardian, prime() and the reconciler all read through this
        adapter. Returning only the simulated book meant a real manual trade in the
        Groww app was never detected during the very phase the build plan mandates
        for guardian drills.
        """
        with self._lock:
            mine = list(self._orders.values())
        if self._data is None:
            return mine
        try:
            real = self._data.get_orders(**kw)
        except Exception as exc:
            log.warning("sim: live order passthrough failed",
                        extra={"error": str(exc)[:140]})
            return mine
        known = {o.order_id for o in mine}
        return mine + [o for o in real if o.order_id not in known]

    def get_positions(self, **kw: Any) -> list[Position]:
        """Simulated positions PLUS real ones the simulator does not know about."""
        with self._lock:
            mine = list(self._positions.values())
        if self._data is None:
            return mine
        try:
            real = self._data.get_positions(**kw)
        except Exception as exc:
            log.warning("sim: live position passthrough failed",
                        extra={"error": str(exc)[:140]})
            return mine
        known = {p.trading_symbol for p in mine}
        return mine + [p for p in real if p.trading_symbol not in known and p.is_open]

    def get_funds(self) -> Funds:
        with self._lock:
            return Funds(available=round(self._funds, 2), used=0.0,
                         total=round(self._funds, 2),
                         raw={"realized": round(self._realized, 2),
                              "costs_paid": round(self._costs_paid, 2)})

    @property
    def realized(self) -> float:
        return round(self._realized, 2)

    @property
    def costs_paid(self) -> float:
        return round(self._costs_paid, 2)

    # ------------------------------------------------------------------ data (delegated)
    def get_instruments(self, **kw: Any) -> InstrumentMaster:
        if self._data is None:
            raise BrokerError("sim has no data source for instruments")
        self._instruments = self._data.get_instruments(**kw)
        return self._instruments

    def get_quote(self, keys: list[str], **kw: Any) -> dict[str, Quote]:
        if self._data is None:
            return {}
        return self._data.get_quote(keys, **kw)

    def get_ltp_batch(self, symbols: list[str], **kw: Any) -> dict[str, float]:
        if self._data is None:
            return {}
        return self._data.get_ltp_batch(symbols, **kw)

    def get_candles(self, key: str, tf: str = "1m", span: int = 375, **kw: Any) -> list[Candle]:
        if self._data is None:
            return []
        return self._data.get_candles(key, tf, span, **kw)

    def stream_ticks(self, keys: list[str], on_tick: Callable[[Quote], None]) -> Subscription:
        if self._data is None:
            return _NullSub()
        return self._data.stream_ticks(keys, on_tick)

    def stream_orders(self, on_order_event: Callable[[Order], None]) -> Subscription:
        self._order_cb = on_order_event
        return _NullSub()

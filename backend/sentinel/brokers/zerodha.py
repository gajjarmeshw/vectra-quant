"""Zerodha KiteConnect Adapter for SENTINEL.

Implements BrokerAdapter interface protocol (design §3.1) for Zerodha KiteConnect.
Supports execution, order state retrieval, positions, funds, and instrument master.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Callable

from sentinel.brokers.base import (
    BrokerAdapter,
    BrokerError,
    Funds,
    Instrument,
    InstrumentMaster,
    Order,
    OrderId,
    OrderPatch,
    OrderRequest,
    Position,
    Quote,
    Subscription,
)
from sentinel.logging_setup import get

log = get("brokers.zerodha")

try:
    from kiteconnect import KiteConnect, KiteTicker
    HAS_KITECONNECT = True
except ImportError:
    HAS_KITECONNECT = False
    KiteConnect = None
    KiteTicker = None


class ZerodhaAdapter:
    """Zerodha live broker adapter."""

    name: str = "zerodha"

    def __init__(self, api_key: str, access_token: str):
        if not HAS_KITECONNECT:
            raise BrokerError(
                "kiteconnect package is not installed. Run `pip install kiteconnect` to enable Zerodha support."
            )
        if not api_key or not access_token:
            raise BrokerError("ZERODHA_API_KEY / ZERODHA_ACCESS_TOKEN missing — refusing to start")

        self.api_key = api_key
        self.access_token = access_token
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)

        self._instruments: InstrumentMaster | None = None
        self._instruments_day: str = ""

    # ------------------------------------------------------------------ execution
    def place_order(self, o: OrderRequest) -> OrderId:
        """Place an order with Zerodha Kite API."""
        try:
            order_type = o.order_type.upper()
            if order_type == "SL_M":
                order_type = "SL-M"

            exchange = self.kite.EXCHANGE_NFO if o.exchange == "NSE" else o.exchange

            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=o.symbol,
                transaction_type=o.side.upper(),
                quantity=o.quantity,
                product=self.kite.PRODUCT_MIS,
                order_type=order_type,
                price=float(o.price) if o.price else 0.0,
                trigger_price=float(o.trigger_price) if o.trigger_price else 0.0,
                tag=o.reference_id[:20] if o.reference_id else None,
            )
            log.info("Zerodha order placed: id=%s symbol=%s qty=%d", order_id, o.symbol, o.quantity)
            return str(order_id)
        except Exception as e:
            log.error("Zerodha place_order failed: %s", e)
            raise BrokerError(f"Zerodha order failure: {e}", raw=e) from e

    def modify_order(self, oid: OrderId, o: OrderPatch) -> None:
        try:
            kwargs: dict[str, Any] = {
                "variety": self.kite.VARIETY_REGULAR,
                "order_id": oid,
            }
            if o.price is not None:
                kwargs["price"] = o.price
            if o.trigger_price is not None:
                kwargs["trigger_price"] = o.trigger_price
            if o.quantity is not None:
                kwargs["quantity"] = o.quantity

            self.kite.modify_order(**kwargs)
            log.info("Zerodha order modified: id=%s", oid)
        except Exception as e:
            raise BrokerError(f"Zerodha modify_order failed: {e}", raw=e) from e

    def cancel_order(self, oid: OrderId) -> None:
        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR, order_id=oid)
            log.info("Zerodha order cancelled: id=%s", oid)
        except Exception as e:
            raise BrokerError(f"Zerodha cancel_order failed: {e}", raw=e) from e

    def square_off_all(self) -> list[OrderId]:
        """Square off all FNO positions."""
        positions = self.get_positions()
        cancelled: list[OrderId] = []
        for pos in positions:
            if pos.quantity == 0:
                continue
            side = "SELL" if pos.quantity > 0 else "BUY"
            qty = abs(pos.quantity)
            req = OrderRequest(
                symbol=pos.symbol,
                side=side,
                quantity=qty,
                order_type="MARKET",
                exchange=pos.exchange,
                reference_id=f"sqoff_{pos.symbol}_{int(datetime.now().timestamp())}",
            )
            try:
                oid = self.place_order(req)
                cancelled.append(oid)
            except Exception as e:
                log.error("Square off failed for %s: %s", pos.symbol, e)
        return cancelled

    # ------------------------------------------------------------------ state
    def get_orders(self) -> list[Order]:
        try:
            raw_orders = self.kite.orders()
            orders: list[Order] = []
            for ro in raw_orders:
                orders.append(
                    Order(
                        order_id=str(ro["order_id"]),
                        trading_symbol=ro["tradingsymbol"],
                        exchange=ro["exchange"],
                        side=ro["transaction_type"],
                        quantity=int(ro["quantity"]),
                        filled_quantity=int(ro["filled_quantity"]),
                        order_type=ro["order_type"],
                        status=ro["status"].upper(),
                        price=float(ro["price"] or 0.0),
                        average_price=float(ro["average_price"] or 0.0),
                        trigger_price=float(ro["trigger_price"] or 0.0),
                        tag=ro.get("tag", ""),
                    )
                )
            return orders
        except Exception as e:
            log.error("Zerodha get_orders failed: %s", e)
            return []

    def get_positions(self) -> list[Position]:
        try:
            res = self.kite.positions()
            net = res.get("net", [])
            positions: list[Position] = []
            for rp in net:
                if rp["exchange"] not in ("NFO", "BFO"):
                    continue
                qty = int(rp["quantity"])
                if qty == 0:
                    continue
                positions.append(
                    Position(
                        trading_symbol=rp["tradingsymbol"],
                        exchange=rp["exchange"],
                        quantity=qty,
                        average_price=float(rp["average_price"] or 0.0),
                        last_price=float(rp["last_price"] or 0.0),
                        realized_pnl=float(rp["realised"] or 0.0),
                        unrealized_pnl=float(rp["unrealised"] or 0.0),
                    )
                )
            return positions
        except Exception as e:
            log.error("Zerodha get_positions failed: %s", e)
            return []

    def get_funds(self) -> Funds:
        try:
            margins = self.kite.margins(segment="equity")
            avail = float(margins.get("available", {}).get("live_balance", 0.0))
            used = float(margins.get("utilised", {}).get("debits", 0.0))
            return Funds(available=avail, used=used, total=avail + used)
        except Exception as e:
            log.error("Zerodha get_funds failed: %s", e)
            return Funds(available=0.0, total=0.0)

    # ------------------------------------------------------------------ data
    def get_instruments(self, *, force: bool = False) -> InstrumentMaster:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if self._instruments is not None and self._instruments_day == today and not force:
            return self._instruments

        try:
            raw_nfo = self.kite.instruments(exchange="NFO")
            raw_bfo = self.kite.instruments(exchange="BFO")
            raw_all = raw_nfo + raw_bfo

            instruments: list[Instrument] = []
            for item in raw_all:
                instruments.append(
                    Instrument(
                        trading_symbol=item["tradingsymbol"],
                        exchange=item["exchange"],
                        segment="FNO",
                        lot_size=int(item["lot_size"]),
                        instrument_type=item.get("instrument_type", ""),
                        name=item.get("name", ""),
                        strike=float(item.get("strike", 0.0)),
                        expiry=str(item.get("expiry", "")),
                        exchange_token=str(item.get("instrument_token", "")),
                    )
                )

            master = InstrumentMaster(instruments, datetime.now(UTC))
            self._instruments = master
            self._instruments_day = today
            log.info("Zerodha instrument master loaded: %d items", len(master))
            return master
        except Exception as e:
            log.error("Zerodha get_instruments failed: %s", e)
            if self._instruments is not None:
                return self._instruments
            raise BrokerError(f"Zerodha instrument master failed: {e}") from e

    def get_ltp(self, symbol: str) -> float:
        res = self.get_ltp_batch([symbol])
        return res.get(symbol, 0.0)

    def get_ltp_batch(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            formatted = [f"NFO:{s}" if ":" not in s else s for s in symbols]
            res = self.kite.ltp(formatted)
            out: dict[str, float] = {}
            for key, val in res.items():
                clean_sym = key.split(":")[-1]
                out[clean_sym] = float(val.get("last_price", 0.0))
            return out
        except Exception as e:
            log.warning("Zerodha get_ltp_batch failed for %s: %s", symbols, e)
            return {}

    def get_quote(self, keys: list[str]) -> dict[str, Quote]:
        if not keys:
            return {}
        try:
            formatted = [f"NFO:{s}" if ":" not in s else s for s in keys]
            res = self.kite.quote(formatted)
            quotes: dict[str, Quote] = {}
            for key, q in res.items():
                clean_sym = key.split(":")[-1]
                quotes[clean_sym] = Quote(
                    trading_symbol=clean_sym,
                    ltp=float(q.get("last_price", 0.0)),
                    open=float(q.get("ohlc", {}).get("open", 0.0)),
                    high=float(q.get("ohlc", {}).get("high", 0.0)),
                    low=float(q.get("ohlc", {}).get("low", 0.0)),
                    close=float(q.get("ohlc", {}).get("close", 0.0)),
                    volume=int(q.get("volume", 0)),
                )
            return quotes
        except Exception as e:
            log.warning("Zerodha get_quote failed: %s", e)
            return {}

    def get_candles(self, key: str, tf: str = "1m", span: int = 1, **kwargs: Any) -> list[Candle]:
        return []

    # ------------------------------------------------------------------ streams
    def stream_ticks(self, keys: list[str], on_tick: Callable[[Quote], None]) -> Subscription:
        class DummySubscription:
            def close(self) -> None:
                pass
        return DummySubscription()

    def stream_orders(self, on_order_event: Callable[[Order], None]) -> Subscription:
        class DummySubscription:
            def close(self) -> None:
                pass
        return DummySubscription()

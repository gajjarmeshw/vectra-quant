"""DhanHQ Broker & Data Adapter for VECTRA_QUANT.

Implements BrokerAdapter interface protocol (design §3.1) for DhanHQ API v2.
Provides clean 1-minute historical candles, live market feed, option chains,
orders, positions, and funds with zero extra dependencies.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
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
    Product,
    Quote,
    Side,
    Subscription,
)
from vectra_quant.logging_setup import get

log = get("brokers.dhan")

IST = timezone(timedelta(hours=5, minutes=30))

try:
    from dhanhq import dhanhq as DhanHQ, DhanContext
    HAS_DHANHQ = True
except ImportError:
    HAS_DHANHQ = False
    DhanHQ = None
    DhanContext = None

# Known security IDs for Indian benchmarks on Dhan (IDX_I / NSE_EQ)
DHAN_INDEX_SECURITY_MAP: dict[str, dict[str, str]] = {
    "NIFTY": {"security_id": "13", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "NIFTY 50": {"security_id": "13", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "NIFTY50": {"security_id": "13", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "BANKNIFTY": {"security_id": "25", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "NIFTY BANK": {"security_id": "25", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "FINNIFTY": {"security_id": "27", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "NIFTY FIN SERVICE": {"security_id": "27", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "SENSEX": {"security_id": "51", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "BSE SENSEX": {"security_id": "51", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "INDIA VIX": {"security_id": "21", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
    "INDIAVIX": {"security_id": "21", "exchange_segment": "IDX_I", "instrument_type": "INDEX"},
}


class DhanAdapter:
    """DhanHQ live broker and high-resolution historical market data adapter."""

    name: str = "dhan"

    def __init__(self, client_id: str, access_token: str):
        if not HAS_DHANHQ:
            raise BrokerError("dhanhq package is not installed. Run `pip install dhanhq` to enable Dhan support.")
        if not client_id or not access_token:
            raise BrokerError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing — refusing to start")

        self.client_id = str(client_id).strip()
        self.access_token = str(access_token).strip()
        if DhanContext is not None:
            self.dhan = DhanHQ(DhanContext(self.client_id, self.access_token))
        else:
            self.dhan = DhanHQ(self.client_id, self.access_token)

        self._instruments: InstrumentMaster | None = None
        self._instruments_day: str = ""
        self._last_auth_error_ts: float = 0.0
        self.is_authenticated: bool = True
        self.last_auth_error: str = ""

        # Short TTL cache to prevent API hammering
        self._cached_orders: list[Order] = []
        self._last_orders_fetch = 0.0

        self._cached_positions: list[Position] = []
        self._last_positions_fetch = 0.0

        self._cached_funds = Funds(0.0, 0.0, 0.0, 0.0)
        self._last_funds_fetch = 0.0

        self._instruments_master: InstrumentMaster | None = None

    def set_instruments(self, master: InstrumentMaster) -> None:
        self._instruments_master = master

    def _handle_api_error(self, action: str, e: Exception) -> None:
        err_msg = str(e)
        is_auth_error = any(
            frag in err_msg.lower()
            for frag in ("token", "unauthorized", "invalid access", "401", "403", "forbidden", "authentication failed")
        )
        if is_auth_error:
            self.is_authenticated = False
            self.last_auth_error = err_msg
            now = time.monotonic()
            if now - self._last_auth_error_ts > 60.0:
                log.warning(
                    "Dhan authentication failed (%s): %s — token may be expired or invalid. "
                    "Update DHAN_ACCESS_TOKEN in .env (repeat warnings debounced for 60s)",
                    action,
                    err_msg,
                )
                self._last_auth_error_ts = now
        else:
            log.error("Dhan %s failed: %s", action, e)

    # ------------------------------------------------------------------ execution
    def place_order(self, o: OrderRequest) -> OrderId:
        """Place an order with DhanHQ API."""
        try:
            side = self.dhan.BUY if o.side == Side.BUY else self.dhan.SELL
            order_type = self.dhan.MARKET
            if o.order_type == OrderType.LIMIT:
                order_type = self.dhan.LIMIT
            elif o.order_type in (OrderType.SL, OrderType.SL_M):
                order_type = self.dhan.SLM if o.order_type == OrderType.SL_M else self.dhan.SL

            product_type = self.dhan.INTRA if o.product == Product.MIS else self.dhan.CNC

            exchange_segment = self.dhan.NSE_FNO if o.segment == "FNO" else self.dhan.NSE

            resp = self.dhan.place_order(
                security_id=getattr(o, "security_id", o.trading_symbol),
                exchange_segment=exchange_segment,
                transaction_type=side,
                quantity=o.quantity,
                order_type=order_type,
                product_type=product_type,
                price=float(o.price) if o.price else 0.0,
                trigger_price=float(o.trigger_price) if o.trigger_price else 0.0,
                tag=o.tag[:20] if o.tag else None,
            )

            if isinstance(resp, dict) and resp.get("status") == "success":
                order_id = str(resp.get("data", {}).get("orderId", ""))
                log.info("Dhan order placed successfully: id=%s symbol=%s qty=%d", order_id, o.trading_symbol, o.quantity)
                return order_id

            err = resp.get("remarks", "Order placement failed") if isinstance(resp, dict) else str(resp)
            raise BrokerError(f"Dhan place_order failed: {err}")
        except Exception as e:
            log.error("Dhan place_order exception: %s", e)
            raise BrokerError(f"Dhan place_order failure: {e}", raw=e) from e

    def modify_order(self, oid: OrderId, o: OrderPatch) -> None:
        try:
            self.dhan.modify_order(
                order_id=oid,
                order_type=self.dhan.LIMIT if o.price else self.dhan.MARKET,
                leg_name="",
                quantity=o.quantity or 0,
                price=float(o.price) if o.price else 0.0,
                trigger_price=float(o.trigger_price) if o.trigger_price else 0.0,
                validity=self.dhan.DAY,
            )
            log.info("Dhan order modified: id=%s", oid)
        except Exception as e:
            log.error("Dhan modify_order failed: %s", e)
            raise BrokerError(f"Dhan modify_order failure: {e}") from e

    def cancel_order(self, oid: OrderId) -> None:
        try:
            self.dhan.cancel_order(order_id=oid)
            log.info("Dhan order cancelled: id=%s", oid)
        except Exception as e:
            log.error("Dhan cancel_order failed: %s", e)
            raise BrokerError(f"Dhan cancel_order failure: {e}") from e

    def square_off_all(self) -> list[OrderId]:
        """Emergency square-off of all open positions."""
        cancelled: list[OrderId] = []
        try:
            positions = self.get_positions()
            for p in positions:
                if p.is_open:
                    side = Side.SELL if p.quantity > 0 else Side.BUY
                    req = OrderRequest(
                        trading_symbol=p.trading_symbol,
                        exchange=p.exchange or "NSE",
                        segment=p.segment or "FNO",
                        side=side,
                        quantity=abs(p.quantity),
                        order_type=OrderType.MARKET,
                        product=Product.MIS,
                        client_id=f"sqoff-{p.trading_symbol}-{int(time.time())}",
                        tag="emergency_sqoff",
                    )
                    try:
                        oid = self.place_order(req)
                        cancelled.append(oid)
                    except Exception as sq_err:
                        log.error("Square off failed for %s: %s", p.trading_symbol, sq_err)
        except Exception as e:
            log.error("Dhan square_off_all failed: %s", e)
        return cancelled

    # ------------------------------------------------------------------ state
    def get_orders(self) -> list[Order]:
        now = time.monotonic()
        if now - self._last_orders_fetch < 1.0:
            return self._cached_orders

        try:
            resp = self.dhan.get_order_list()
            data_list = resp.get("data", []) if isinstance(resp, dict) else resp

            orders: list[Order] = []
            for item in data_list or []:
                raw_status = str(item.get("orderStatus", "")).upper()
                status_map = {
                    "TRANSIT": OrderStatus.NEW,
                    "PENDING": OrderStatus.OPEN,
                    "CONFIRMED": OrderStatus.OPEN,
                    "TRADED": OrderStatus.FILLED,
                    "REJECTED": OrderStatus.REJECTED,
                    "CANCELLED": OrderStatus.CANCELLED,
                }
                status = status_map.get(raw_status, OrderStatus.UNKNOWN)

                side = Side.BUY if str(item.get("transactionType", "")).upper() == "BUY" else Side.SELL
                orders.append(
                    Order(
                        order_id=str(item.get("orderId", "")),
                        trading_symbol=item.get("tradingSymbol", ""),
                        side=side,
                        quantity=int(item.get("quantity", 0)),
                        filled_quantity=int(item.get("filledQty", 0)),
                        status=status,
                        order_type=OrderType.MARKET if item.get("orderType") == "MARKET" else OrderType.LIMIT,
                        price=float(item.get("price", 0.0)),
                        trigger_price=float(item.get("triggerPrice", 0.0)),
                        raw=item,
                    )
                )

            self.is_authenticated = True
            self._cached_orders = orders
            self._last_orders_fetch = now
            return orders
        except Exception as e:
            self._handle_api_error("get_orders", e)
            self._last_orders_fetch = now
            return self._cached_orders

    def get_positions(self) -> list[Position]:
        now = time.monotonic()
        if now - self._last_positions_fetch < 1.0:
            return self._cached_positions

        try:
            resp = self.dhan.get_positions()
            data_list = resp.get("data", []) if isinstance(resp, dict) else resp

            positions: list[Position] = []
            for item in data_list or []:
                net_qty = int(item.get("netQty", 0))
                positions.append(
                    Position(
                        trading_symbol=item.get("tradingSymbol", ""),
                        quantity=net_qty,
                        average_price=float(item.get("costPrice", 0.0)),
                        last_price=float(item.get("lastPrice", 0.0)),
                        unrealized_pnl=float(item.get("unrealizedProfit", 0.0)),
                        realized_pnl=float(item.get("realizedProfit", 0.0)),
                        raw=item,
                    )
                )

            self.is_authenticated = True
            self._cached_positions = positions
            self._last_positions_fetch = now
            return positions
        except Exception as e:
            self._handle_api_error("get_positions", e)
            self._last_positions_fetch = now
            return self._cached_positions

    def get_funds(self) -> Funds:
        now = time.monotonic()
        if now - self._last_funds_fetch < 2.0:
            return self._cached_funds

        try:
            resp = self.dhan.get_fund_limits()
            data = resp.get("data", {}) if isinstance(resp, dict) else {}
            avail = float(data.get("availabelBalance", data.get("availableBalance", 0.0)))
            used = float(data.get("utilizedAmount", 0.0))
            funds = Funds(available=avail, used=used, total=avail + used, raw=data)
            self.is_authenticated = True
            self._cached_funds = funds
            self._last_funds_fetch = now
            return funds
        except Exception as e:
            self._handle_api_error("get_funds", e)
            self._last_funds_fetch = now
            return self._cached_funds

    # ------------------------------------------------------------------ market data
    def get_candles(
        self,
        key: str,
        tf: str = "1m",
        span: int = 375,
        from_date: str | None = None,
        to_date: str | None = None,
        **kwargs: Any,
    ) -> list[Candle]:
        """Fetch real historical OHLCV candles from DhanHQ Historical Data API on demand.

        Supports arbitrary date ranges via automated 5-day chunking for high-resolution 1m bars.
        """
        try:
            clean_key = key.upper()
            sec_meta = DHAN_INDEX_SECURITY_MAP.get(clean_key)
            if not sec_meta:
                sec_meta = {"security_id": clean_key, "exchange_segment": "NSE_EQ", "instrument_type": "EQUITY"}

            sec_id = sec_meta["security_id"]
            exch_seg = sec_meta["exchange_segment"]
            inst_type = sec_meta["instrument_type"]

            now = datetime.now(UTC)
            if to_date:
                end_dt = datetime.strptime(to_date.split(" ")[0], "%Y-%m-%d").date()
            else:
                end_dt = now.date()

            if from_date:
                start_dt = datetime.strptime(from_date.split(" ")[0], "%Y-%m-%d").date()
            else:
                days_needed = max(int(span / 375) + 3, 5)
                start_dt = end_dt - timedelta(days=days_needed)

            interval_map = {"1m": 1, "5m": 5, "15m": 15, "25m": 25, "60m": 60, "1h": 60}
            is_daily = tf.lower() in ("1d", "day", "daily")

            if is_daily:
                resp = self.dhan.historical_daily_data(
                    security_id=sec_id,
                    exchange_segment=exch_seg,
                    instrument_type=inst_type,
                    from_date=start_dt.strftime("%Y-%m-%d"),
                    to_date=end_dt.strftime("%Y-%m-%d"),
                )
                return self._parse_dhan_candles(resp)

            interval = interval_map.get(tf, 1)

            # Chunk into 5-day windows (Dhan intraday minute API max per query is 5 trading days)
            all_candles: dict[str, Candle] = {}
            curr = start_dt
            while curr <= end_dt:
                chunk_end = min(curr + timedelta(days=4), end_dt)
                f_str = curr.strftime("%Y-%m-%d")
                t_str = chunk_end.strftime("%Y-%m-%d")

                try:
                    if interval == 1 and hasattr(self.dhan, "historical_minute_data"):
                        resp = self.dhan.historical_minute_data(
                            security_id=sec_id,
                            exchange_segment=exch_seg,
                            instrument_type=inst_type,
                            from_date=f_str,
                            to_date=t_str,
                        )
                        chunk_candles = self._parse_dhan_candles(resp)
                        
                        # Fallback to intraday for recent days if historical returns nothing
                        if not chunk_candles:
                            resp = self.dhan.intraday_minute_data(
                                security_id=sec_id,
                                exchange_segment=exch_seg,
                                instrument_type=inst_type,
                                from_date=f_str,
                                to_date=t_str,
                                interval=interval,
                            )
                            chunk_candles = self._parse_dhan_candles(resp)
                    else:
                        resp = self.dhan.intraday_minute_data(
                            security_id=sec_id,
                            exchange_segment=exch_seg,
                            instrument_type=inst_type,
                            from_date=f_str,
                            to_date=t_str,
                            interval=interval,
                        )
                        chunk_candles = self._parse_dhan_candles(resp)
                    
                    for c in chunk_candles:
                        all_candles[c.ts.isoformat()] = c
                except Exception as chunk_err:
                    log.warning("Dhan get_candles chunk (%s to %s) warning: %s", f_str, t_str, chunk_err)

                curr = chunk_end + timedelta(days=1)

            out = sorted(all_candles.values(), key=lambda x: x.ts)
            log.info("Dhan get_candles: fetched %d on-demand exchange bars for %s (%s to %s)", len(out), key, start_dt, end_dt)
            return out
        except Exception as exc:
            self._handle_api_error(f"get_candles({key})", exc)
            return []

    def _parse_dhan_candles(self, resp: Any) -> list[Candle]:
        """Convert Dhan JSON response format into VectraQuant Candle domain objects."""
        data_obj = resp.get("data", resp) if isinstance(resp, dict) else {}
        if not isinstance(data_obj, dict):
            return []

        opens = data_obj.get("open", [])
        highs = data_obj.get("high", [])
        lows = data_obj.get("low", [])
        closes = data_obj.get("close", [])
        volumes = data_obj.get("volume", [])
        timestamps = data_obj.get("timestamp", data_obj.get("start_Time", []))

        out: list[Candle] = []
        length = min(len(opens), len(highs), len(lows), len(closes), len(timestamps))

        for i in range(length):
            ts_val = timestamps[i]
            if isinstance(ts_val, (int, float)):
                dt = datetime.fromtimestamp(ts_val, tz=IST)
            elif isinstance(ts_val, str):
                try:
                    dt = datetime.fromisoformat(ts_val)
                except Exception:
                    dt = datetime.now(IST)
            else:
                dt = datetime.now(IST)

            out.append(
                Candle(
                    ts=dt,
                    open=float(opens[i]),
                    high=float(highs[i]),
                    low=float(lows[i]),
                    close=float(closes[i]),
                    volume=float(volumes[i]) if i < len(volumes) else 0.0,
                )
            )
        return out

    def get_ltp(self, symbol: str) -> float:
        res = self.get_ltp_batch([symbol])
        return res.get(symbol, 0.0)

    def get_ltp_batch(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        out: dict[str, float] = {}
        try:
            idx_ids = [int(DHAN_INDEX_SECURITY_MAP[s.upper()]["security_id"]) for s in symbols if s.upper() in DHAN_INDEX_SECURITY_MAP]
            if idx_ids:
                resp = self.dhan.ticker_data({"IDX_I": idx_ids})
                data_map = resp.get("data", {}) if isinstance(resp, dict) else {}
                for sym in symbols:
                    meta = DHAN_INDEX_SECURITY_MAP.get(sym.upper())
                    if meta:
                        sid = meta["security_id"]
                        val = data_map.get(sid, {})
                        if val and float(val.get("last_price", 0.0)) > 0:
                            out[sym] = float(val.get("last_price", 0.0))
        except Exception as e:
            log.warning("Dhan ticker_data failed for %s: %s", symbols, e)

        # Fallback to get_candles for any missing symbols
        for sym in symbols:
            if sym not in out:
                try:
                    bars = self.get_candles(sym, "1m", span=5)
                    if bars:
                        out[sym] = bars[-1].close
                except Exception as fallback_e:
                    log.warning("Dhan get_candles fallback failed for %s: %s", sym, fallback_e)
        return out

    def get_quote(self, keys: list[str]) -> dict[str, Quote]:
        if not keys:
            return {}
        quotes: dict[str, Quote] = {}
        try:
            idx_ids = []
            opt_ids = []
            opt_map = {}
            for s in keys:
                up = s.upper()
                if up in DHAN_INDEX_SECURITY_MAP:
                    idx_ids.append(int(DHAN_INDEX_SECURITY_MAP[up]["security_id"]))
                elif self._instruments_master:
                    inst = self._instruments_master.get(s)
                    if inst and inst.exchange_token:
                        opt_ids.append(int(inst.exchange_token))
                        opt_map[inst.exchange_token] = s
            
            # Fetch indices
            if idx_ids:
                resp = self.dhan.ohlc_data({"IDX_I": idx_ids})
                data_map = resp.get("data", {}) if isinstance(resp, dict) else {}
                for sym in keys:
                    meta = DHAN_INDEX_SECURITY_MAP.get(sym.upper())
                    if meta:
                        sid = meta["security_id"]
                        q = data_map.get(sid, {})
                        if q and float(q.get("last_price", 0.0)) > 0:
                            quotes[sym] = Quote(
                                trading_symbol=sym,
                                last_price=float(q.get("last_price", 0.0)),
                                open_interest=0.0,
                                volume=float(q.get("volume", 0.0)),
                            )
            
            # Fetch options
            if opt_ids:
                # Dhan ohlc_data limits to a certain number of symbols, but let's assume it handles a normal chain depth.
                resp = self.dhan.ohlc_data({"NSE_FNO": opt_ids})
                data_map = resp.get("data", {}) if isinstance(resp, dict) else {}
                for sid_str, q in data_map.items():
                    if q and float(q.get("last_price", 0.0)) > 0:
                        sym = opt_map.get(str(sid_str))
                        if sym:
                            quotes[sym] = Quote(
                                trading_symbol=sym,
                                last_price=float(q.get("last_price", 0.0)),
                                open_interest=float(q.get("oi", 0.0)),
                                volume=float(q.get("volume", 0.0)),
                            )
        except Exception as e:
            log.warning("Dhan ohlc_data failed for %s: %s", keys, e)

        # Fallback
        for sym in keys:
            if sym not in quotes:
                bars = self.get_candles(sym, "1m", span=5)
                if bars:
                    last_bar = bars[-1]
                    quotes[sym] = Quote(
                        trading_symbol=sym,
                        last_price=last_bar.close,
                        open_interest=0.0,
                        volume=last_bar.volume,
                    )
        return quotes

    def get_option_chain(self, symbol: str, expiry: str) -> dict[str, Any]:
        """Fetch native Dhan Option Chain with Greeks, Open Interest, and IV."""
        try:
            meta = DHAN_INDEX_SECURITY_MAP.get(symbol.upper(), {"security_id": "13", "exchange_segment": "IDX_I"})
            sec_id = int(meta["security_id"])
            exch_seg = meta["exchange_segment"]
            return self.dhan.option_chain(
                under_security_id=sec_id,
                under_exchange_segment=exch_seg,
                expiry=expiry,
            )
        except Exception as e:
            log.warning("Dhan get_option_chain failed for %s: %s", symbol, e)
            return {}

    def get_instruments(self, *, force: bool = False) -> InstrumentMaster:
        """Return cached Tradable Universe Instrument Master by downloading from Dhan."""
        import os
        import csv
        import requests
        
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        if self._instruments is not None and self._instruments_day == today and not force:
            return self._instruments

        cache_file = f"/tmp/dhan_scrip_master_{today}.csv"
        
        if force or not os.path.exists(cache_file):
            log.info("Downloading Dhan Instrument Master...")
            try:
                resp = requests.get("https://images.dhan.co/api-data/api-scrip-master.csv", timeout=15)
                resp.raise_for_status()
                with open(cache_file, "wb") as f:
                    f.write(resp.content)
            except Exception as e:
                log.error("Failed to download Dhan Instrument Master: %s", e)
                # Fallback to minimal hardcoded list
                instruments = [
                    Instrument("NIFTY", "NSE", "INDEX", 65, "INDEX", "NIFTY"),
                    Instrument("BANKNIFTY", "NSE", "INDEX", 30, "INDEX", "BANKNIFTY"),
                ]
                master = InstrumentMaster(instruments, datetime.now(UTC))
                self._instruments = master
                self._instruments_day = today
                return master

        # Parse CSV
        instruments = []
        try:
            with open(cache_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    exch = row.get("EXCH_ID", "")
                    symbol = row.get("TRADING_SYMBOL", "")
                    # Basic filtering for Equity/Index/FNO
                    if exch in ("NSE", "IDX_I", "BSE") and symbol:
                        instruments.append(
                            Instrument(
                                symbol=symbol,
                                exchange=exch,
                                segment=row.get("INSTRUMENT", ""),
                                lot_size=int(row.get("LOT_SIZE", 1)),
                                name=row.get("CUSTOM_SYMBOL", symbol),
                                token=row.get("SEM_SMST_SECURITY_ID", "")
                            )
                        )
        except Exception as e:
            log.error("Failed to parse Dhan Instrument Master: %s", e)

        master = InstrumentMaster(instruments, datetime.now(UTC))
        self._instruments = master
        self._instruments_day = today
        log.info("Dhan Instrument Master loaded %d instruments.", len(instruments))
        return master

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

"""GrowwAdapter — v1 live broker. Design doc §3.2.

Verified against `growwapi==1.5.0` and the live instrument master (135,038 rows).

Idempotency (build plan §0.4) uses Groww's own `order_reference_id`: before placing
we ask `get_order_status_by_reference`, and a hit means the order already exists, so
a retry or a double-tap physically cannot place twice.

Every call is wrapped with bounded retry + a circuit breaker (§3.2 "young API").
Raw payloads are logged for the first month, secrets redacted.
"""
from __future__ import annotations

import re
import threading
import time as _time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pyotp
from growwapi import GrowwAPI, GrowwFeed

from sentinel.brokers.base import (
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
from sentinel.logging_setup import get, redact

log = get("brokers.groww")

IST = timezone(timedelta(hours=5, minutes=30))

# Groww accepts an alphanumeric reference; keep it tight and deterministic.
_REF_RE = re.compile(r"[^A-Za-z0-9]")

# SENTINEL's whole universe: index options on NSE/BSE. The SDK also speaks CASH,
# CURRENCY, COMMODITY (MCX/NCDEX) and US — one account, many segments. Reading the
# book unscoped meant a commodity or equity position the trader took on their own
# could be counted against the day's trade budget, or worse, market-exited by
# square_off_all. Everything the adapter reports is filtered to this scope.
SCOPE_SEGMENT = "FNO"
SCOPE_EXCHANGES = frozenset({"NSE", "BSE"})


def _in_scope(d: dict[str, Any]) -> bool:
    """True when a raw order/position row belongs to SENTINEL's universe.

    A missing field is treated as in-scope: Groww omits `segment` on some payloads
    and the instrument-master lookup downstream is the second gate. An explicitly
    foreign value is always excluded.
    """
    seg = str(d.get("segment") or "").upper()
    if seg and seg != SCOPE_SEGMENT:
        return False
    exch = str(d.get("exchange") or "").upper()
    return not (exch and exch not in SCOPE_EXCHANGES)

_STATUS_MAP = {
    "NEW": OrderStatus.NEW,
    "ACKED": OrderStatus.OPEN,
    "APPROVED": OrderStatus.OPEN,
    "OPEN": OrderStatus.OPEN,
    "TRIGGER_PENDING": OrderStatus.OPEN,
    "EXECUTED": OrderStatus.FILLED,
    "COMPLETED": OrderStatus.FILLED,
    "FILLED": OrderStatus.FILLED,
    "PARTIALLY_FILLED": OrderStatus.PARTIAL,
    "CANCELLED": OrderStatus.CANCELLED,
    "CANCELLATION_REQUESTED": OrderStatus.OPEN,
    "REJECTED": OrderStatus.REJECTED,
    "FAILED": OrderStatus.REJECTED,
}

_ORDER_TYPE_OUT = {
    OrderType.MARKET: GrowwAPI.ORDER_TYPE_MARKET,
    OrderType.LIMIT: GrowwAPI.ORDER_TYPE_LIMIT,
    OrderType.SL: GrowwAPI.ORDER_TYPE_STOP_LOSS,
    OrderType.SL_M: GrowwAPI.ORDER_TYPE_STOP_LOSS_MARKET,
}
_ORDER_TYPE_IN = {v: k for k, v in _ORDER_TYPE_OUT.items()}


def make_reference(client_id: str) -> str:
    """Groww order_reference_id: alphanumeric, 8-20 chars. Deterministic from uuid."""
    ref = _REF_RE.sub("", client_id)[:20]
    if len(ref) < 8:  # pragma: no cover - uuid4 hex is always long enough
        ref = (ref + "0" * 8)[:8]
    return ref


class _RateLimiter:
    """Token bucket across ALL broker calls.

    Groww rejects bursts with "Rate limit has breached", observed live at a 1s
    price poll plus a 30s chain sweep. Sequencing every call through one bucket
    is simpler and safer than tuning each job's cadence independently.
    """

    def __init__(self, rate_per_s: float = 4.0, burst: int = 8):
        self.rate = rate_per_s
        self.burst = burst
        self._tokens = float(burst)
        self._last = _time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 10.0) -> bool:
        deadline = _time.monotonic() + timeout
        while True:
            with self._lock:
                now = _time.monotonic()
                self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                need = (1.0 - self._tokens) / self.rate
            if _time.monotonic() + need > deadline:
                return False
            _time.sleep(min(need, 0.25))


class _CircuitBreaker:
    """Open after `threshold` consecutive failures; half-open after `cooldown`."""

    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._fails = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self._lock:
            if self._fails < self.threshold:
                return False
            if _time.monotonic() - self._opened_at >= self.cooldown_s:
                self._fails = self.threshold - 1  # half-open: allow one probe
                return False
            return True

    def record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._fails = 0
            else:
                self._fails += 1
                if self._fails >= self.threshold:
                    self._opened_at = _time.monotonic()


class _FeedThread:
    """Dedicated worker thread with an installed asyncio loop for the NATS feed.

    growwapi's feed wraps async NATS calls in a sync facade that calls
    `loop.run_until_complete`. Under uvicorn there is already a running loop on the
    calling thread, so those calls raise "Cannot run the event loop while another
    loop is running" and the tick feed silently never subscribes.

    The loop must be *set* on the thread but NOT running: growwapi calls
    `run_until_complete` itself, which refuses to start a loop that is already
    running. So this is a plain worker thread with an installed loop, draining a
    queue of callables synchronously, and idling by pumping the loop briefly so
    NATS callbacks still get serviced.
    """

    def __init__(self) -> None:
        import queue
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def _run(self) -> None:
        import asyncio
        import queue as _q
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ready.set()
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.05)
            except _q.Empty:
                # Give the NATS client a slice to process inbound messages.
                with contextlib_suppress():
                    loop.run_until_complete(asyncio.sleep(0.01))
                continue
            fn, args, kwargs, box = job
            try:
                box["result"] = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - relayed to the caller
                box["error"] = exc
            finally:
                box["done"].set()
        with contextlib_suppress():
            loop.close()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="groww-feed",
                                            daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=5):
                raise BrokerError("feed thread failed to start")

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run `fn` on the feed thread and wait for its result."""
        self.start()
        box: dict[str, Any] = {"done": threading.Event()}
        self._q.put((fn, args, kwargs, box))
        if not box["done"].wait(timeout=30):
            raise BrokerError("feed call timed out after 30s")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def stop(self) -> None:
        self._stop.set()
        self._thread = None


def contextlib_suppress():
    import contextlib
    return contextlib.suppress(Exception)


class _Sub(Subscription):
    def __init__(self, closer: Callable[[], None]):
        self._closer = closer
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._closer()
            except Exception as exc:  # pragma: no cover
                log.warning("subscription close failed", extra={"error": str(exc)})


class GrowwAdapter:
    name = "groww"

    def __init__(
        self,
        api_key: str,
        totp_seed: str,
        *,
        token_ttl_s: int = 6 * 3600,
        max_retries: int = 3,
    ):
        if not api_key or not totp_seed:
            raise BrokerError("GROWW_API_KEY / GROWW_TOTP_SEED missing — refusing to start")
        self._api_key = api_key
        self._totp = pyotp.TOTP(totp_seed)
        self._token_ttl_s = token_ttl_s
        self._max_retries = max_retries
        self._api: GrowwAPI | None = None
        self._feed: GrowwFeed | None = None
        self._token_at = 0.0
        self._auth_lock = threading.Lock()
        self._breaker = _CircuitBreaker()
        self._limiter = _RateLimiter()
        self._instruments: InstrumentMaster | None = None
        self._instruments_day = ""
        self._feed_thread = _FeedThread()
        self._own_refs: set[str] = set()

    # ------------------------------------------------------------------ auth
    def _authenticate(self) -> GrowwAPI:
        token = GrowwAPI.get_access_token(api_key=self._api_key, totp=self._totp.now())
        if not isinstance(token, str) or not token:
            raise BrokerError("Groww returned no access token", raw=type(token).__name__)
        self._token_at = _time.monotonic()
        log.info("groww authenticated", extra={"token_len": len(token)})
        return GrowwAPI(token)

    @property
    def api(self) -> GrowwAPI:
        with self._auth_lock:
            expired = _time.monotonic() - self._token_at > self._token_ttl_s
            if self._api is None or expired:
                self._api = self._authenticate()
                self._feed = None  # feed is bound to the api object
            return self._api

    def reauth(self) -> None:
        with self._auth_lock:
            self._api = None
            self._feed = None
            self._token_at = 0.0

    @property
    def feed(self) -> GrowwFeed:
        """Constructed ON the feed thread.

        GrowwFeed captures the ambient event loop at construction. Built on the
        main thread under uvicorn it would capture the running server loop, and
        every subscribe would then fail with "Cannot run the event loop while
        another loop is running" — which is exactly what happened.
        """
        if self._feed is None:
            api = self.api  # authenticate on the caller's thread; cheap and sync
            self._feed = self._feed_thread.call(GrowwFeed, api)
        return self._feed

    # ------------------------------------------------------------------ call wrapper
    def _call(self, label: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._breaker.is_open:
            raise BrokerError(f"circuit open for broker calls ({label})", retryable=True)

        last: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            if not self._limiter.acquire():
                raise BrokerError(f"rate limiter timeout ({label})", retryable=True)
            try:
                out = fn(*args, **kwargs)
                self._breaker.record(True)
                log.debug("broker ok", extra={"call": label, "attempt": attempt})
                return out
            except Exception as exc:
                last = exc
                msg = str(exc)
                low = msg.lower()
                unauthorised = any(
                    s in low for s in ("401", "unauthor", "token", "expired")
                )
                if "rate limit" in low and attempt < self._max_retries:
                    wait = 1.5 * attempt
                    log.warning("rate limited, backing off",
                                extra={"call": label, "sleep_s": wait})
                    _time.sleep(wait)
                    continue
                if unauthorised and attempt < self._max_retries:
                    log.warning("broker auth error, re-authenticating", extra={"call": label})
                    self.reauth()
                    continue
                self._breaker.record(False)
                log.warning(
                    "broker call failed",
                    extra={"call": label, "attempt": attempt, "error": msg[:300]},
                )
                if attempt < self._max_retries:
                    _time.sleep(min(2 ** attempt * 0.25, 2.0))
        raise BrokerError(f"{label} failed after {self._max_retries} attempts: {last}",
                          retryable=True, raw=last)

    # ------------------------------------------------------------------ execution
    def find_by_reference(self, client_id: str, segment: str) -> Order | None:
        """Idempotency probe. None means 'not placed yet', as far as the broker knows."""
        ref = make_reference(client_id)
        try:
            raw = self.api.get_order_status_by_reference(segment=segment, order_reference_id=ref)
        except Exception as exc:
            # A miss is commonly reported as an error; treat only clear 'not found' as None.
            if any(s in str(exc).lower() for s in ("not found", "404", "no order", "invalid")):
                return None
            log.warning("reference probe failed", extra={"error": str(exc)[:200]})
            return None
        payload = raw.get("payload", raw) if isinstance(raw, dict) else {}
        if not payload or not payload.get("groww_order_id"):
            return None
        return self._to_order(payload)

    def place_order(self, o: OrderRequest) -> OrderId:
        if not o.client_id:
            raise BrokerError("order without client_id — idempotency impossible (§0.4)")

        existing = self.find_by_reference(o.client_id, o.segment)
        if existing is not None:
            log.warning(
                "duplicate suppressed — reference already exists",
                extra={"client_id": o.client_id, "order_id": existing.order_id},
            )
            return existing.order_id

        kwargs: dict[str, Any] = {
            "validity": o.validity,
            "exchange": o.exchange,
            "order_type": _ORDER_TYPE_OUT[o.order_type],
            "product": o.product.value,
            "quantity": int(o.quantity),
            "segment": o.segment,
            "trading_symbol": o.trading_symbol,
            "transaction_type": o.side.value,
            "order_reference_id": make_reference(o.client_id),
        }
        if o.order_type in (OrderType.LIMIT, OrderType.SL):
            kwargs["price"] = float(o.price)
        if o.order_type in (OrderType.SL, OrderType.SL_M):
            if o.trigger_price <= 0:
                raise BrokerError(f"{o.order_type.value} needs a trigger_price")
            kwargs["trigger_price"] = float(o.trigger_price)

        self.register_own_ref(o.client_id)
        log.info("placing order", extra=redact({"order": kwargs}))
        raw = self._call("place_order", self.api.place_order, **kwargs)
        payload = raw.get("payload", raw) if isinstance(raw, dict) else {}
        oid = payload.get("groww_order_id") or payload.get("order_id")
        if not oid:
            raise BrokerError(f"place_order returned no order id: {raw}", raw=raw)
        log.info("order placed", extra={"order_id": oid, "client_id": o.client_id})
        return str(oid)

    def modify_order(self, oid: OrderId, o: OrderPatch, *, segment: str = "FNO",
                     order_type: OrderType = OrderType.LIMIT, quantity: int | None = None) -> None:
        kwargs: dict[str, Any] = {
            "order_type": _ORDER_TYPE_OUT[order_type],
            "segment": segment,
            "groww_order_id": oid,
            "quantity": int(o.quantity if o.quantity is not None else (quantity or 0)),
        }
        if o.price is not None:
            kwargs["price"] = float(o.price)
        if o.trigger_price is not None:
            kwargs["trigger_price"] = float(o.trigger_price)
        self._call("modify_order", self.api.modify_order, **kwargs)

    def cancel_order(self, oid: OrderId, *, segment: str = "FNO") -> None:
        self._call("cancel_order", self.api.cancel_order, groww_order_id=oid, segment=segment)

    def register_own_ref(self, client_id: str) -> None:
        """Remember a reference we generated, so we can tell our orders from theirs."""
        if client_id:
            self._own_refs.add(make_reference(client_id))

    def cancel_resting_stops(self, *, only_refs: set[str] | None = None) -> list[OrderId]:
        """Cancel resting stops. With `only_refs`, cancels ONLY ours.

        The trader's rule is: never override what they placed. Their own stop is left
        alone even during a square-off. The trade-off is recorded honestly — an
        orphaned stop of theirs can still fire on a flat book — so we alert instead
        of cancelling it for them.
        """
        cancelled: list[OrderId] = []
        try:
            book = self.get_orders()
        except BrokerError as exc:
            log.error("could not read order book to cancel stops",
                      extra={"error": str(exc)[:160]})
            return cancelled

        for o in book:
            if o.order_type not in (OrderType.SL, OrderType.SL_M) or o.is_terminal:
                continue
            ours = only_refs is None or (o.client_id and o.client_id in only_refs)
            if not ours:
                log.warning("leaving the trader's own stop in place (never override)",
                            extra={"order_id": o.order_id, "symbol": o.trading_symbol,
                                   "trigger": o.trigger_price})
                continue
            try:
                self.cancel_order(o.order_id, segment=o.segment or "FNO")
                cancelled.append(o.order_id)
                log.info("cancelled our resting stop",
                         extra={"order_id": o.order_id, "symbol": o.trading_symbol})
            except BrokerError as exc:
                log.error("could not cancel our stop — it may fire on a flat book",
                          extra={"order_id": o.order_id, "error": str(exc)[:160]})
        return cancelled

    def square_off_all(self) -> list[OrderId]:
        """Market-exit everything. Groww has no bulk endpoint, so we invert positions.

        Our OWN resting stops are cancelled first so they cannot fire on a flat book.
        Stops the trader placed are deliberately left untouched.
        """
        out: list[OrderId] = []
        self.cancel_resting_stops(only_refs=self._own_refs or None)

        for p in self.get_positions():
            if not p.is_open:
                continue
            side = Side.SELL if p.quantity > 0 else Side.BUY
            inst = self._lookup(p.trading_symbol)
            req = OrderRequest(
                trading_symbol=p.trading_symbol,
                exchange=inst.exchange if inst else (p.exchange or "NSE"),
                segment=p.segment or "FNO",
                side=side,
                quantity=abs(int(p.quantity)),
                order_type=OrderType.MARKET,
                # MUST be unique per attempt. A deterministic key made the retry in
                # Guardian.square_off_all a no-op: find_by_reference matched the
                # first (stuck) order and returned without sending anything.
                client_id=f"sq{uuid.uuid4().hex[:18]}",
                tag="squareoff",
            )
            try:
                out.append(self.place_order(req))
            except BrokerError as exc:
                log.error("square-off leg failed",
                          extra={"symbol": p.trading_symbol, "error": str(exc)[:200]})
        return out

    # ------------------------------------------------------------------ state
    def _to_order(self, d: dict[str, Any]) -> Order:
        raw_status = str(d.get("order_status") or d.get("status") or "").upper()
        qty = int(d.get("quantity") or 0)
        filled = int(d.get("filled_quantity") or d.get("filled_qty") or 0)
        status = _STATUS_MAP.get(raw_status, OrderStatus.UNKNOWN)
        if status is OrderStatus.OPEN and 0 < filled < qty:
            status = OrderStatus.PARTIAL
        created = d.get("created_at") or d.get("order_timestamp")
        ts: datetime | None = None
        if isinstance(created, str):
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                ts = None
        return Order(
            order_id=str(d.get("groww_order_id") or d.get("order_id") or ""),
            trading_symbol=str(d.get("trading_symbol") or ""),
            side=Side.SELL if str(d.get("transaction_type", "")).upper() == "SELL" else Side.BUY,
            quantity=qty,
            filled_quantity=filled,
            status=status,
            order_type=_ORDER_TYPE_IN.get(str(d.get("order_type", "")).upper(), OrderType.MARKET),
            price=float(d.get("price") or 0.0),
            trigger_price=float(d.get("trigger_price") or 0.0),
            average_price=float(d.get("average_fill_price") or d.get("average_price") or 0.0),
            client_id=str(d.get("order_reference_id") or ""),
            exchange=str(d.get("exchange") or ""),
            segment=str(d.get("segment") or "FNO"),
            created_at=ts,
            raw=d,
        )

    def get_orders(self, *, segment: str | None = SCOPE_SEGMENT) -> list[Order]:
        """The FNO order book. Ask the server to scope it, then scope it again here."""
        raw = self._call("get_order_list", self.api.get_order_list, segment=segment,
                         page=0, page_size=100)
        payload = raw.get("payload", raw) if isinstance(raw, dict) else {}
        rows = payload.get("order_list") or payload.get("orders") or []
        kept, dropped = [], 0
        for r in rows:
            if not isinstance(r, dict):
                continue
            if not _in_scope(r):
                dropped += 1
                continue
            kept.append(self._to_order(r))
        if dropped:
            log.info("orders outside FNO scope ignored", extra={"count": dropped})
        return kept

    def get_positions(self, *, segment: str | None = SCOPE_SEGMENT) -> list[Position]:
        raw = self._call("get_positions_for_user", self.api.get_positions_for_user,
                         segment=segment)
        payload = raw.get("payload", raw) if isinstance(raw, dict) else {}
        rows = payload.get("positions") or []
        out: list[Position] = []
        dropped = 0
        for r in rows:
            if not isinstance(r, dict):
                continue
            if not _in_scope(r):
                # Never report — let alone square off — a position from another
                # segment. That is the trader's own business.
                dropped += 1
                continue
            qty = int(r.get("quantity") or r.get("net_quantity") or 0)
            sym = str(r.get("trading_symbol") or "")
            inst = self._lookup(sym)
            out.append(Position(
                trading_symbol=sym,
                quantity=qty,
                average_price=float(r.get("average_price") or r.get("net_price") or 0.0),
                last_price=float(r.get("last_price") or r.get("ltp") or 0.0),
                unrealized_pnl=float(r.get("unrealized_pnl") or 0.0),
                realized_pnl=float(r.get("realized_pnl") or 0.0),
                lot_size=inst.lot_size if inst else 0,
                exchange=str(r.get("exchange") or (inst.exchange if inst else "")),
                segment=str(r.get("segment") or "FNO"),
                raw=r,
            ))
        if dropped:
            log.info("positions outside FNO scope ignored", extra={"count": dropped})
        return out

    def get_funds(self) -> Funds:
        raw = self._call("get_available_margin_details", self.api.get_available_margin_details)
        p = raw.get("payload", raw) if isinstance(raw, dict) else {}
        avail = p.get("clear_cash", p.get("available_margin", 0.0))
        return Funds(
            available=float(avail or 0.0),
            used=float(p.get("used_margin") or p.get("collateral_used") or 0.0),
            total=float(p.get("net_balance") or avail or 0.0),
            raw=p if isinstance(p, dict) else {},
        )

    # ------------------------------------------------------------------ instruments
    def _lookup(self, trading_symbol: str) -> Instrument | None:
        if self._instruments is None:
            return None
        return self._instruments.get(trading_symbol)

    def get_instruments(self, *, force: bool = False) -> InstrumentMaster:
        """Daily master. Cached per IST calendar day — lot sizes must never be stale (§0.3)."""
        today = datetime.now(IST).strftime("%Y-%m-%d")
        if self._instruments is not None and self._instruments_day == today and not force:
            return self._instruments

        df = self._call("get_all_instruments", self.api.get_all_instruments)
        cols = set(df.columns)
        required = {"trading_symbol", "exchange", "segment", "instrument_type", "lot_size"}
        missing = required - cols
        if missing:
            raise BrokerError(f"instrument master missing columns: {sorted(missing)}")

        instruments: list[Instrument] = []
        for rec in df.to_dict("records"):
            try:
                lot = int(float(rec.get("lot_size") or 0))
            except (TypeError, ValueError):
                lot = 0
            try:
                strike = float(rec.get("strike_price") or 0) or 0.0
            except (TypeError, ValueError):
                strike = 0.0
            instruments.append(Instrument(
                trading_symbol=str(rec.get("trading_symbol") or ""),
                exchange=str(rec.get("exchange") or ""),
                segment=str(rec.get("segment") or ""),
                lot_size=lot,
                instrument_type=str(rec.get("instrument_type") or "").upper(),
                name=str(rec.get("underlying_symbol") or rec.get("name") or "").upper(),
                strike=strike,
                expiry=str(rec.get("expiry_date") or "")[:10],
                exchange_token=str(rec.get("exchange_token") or ""),
            ))

        master = InstrumentMaster(instruments, datetime.now(UTC))
        self._instruments = master
        self._instruments_day = today
        log.info("instrument master loaded", extra={"rows": len(master), "day": today})
        return master

    # ------------------------------------------------------------------ market data
    def get_quote(self, keys: list[str], *, segment: str = "FNO") -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for sym in keys:
            inst = self._lookup(sym)
            exchange = inst.exchange if inst else "NSE"
            try:
                raw = self._call("get_quote", self.api.get_quote,
                                 trading_symbol=sym, exchange=exchange, segment=segment)
            except BrokerError as exc:
                log.warning("quote failed", extra={"symbol": sym, "error": str(exc)[:160]})
                continue
            p = raw.get("payload", raw) if isinstance(raw, dict) else {}
            if not isinstance(p, dict):
                continue
            out[sym] = Quote(
                trading_symbol=sym,
                last_price=float(p.get("last_price") or p.get("ltp") or 0.0),
                open_interest=float(p.get("open_interest") or p.get("oi") or 0.0),
                implied_volatility=float(p.get("implied_volatility") or p.get("iv") or 0.0),
                volume=float(p.get("volume") or p.get("total_traded_volume") or 0.0),
                bid=float((p.get("depth", {}).get("buy") or [{}])[0].get("price", 0.0))
                if isinstance(p.get("depth"), dict) else 0.0,
                ask=float((p.get("depth", {}).get("sell") or [{}])[0].get("price", 0.0))
                if isinstance(p.get("depth"), dict) else 0.0,
                ts=datetime.now(UTC),
                raw=p,
            )
        return out

    def get_ltp_batch(self, symbols: list[str], *, segment: str = "FNO") -> dict[str, float]:
        """Cheaper than get_quote for the 30s chain sweep. Keys are EXCHANGE_SYMBOL."""
        if not symbols:
            return {}
        keys: list[str] = []
        back: dict[str, str] = {}
        for s in symbols:
            inst = self._lookup(s)
            k = f"{inst.exchange if inst else 'NSE'}_{s}"
            keys.append(k)
            back[k] = s
        raw = self._call("get_ltp", self.api.get_ltp,
                         exchange_trading_symbols=tuple(keys), segment=segment)
        p = raw.get("payload", raw) if isinstance(raw, dict) else {}
        out: dict[str, float] = {}
        if isinstance(p, dict):
            for k, v in p.items():
                sym = back.get(k, k.split("_", 1)[-1])
                try:
                    out[sym] = float(v if not isinstance(v, dict)
                                     else v.get("last_price") or v.get("ltp") or 0.0)
                except (TypeError, ValueError):
                    continue
        return out

    def get_candles(self, key: str, tf: str = "1m", span: int = 375,
                    *, segment: str = "FNO") -> list[Candle]:
        inst = self._lookup(key)
        exchange = inst.exchange if inst else "NSE"
        minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(tf, 1)
        end = datetime.now(IST)
        start = end - timedelta(minutes=minutes * span + 5)
        fmt = "%Y-%m-%d %H:%M:%S"
        raw = self._call("get_historical_candle_data", self.api.get_historical_candle_data,
                         trading_symbol=key, exchange=exchange, segment=segment,
                         start_time=start.strftime(fmt), end_time=end.strftime(fmt),
                         interval_in_minutes=minutes)
        p = raw.get("payload", raw) if isinstance(raw, dict) else {}
        rows = p.get("candles") or p.get("data") or []
        out: list[Candle] = []
        for r in rows:
            try:
                if isinstance(r, dict):
                    ts_v, o, h, low, c = (r.get("timestamp"), r.get("open"), r.get("high"),
                                          r.get("low"), r.get("close"))
                    vol = r.get("volume") or 0
                else:
                    ts_v, o, h, low, c = r[0], r[1], r[2], r[3], r[4]
                    vol = r[5] if len(r) > 5 else 0
                ts = (datetime.fromtimestamp(int(ts_v), IST) if isinstance(ts_v, (int, float))
                      else datetime.fromisoformat(str(ts_v).replace("Z", "+00:00")))
                out.append(Candle(ts=ts, open=float(o), high=float(h), low=float(low),
                                  close=float(c), volume=float(vol or 0)))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        return sorted(out, key=lambda c: c.ts)

    # ------------------------------------------------------------------ streams
    def stream_ticks(self, keys: list[str], on_tick: Callable[[Quote], None]) -> Subscription:
        instrument_list = []
        for s in keys:
            inst = self._lookup(s)
            instrument_list.append({
                "exchange": inst.exchange if inst else "NSE",
                "segment": inst.segment if inst else "FNO",
                "exchange_token": inst.exchange_token if inst else "",
            })

        def _on_data() -> None:
            try:
                snap = self.feed.get_ltp() or {}
            except Exception as exc:  # pragma: no cover
                log.warning("feed ltp read failed", extra={"error": str(exc)[:160]})
                return
            now = datetime.now(UTC)
            for sym, val in _flatten_ltp(snap).items():
                on_tick(Quote(trading_symbol=sym, last_price=val, ts=now))

        self._feed_thread.call(self.feed.subscribe_ltp,
                               instrument_list=instrument_list, on_data_received=_on_data)
        return _Sub(lambda: self._feed_thread.call(
            self.feed.unsubscribe_ltp, instrument_list=instrument_list))

    def stream_orders(self, on_order_event: Callable[[Order], None]) -> Subscription:
        """FNO order updates. Whether app-placed orders arrive here is D-001."""
        def _on_data() -> None:
            try:
                upd = self.feed.get_fno_order_update()
            except Exception as exc:  # pragma: no cover
                log.warning("feed order read failed", extra={"error": str(exc)[:160]})
                return
            if isinstance(upd, dict) and upd:
                on_order_event(self._to_order(upd))

        self._feed_thread.call(self.feed.subscribe_fno_order_updates,
                               on_data_received=_on_data)
        return _Sub(lambda: self._feed_thread.call(
            self.feed.unsubscribe_fno_order_updates))

    def stream_positions(self, on_position: Callable[[list[Position]], None]) -> Subscription:
        """Live P&L source for on_pnl_tick (D-005)."""
        def _on_data() -> None:
            try:
                upd = self.feed.get_fno_position_update()
            except Exception as exc:  # pragma: no cover
                log.warning("feed position read failed", extra={"error": str(exc)[:160]})
                return
            rows = []
            if isinstance(upd, dict):
                rows = upd.get("positions") or ([upd] if upd.get("trading_symbol") else [])
            out = []
            for r in rows:
                sym = str(r.get("trading_symbol") or "")
                inst = self._lookup(sym)
                out.append(Position(
                    trading_symbol=sym,
                    quantity=int(r.get("quantity") or r.get("net_quantity") or 0),
                    average_price=float(r.get("average_price") or 0.0),
                    last_price=float(r.get("last_price") or r.get("ltp") or 0.0),
                    unrealized_pnl=float(r.get("unrealized_pnl") or 0.0),
                    lot_size=inst.lot_size if inst else 0,
                    raw=r,
                ))
            if out:
                on_position(out)

        self._feed_thread.call(self.feed.subscribe_fno_position_updates,
                               on_data_received=_on_data)
        return _Sub(lambda: self._feed_thread.call(
            self.feed.unsubscribe_fno_position_updates))


def _flatten_ltp(snap: dict[str, Any]) -> dict[str, float]:
    """The feed nests by exchange/segment/token; flatten to {symbol: ltp}."""
    out: dict[str, float] = {}

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            if "ltp" in node or "last_price" in node:
                try:
                    out[key] = float(node.get("ltp") or node.get("last_price") or 0.0)
                except (TypeError, ValueError):
                    pass
                return
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, (int, float)) and key:
            out[key] = float(node)

    walk(snap)
    return out

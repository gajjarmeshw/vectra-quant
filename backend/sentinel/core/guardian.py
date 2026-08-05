"""Guardian — protects trades the system did not initiate. Build plan Phase 2 §2.

It cannot block a manual entry (you placed it in the Groww app). It can:
  - detect it within seconds,
  - count it against the day's trade budget,
  - force-attach an SL-M if none exists inside the deadline,
  - log a violation when the trade broke a lock or a window.

Detection runs BOTH paths (D-001): a 5s REST order-book diff as the default truth,
plus the WS order stream as an accelerator. Whichever sees an unknown order first
wins; `_seen` dedupes by broker order id so a trade is never counted twice.
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sentinel.brokers.base import (
    BrokerError,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from sentinel.core.session_clock import now_ist, session_date
from sentinel.db import Trade, Violation, session, utcnow
from sentinel.logging_setup import get
from sentinel.risk_engine import DayState, RiskEngine

log = get("core.guardian")


class Guardian:
    def __init__(
        self,
        broker,  # noqa: ANN001
        engine: RiskEngine,
        *,
        sl_attach_deadline_s: int = 60,
        squareoff_verify_s: int = 10,
        yield_to_manual_sl: bool = True,
        on_alert: Callable[[str, str, dict], None] | None = None,
        instrument_of: Callable[[str], object | None] | None = None,
    ):
        self.broker = broker
        self.engine = engine
        self.sl_attach_deadline_s = sl_attach_deadline_s
        self.squareoff_verify_s = squareoff_verify_s
        self.yield_to_manual_sl = yield_to_manual_sl
        # Stops guardian placed itself: symbol -> broker order id. Used to step
        # aside the moment the trader's own stop shows up.
        self._own_stops: dict[str, str] = {}
        self._on_alert = on_alert
        self._instrument_of = instrument_of
        self._lock = threading.RLock()
        # Order ids we placed ourselves, by client_id and by broker id.
        self._own_client_ids: set[str] = set()
        self._own_order_ids: set[str] = set()
        # Every order id already processed, whichever path saw it.
        self._seen: set[str] = set()
        # Manual trades awaiting an SL: symbol -> (first_seen, entry_premium, qty)
        self._pending_sl: dict[str, tuple[datetime, float, int]] = {}
        # Orders that already existed when this process started are history, not
        # new exposure. Without this, a mid-day restart re-adopts the entire day's
        # order book, inflates trades_taken, and locks the session on boot.
        self._started_at = datetime.now(UTC)
        self._primed = False
        # Which path actually saw each manual trade first. Groww's docs do not state
        # whether the WS order feed reports app-placed orders (D-001); rather than
        # guess, count it and let real usage answer.
        self.detected_by: dict[str, int] = {"ws": 0, "rest": 0}

    # ------------------------------------------------------------------ registration
    def register_own(self, client_id: str, order_id: str = "") -> None:
        """Called by the lifecycle before/after placing a system order."""
        with self._lock:
            if client_id:
                self._own_client_ids.add(client_id)
            if order_id:
                self._own_order_ids.add(order_id)
                self._seen.add(order_id)

    def prime(self) -> int:
        """Mark every order already on the book as seen. Call once, at boot.

        Returns how many were baselined. These are NOT counted against the trade
        budget and get no stop attached; only positions still open are adopted,
        via `adopt_existing`.
        """
        try:
            orders = self.broker.get_orders()
        except BrokerError as exc:
            log.error("guardian could not prime — refusing to treat history as new",
                      extra={"error": str(exc)[:200]})
            self._primed = True          # fail closed: act on nothing we cannot verify
            return 0
        with self._lock:
            for o in orders:
                if o.order_id:
                    self._seen.add(o.order_id)
            self._primed = True
            n = len(self._seen)
        log.warning("guardian primed", extra={"baselined_orders": n})
        return n

    def is_own(self, order: Order) -> bool:
        with self._lock:
            if order.order_id and order.order_id in self._own_order_ids:
                return True
            ref = (order.client_id or "").strip()
            if not ref:
                return False
            if ref in self._own_client_ids:
                return True
            # Groww returns the 20-char alphanumeric reference, not our raw uuid.
            return any(c.replace("-", "")[:20] == ref for c in self._own_client_ids)

    def alert(self, kind: str, message: str, data: dict | None = None) -> None:
        if self._on_alert:
            try:
                self._on_alert(kind, message, data or {})
            except Exception as exc:  # pragma: no cover
                log.warning("guardian alert failed", extra={"error": str(exc)[:160]})

    # ------------------------------------------------------------------ detection
    def on_order_event(self, order: Order) -> bool:
        """WS path. Returns True when this was an unknown (manual) filled order."""
        return self._consider(order, source="ws")

    def poll_once(self) -> list[Order]:
        """REST path — the default truth (D-001). Returns newly detected manual orders."""
        try:
            orders = self.broker.get_orders()
        except BrokerError as exc:
            log.warning("guardian poll failed", extra={"error": str(exc)[:200]})
            return []
        found = []
        for o in orders:
            if self._consider(o, source="rest"):
                found.append(o)
        return found

    def _consider(self, order: Order, *, source: str) -> bool:
        if not order.order_id:
            return False
        if not self._primed:
            # Never act on the book before it has been baselined.
            return False
        with self._lock:
            if order.order_id in self._seen:
                return False
            # An order stamped before we started is history. Baseline it silently.
            if order.created_at is not None:
                created = (order.created_at if order.created_at.tzinfo
                           else order.created_at.replace(tzinfo=UTC))
                if created < self._started_at:
                    self._seen.add(order.order_id)
                    return False
            if self.is_own(order):
                self._own_order_ids.add(order.order_id)
                self._seen.add(order.order_id)
                return False
            # Only a fill creates exposure. Resting/cancelled manual orders are ignored
            # until they actually execute.
            if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL):
                return False
            if order.filled_quantity <= 0:
                return False
            # An exit leg is not a new trade.
            if order.side is Side.SELL:
                self._seen.add(order.order_id)
                return False
            self._seen.add(order.order_id)

        # A fill with no resulting open position is a completed round trip, not
        # exposure we need to guard.
        try:
            held = any(p.trading_symbol == order.trading_symbol and p.is_open
                       for p in self.broker.get_positions())
        except BrokerError:
            held = True          # cannot verify -> assume exposure and protect it
        if not held:
            log.info("manual fill with no open position — ignoring",
                     extra={"symbol": order.trading_symbol, "order_id": order.order_id})
            return False

        self.detected_by[source] = self.detected_by.get(source, 0) + 1
        log.warning("MANUAL trade detected", extra={
            "source": source, "order_id": order.order_id,
            "symbol": order.trading_symbol, "qty": order.filled_quantity,
        })
        self._record_manual(order, source)
        return True

    # ------------------------------------------------------------------ bookkeeping
    def _record_manual(self, order: Order, source: str) -> None:
        sdate = session_date()
        state_before = self.engine.state
        entry = order.average_price or order.price

        # Count it against the budget, exactly like a system trade.
        self.engine.on_trade_opened()

        inst = self._instrument_of(order.trading_symbol) if self._instrument_of else None
        lot_size = getattr(inst, "lot_size", 0) or 0
        instrument_name = (getattr(inst, "name", "") or "").upper()
        lots = (order.filled_quantity // lot_size) if lot_size else 0

        trade_id = str(uuid.uuid4())
        with session() as s:
            s.add(Trade(
                id=trade_id,
                opened_at=utcnow(),
                session_date=sdate,
                origin="manual",
                instrument=instrument_name,
                trading_symbol=order.trading_symbol,
                exchange=order.exchange or getattr(inst, "exchange", "") or "",
                segment=order.segment or "FNO",
                direction=(getattr(inst, "instrument_type", "") or ""),
                lots=lots,
                lot_size=lot_size,
                qty=int(order.filled_quantity),
                entry_price=float(entry),
                entry_order_id=order.order_id,
                status="OPEN",
                guardian_note=f"detected via {source}",
            ))

            # Violation if it broke a lock or a window (§4.5 layer 5).
            reasons = []
            if state_before == DayState.LOCKED:
                reasons.append("placed while day LOCKED")
            t = now_ist().time()
            if not (self.engine.cfg.entry_open <= t < self.engine.cfg.entry_close):
                reasons.append(f"outside entry window ({t.strftime('%H:%M')})")
            if self.engine.trades_taken > self.engine.trade_cap:
                reasons.append(
                    f"exceeds trade cap ({self.engine.trades_taken}/{self.engine.trade_cap})"
                )
            for r in reasons:
                s.add(Violation(ts=utcnow(), session_date=sdate,
                                kind="MANUAL_TRADE", detail=r, trade_id=trade_id))

        with self._lock:
            self._pending_sl[order.trading_symbol] = (
                datetime.now(UTC), float(entry), int(order.filled_quantity)
            )

        self.alert("GUARDIAN", (
            f"Manual {order.trading_symbol} detected. "
            f"Trade {self.engine.trades_taken} of {self.engine.trade_cap}."
        ), {"order_id": order.order_id, "entry": entry, "source": source})

    # ------------------------------------------------------------------ SL enforcement
    def protected_quantity(self, symbol: str,
                           orders: list[Order] | None = None) -> int | None:
        """Total quantity covered by resting stops. None = cannot tell.

        Quantity matters: a 20-qty stop does NOT protect 80 qty. Treating protection
        as a per-symbol boolean meant a manual add onto a contract that already had a
        system stop was recorded as covered, leaving the added lots naked.
        """
        if orders is None:
            try:
                orders = self.broker.get_orders()
            except BrokerError:
                log.warning("cannot read order book — assuming protected",
                            extra={"symbol": symbol})
                return None
        return sum(
            max(0, o.quantity - o.filled_quantity)
            for o in orders
            if o.trading_symbol == symbol
            and o.order_type in (OrderType.SL, OrderType.SL_M)
            and not o.is_terminal
        )

    def has_resting_stop(self, symbol: str, orders: list[Order] | None = None) -> bool:
        qty = self.protected_quantity(symbol, orders)
        return qty is None or qty > 0

    def enforce_stops(self) -> list[str]:
        """Attach an SL-M to any manual position still naked past the deadline."""
        attached: list[str] = []
        now = datetime.now(UTC)

        with self._lock:
            due = {
                sym: v for sym, v in self._pending_sl.items()
                if now - v[0] >= timedelta(seconds=self.sl_attach_deadline_s)
            }

        if not due:
            return attached
        try:
            positions = {p.trading_symbol: p for p in self.broker.get_positions()}
            book = self.broker.get_orders()
        except BrokerError as exc:
            log.warning("stop enforcement: broker state unavailable",
                        extra={"error": str(exc)[:160]})
            return attached

        for symbol, (_first_seen, entry, _qty) in due.items():
            pos = positions.get(symbol)
            if pos is None or not pos.is_open:
                with self._lock:
                    self._pending_sl.pop(symbol, None)
                continue

            covered = self.protected_quantity(symbol, book)
            exposure = abs(int(pos.quantity))
            if covered is None:
                log.warning("order book unreadable — leaving position alone",
                            extra={"symbol": symbol})
                continue
            gap = exposure - covered
            if gap <= 0:
                log.info("manual trade already protected",
                         extra={"symbol": symbol, "exposure": exposure,
                                "covered": covered})
                with self._lock:
                    self._pending_sl.pop(symbol, None)
                continue
            if covered > 0:
                log.warning("partially protected — stopping only the uncovered lots",
                            extra={"symbol": symbol, "exposure": exposure,
                                   "covered": covered, "gap": gap})

            inst = self._instrument_of(symbol) if self._instrument_of else None
            name = (getattr(inst, "name", "") or "").upper()
            if name not in self.engine.cfg.default_sl_premium_pts:
                # Fail closed rather than invent a stop distance (§0.2, §0.3).
                log.error("no default SL points for instrument — cannot attach",
                          extra={"symbol": symbol, "instrument": name or "unknown"})
                self.alert("GUARDIAN", (
                    f"{symbol} is UNPROTECTED — no default stop configured for "
                    f"{name or 'unknown instrument'}. Set an SL in the Groww app now."
                ), {"symbol": symbol})
                with self._lock:
                    self._pending_sl.pop(symbol, None)
                continue

            entry_px = float(pos.average_price or entry)
            trigger = self.engine.default_stop_premium(name, entry_px)
            if trigger >= entry_px:
                log.error("computed stop is not below entry — refusing",
                          extra={"symbol": symbol, "entry": entry_px, "trigger": trigger})
                continue

            req = OrderRequest(
                trading_symbol=symbol,
                exchange=pos.exchange or getattr(inst, "exchange", "") or "NSE",
                segment=pos.segment or "FNO",
                side=Side.SELL,
                quantity=int(gap),          # only the unprotected remainder
                order_type=OrderType.SL_M,
                trigger_price=trigger,
                client_id=str(uuid.uuid4()),
                tag="guardian_sl",
            )
            try:
                oid = self.broker.place_order(req)
            except BrokerError as exc:
                log.error("guardian SL attach FAILED", extra={
                    "symbol": symbol, "error": str(exc)[:200],
                })
                self.alert("SYSTEM", (
                    f"{symbol} is UNPROTECTED — guardian could not place the stop "
                    f"({str(exc)[:80]}). Set an SL in the Groww app now."
                ), {"symbol": symbol})
                continue

            self.register_own(req.client_id, oid)
            with self._lock:
                self._pending_sl.pop(symbol, None)
                self._own_stops[symbol] = oid
            attached.append(symbol)

            with session() as s:
                t = (s.query(Trade)
                     .filter(Trade.trading_symbol == symbol, Trade.status == "OPEN")
                     .order_by(Trade.opened_at.desc()).first())
                if t:
                    t.sl_order_id = oid
                    t.sl_price = trigger
                    t.guardian_note = (
                        f"{t.guardian_note}; SL-M attached at {trigger} by guardian"
                    ).strip("; ")

            log.warning("guardian attached SL", extra={
                "symbol": symbol, "trigger": trigger, "order_id": oid,
            })
            self.alert("GUARDIAN", (
                f"Manual {symbol} detected. SL attached at {trigger:.1f}. "
                f"Trade {self.engine.trades_taken} of {self.engine.trade_cap}."
            ), {"symbol": symbol, "trigger": trigger, "order_id": oid})

        return attached

    def yield_to_own_stops(self, book: list[Order] | None = None) -> list[str]:
        """Cancel guardian's stop once the trader's own stop appears.

        "Don't override my SL" taken literally. Guardian adds a stop at the deadline
        because an unprotected position is unacceptable — but the instant a stop the
        trader placed themselves is resting on that symbol, guardian withdraws its
        own so exactly ONE stop is ever live. Without this, both fire and the
        position flips from long to short.
        """
        if not self.yield_to_manual_sl:
            return []
        with self._lock:
            mine = dict(self._own_stops)
        if not mine:
            return []
        if book is None:
            try:
                book = self.broker.get_orders()
            except BrokerError:
                return []

        withdrawn: list[str] = []
        for symbol, my_oid in mine.items():
            live_mine = None
            theirs = []
            for o in book:
                if o.trading_symbol != symbol:
                    continue
                if o.order_type not in (OrderType.SL, OrderType.SL_M) or o.is_terminal:
                    continue
                if o.order_id == my_oid:
                    live_mine = o
                else:
                    theirs.append(o)

            if live_mine is None:
                with self._lock:
                    self._own_stops.pop(symbol, None)   # already gone
                continue
            if not theirs:
                continue

            try:
                self.broker.cancel_order(my_oid, segment=live_mine.segment or "FNO")
            except BrokerError as exc:
                log.warning("could not withdraw guardian stop",
                            extra={"symbol": symbol, "error": str(exc)[:160]})
                continue
            with self._lock:
                self._own_stops.pop(symbol, None)
            withdrawn.append(symbol)
            log.warning("guardian stop withdrawn — your own stop is resting",
                        extra={"symbol": symbol, "cancelled": my_oid,
                               "yours": theirs[0].order_id,
                               "your_trigger": theirs[0].trigger_price})
            self.alert("GUARDIAN", (
                f"Your own stop on {symbol} is live at {theirs[0].trigger_price:.1f} — "
                f"guardian cancelled its backup stop."
            ), {"symbol": symbol})
        return withdrawn

    # ------------------------------------------------------------------ square-off
    def square_off_all(self, reason: str) -> tuple[list[str], bool]:
        """Market-exit everything and verify flat via REST. Returns (order_ids, flat)."""
        log.warning("SQUARE OFF", extra={"reason": reason})
        try:
            ids = self.broker.square_off_all()
        except BrokerError as exc:
            log.error("square-off failed", extra={"error": str(exc)[:200]})
            self.alert("SYSTEM", f"SQUARE-OFF FAILED: {str(exc)[:120]}", {"reason": reason})
            return [], False

        from sentinel.brokers.recon import Reconciler
        flat = Reconciler(self.broker).verify_flat(timeout_s=self.squareoff_verify_s)
        if not flat:
            log.error("retrying square-off — still not flat")
            try:
                ids += self.broker.square_off_all()
            except BrokerError as exc:
                log.error("square-off retry failed", extra={"error": str(exc)[:200]})
            flat = Reconciler(self.broker).verify_flat(timeout_s=self.squareoff_verify_s)

        if not flat:
            self.alert("SYSTEM", (
                "CRITICAL: not flat after square-off and one retry. "
                "Exit manually in the Groww app now."
            ), {"reason": reason})
        with self._lock:
            self._pending_sl.clear()
        return ids, flat

    def adopt_existing(self, positions: list[Position]) -> None:
        """At boot, treat already-open broker positions as known, not as new trades.

        Prevents a restart mid-session from double-counting the budget or re-attaching
        stops to positions that already have them.
        """
        with self._lock:
            for p in positions:
                if p.is_open:
                    self._pending_sl.setdefault(
                        p.trading_symbol,
                        (datetime.now(UTC), float(p.average_price), abs(p.quantity)),
                    )

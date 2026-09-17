"""Suggested-trade lifecycle. Design §8 flow A, build plan Phase 3 §5 / Phase 4 §1.

event -> snapshot -> LLM -> code gates -> PWA card -> approve-tap
      -> RE-VALIDATE at tap time -> size -> entry + SL -> manage -> exit -> FSM

Two rules carry the safety here:
  1. `can_enter` runs again at tap time, not at suggestion time. State may have moved
     in those 90 seconds.
  2. Every order path carries a client uuid, so a double-tap or a retry cannot
     double-order (§0.4).
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from vectra_quant.brokers.base import (
    BrokerError,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
)
from vectra_quant.brokers.costs import net_pnl
from vectra_quant.core.session_clock import now_ist, session_date
from vectra_quant.core.sizing import select_strike, size_with_cap
from vectra_quant.db import Suggestion, Trade, session, utcnow
from vectra_quant.logging_setup import get
from vectra_quant.risk_engine import RejectReason

log = get("core.lifecycle")

TTL_SECONDS = 90


@dataclass
class GateResult:
    passed: bool
    reason: str = ""


@dataclass
class QueuedSuggestion:
    id: str
    trading_symbol: str
    instrument: str
    direction: str
    strike: float
    lots: int
    qty: int
    entry_low: float
    entry_high: float
    stop_loss: float
    target: float
    confidence: int
    risk: float
    cost: float
    expires_at: datetime
    over_risk: bool
    raw: dict[str, Any]

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    @property
    def seconds_left(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


class Lifecycle:
    def __init__(
        self,
        *,
        broker,
        orchestrator,
        guardian,
        instruments,
        chain,
        settings,
        killswitch,
        on_alert=None,
        fill_wait_s: int = 20,
    ):
        self.broker = broker
        self.orch = orchestrator
        self.guardian = guardian
        self.instruments = instruments
        self.chain = chain
        self.settings = settings
        self.killswitch = killswitch
        self.fill_wait_s = fill_wait_s
        self._on_alert = on_alert
        self._queue: dict[str, QueuedSuggestion] = {}
        self._lock = threading.RLock()
        self._approving: set[str] = set()

    def alert(self, kind: str, message: str, data: dict | None = None) -> None:
        if self._on_alert:
            try:
                self._on_alert(kind, message, data or {})
            except Exception as exc:  # pragma: no cover
                log.warning("lifecycle alert failed", extra={"error": str(exc)[:160]})

    # ------------------------------------------------------------------ gates
    def apply_gates(self, payload: dict[str, Any], *, spot: float,
                    is_expiry_day: bool) -> tuple[GateResult, QueuedSuggestion | None]:
        """Post-LLM code gates. Build plan Phase 3 §5, design §6.2."""
        cfg = self.settings
        instrument = str(payload.get("instrument", "")).upper()
        side = str(payload.get("direction", "")).upper()

        conf = int(payload.get("confidence", 0))
        decision = self.orch.can_enter(
            confidence=conf, is_expiry_day=is_expiry_day,
            active_trades=self.open_trades(),
            proposed_instrument=instrument,
        )
        if not decision.allowed:
            # design_spec §3.2 wants the numbers on the GATED chip, not just a label.
            if decision.reason is RejectReason.CONFIDENCE:
                return GateResult(False, f"conf {conf} < {decision.min_confidence}"), None
            return GateResult(False, decision.reason.value if decision.reason else "blocked"), None

        zone = payload.get("entry_zone") or {}
        low, high = float(zone.get("low", 0)), float(zone.get("high", 0))
        sl = float(payload.get("stop_loss_premium", 0))
        tgt = float(payload.get("target_premium", 0))
        mid = (low + high) / 2.0
        # Signed first, not abs() on both sides: for a long-premium trade the
        # stop must sit below entry and the target above it. Wrapping both
        # halves in abs() (as this was briefly inlined) makes a backwards
        # setup -- stop above entry, or target below it -- produce a
        # positive-looking ratio instead of getting caught, which the
        # deleted `implied_rr()` helper this replaced would have rejected.
        if sl > 0 and (mid - sl) != 0:
            rr = (tgt - mid) / (mid - sl)
        else:
            rr = 0.0
        if rr <= 0:
            return GateResult(False, f"RR {rr:.2f} <= 0 — stop/target on the wrong side of entry"), None
        if rr < 1.5:
            return GateResult(False, f"RR {rr:.2f} < 1.5"), None

        # Resolve the offset to a real contract from TODAY's master (§0.3).
        inst = self.instruments.resolve_offset(instrument, spot,
                                              str(payload.get("strike_offset", "ATM")), side)
        if inst is None:
            return GateResult(False, "could not resolve strike from instrument master"), None

        # Prefer a better contract nearby if the chain offers one (D-010).
        candidates = self.chain.candidates(instrument, side)
        chosen_symbol, chosen_premium, lot_size = inst.trading_symbol, mid, inst.lot_size
        if candidates:
            sel = self.settings.raw.get("strike_selection", {}) or {}
            best = select_strike(
                candidates,
                preferred_min=float(cfg.raw["sizing"].get("preferred_cost_min", 7000)),
                preferred_max=float(cfg.raw["sizing"].get("preferred_cost_max", 12000)),
                max_position_cost=cfg.sizing.max_position_cost,
                max_atm_offset=int(sel.get("max_atm_offset", 2)),
                weights=sel.get("weights"),
                min_open_interest=float(sel.get("min_open_interest", 0)),
                min_volume=float(sel.get("min_volume", 0)),
            )
            if best is not None and best.trading_symbol != inst.trading_symbol:
                # A different contract means the LLM's absolute premiums are wrong for
                # it. Rescale entry/stop/target by the SAME RATIOS the model chose,
                # anchored on the new contract's live premium. Previously the order
                # went out priced for a contract it was no longer buying — the stop
                # could sit ABOVE the entry and fire instantly.
                ratio_sl = sl / mid if mid > 0 else 0.0
                ratio_tgt = tgt / mid if mid > 0 else 0.0
                ratio_lo = low / mid if mid > 0 else 0.0
                ratio_hi = high / mid if mid > 0 else 0.0
                if not (0 < ratio_sl < 1 < ratio_tgt):
                    return GateResult(
                        False, "cannot rescale prices onto the substituted strike"), None
                chosen_symbol, chosen_premium, lot_size = (
                    best.trading_symbol, best.premium, best.lot_size
                )
                mid = chosen_premium
                low = round(chosen_premium * ratio_lo, 2)
                high = round(chosen_premium * ratio_hi, 2)
                sl = round(chosen_premium * ratio_sl, 2)
                tgt = round(chosen_premium * ratio_tgt, 2)
                inst = self.instruments.get(best.trading_symbol) or inst
                log.info("strike substituted, prices rescaled", extra={
                    "symbol": chosen_symbol, "premium": chosen_premium,
                    "entry": [low, high], "sl": sl, "target": tgt,
                })
            elif best is not None:
                chosen_symbol, chosen_premium, lot_size = (
                    best.trading_symbol, best.premium, best.lot_size
                )

        if lot_size <= 0:
            return GateResult(False, "lot size unknown for chosen contract"), None

        sl_points = max(0.05, chosen_premium - sl)
        sized = size_with_cap(
            max_risk=decision.max_risk, sl_points_premium=sl_points, lot_size=lot_size,
            premium=chosen_premium, capital=cfg.capital,
            max_position_cost=cfg.sizing.max_position_cost,
            min_lots=cfg.sizing.min_lots,
            max_lots=cfg.sizing.lots_ceiling(instrument),
        )
        if not sized.allowed:
            return GateResult(False, sized.reason), None

        q = QueuedSuggestion(
            id=str(uuid.uuid4()), trading_symbol=chosen_symbol, instrument=instrument,
            direction=side, strike=inst.strike, lots=sized.lots, qty=sized.qty,
            entry_low=low, entry_high=high, stop_loss=sl, target=tgt, confidence=conf,
            risk=sized.risk, cost=sized.cost,
            expires_at=datetime.now(UTC) + timedelta(seconds=TTL_SECONDS),
            over_risk=sized.over_risk, raw=payload,
        )
        return GateResult(True), q

    # ------------------------------------------------------------------ queue
    def enqueue(self, q: QueuedSuggestion, *, event_kind: str = "",
                model: str = "", event_id: int | None = None) -> None:
        # The card names what woke the model and which model answered — provenance the
        # trader can judge in the 90 seconds the card is alive.
        q.raw["_event"] = event_kind
        q.raw["_model"] = model
        with self._lock:
            self._queue[q.id] = q
        with session() as s:
            s.add(Suggestion(
                id=q.id, ts=utcnow(), event_id=event_id, event_kind=event_kind,
                model=model, action="SUGGEST", instrument=q.instrument,
                direction=q.direction, strike_offset=str(q.raw.get("strike_offset", "")),
                resolved_strike=q.strike, trading_symbol=q.trading_symbol,
                entry_low=q.entry_low, entry_high=q.entry_high,
                stop_loss_premium=q.stop_loss, target_premium=q.target,
                time_stop_minutes=int(q.raw.get("time_stop_minutes", 0)),
                confidence=q.confidence, thesis=str(q.raw.get("thesis", "")),
                invalidation=str(q.raw.get("invalidation", "")),
                risk_reward=float(q.raw.get("risk_reward", 0)),
                lots=q.lots, rupee_risk=q.risk, position_cost=q.cost,
                status="QUEUED", expires_at=q.expires_at,
                raw_json=json.dumps(q.raw, default=str),
            ))
        log.info("suggestion queued", extra={"id": q.id, "symbol": q.trading_symbol,
                                            "lots": q.lots, "risk": q.risk})
        self.alert("SUGGESTION", (
            f"{q.instrument} {q.direction} {q.strike:.0f} — "
            f"conf {q.confidence}, risk Rs.{q.risk:.0f}. Expires in 90s."
        ), {"suggestion_id": q.id})

    def record_gated(self, payload: dict[str, Any], reason: str, *,
                     event_kind: str = "", model: str = "") -> None:
        """NO_TRADE and GATED rows are free calibration data (§6.2)."""
        action = str(payload.get("action", "SUGGEST"))
        with session() as s:
            s.add(Suggestion(
                id=str(uuid.uuid4()), ts=utcnow(), event_kind=event_kind, model=model,
                action=action, instrument=str(payload.get("instrument", "")).upper(),
                direction=str(payload.get("direction", "")).upper(),
                confidence=int(payload.get("confidence", 0) or 0),
                thesis=str(payload.get("thesis", "") or payload.get("reason", "")),
                status="NO_TRADE" if action == "NO_TRADE" else "GATED",
                gate_reason=reason[:128], raw_json=json.dumps(payload, default=str),
            ))

    def active(self) -> list[QueuedSuggestion]:
        with self._lock:
            return [q for q in self._queue.values() if not q.expired]

    def expire_stale(self) -> list[str]:
        with self._lock:
            gone = [q.id for q in self._queue.values() if q.expired]
            for sid in gone:
                self._queue.pop(sid, None)
        if gone:
            with session() as s:
                for sid in gone:
                    row = s.get(Suggestion, sid)
                    if row and row.status == "QUEUED":
                        row.status = "EXPIRED"
            log.info("suggestions expired", extra={"count": len(gone)})
        return gone

    def reject(self, sid: str, reason: str = "") -> dict[str, Any]:
        with self._lock:
            self._queue.pop(sid, None)
        with session() as s:
            row = s.get(Suggestion, sid)
            if row is None:
                return {"ok": False, "error": "unknown suggestion"}
            if row.status != "QUEUED":
                return {"ok": False, "error": f"already {row.status}"}
            row.status = "REJECTED"
            row.gate_reason = (reason or "user rejected")[:128]
        log.info("suggestion rejected", extra={"id": sid, "reason": reason})
        return {"ok": True, "status": "REJECTED"}

    # ------------------------------------------------------------------ approve
    def approve(self, sid: str, *, idempotency_key: str = "") -> dict[str, Any]:
        """Approve-tap. Re-validates at tap time and places entry + SL."""
        with self._lock:
            if sid in self._approving:
                return {"ok": False, "error": "approval already in flight"}
            q = self._queue.get(sid)
            if q is None:
                return {"ok": False, "error": "unknown or already-handled suggestion"}
            if q.expired:
                self._queue.pop(sid, None)
                return {"ok": False, "error": "expired — market moved"}
            self._approving.add(sid)

        try:
            if not self.killswitch.guard_entry(session_date()):
                return {"ok": False, "error": "kill switch is OFF"}
            if self.orch.week_locked:
                return {"ok": False, "error": "week is locked"}

            expiring = self.instruments.expiring_today(
                [self.settings.instruments.primary, self.settings.instruments.secondary]
            )
            is_expiry = q.instrument in expiring

            # THE re-validation. State may have changed since the card appeared.
            # proposed_risk is deliberately NOT passed when the card is flagged
            # over_risk. The user's rule is that one lot is always allowed and merely
            # flagged; passing it here made the engine reject at tap time, so every
            # over_risk card was dead on arrival.
            decision = self.orch.can_enter(
                confidence=q.confidence, is_expiry_day=is_expiry,
                proposed_risk=None if q.over_risk else q.risk,
                active_trades=self.open_trades(),
                proposed_instrument=q.instrument,
            )
            if not decision.allowed:
                reason = decision.reason.value if decision.reason else "blocked"
                log.warning("approval rejected at tap time", extra={"id": sid,
                                                                    "reason": reason})
                with session() as s:
                    row = s.get(Suggestion, sid)
                    if row:
                        row.status = "GATED"
                        row.gate_reason = f"at tap: {reason}"[:128]
                with self._lock:
                    self._queue.pop(sid, None)
                return {"ok": False, "error": f"blocked at tap time: {reason}"}

            client_id = idempotency_key or str(uuid.uuid4())
            inst = self.instruments.get(q.trading_symbol)
            exchange = inst.exchange if inst else "NSE"

            entry_req = OrderRequest(
                trading_symbol=q.trading_symbol, exchange=exchange, segment="FNO",
                side=Side.BUY, quantity=q.qty, order_type=OrderType.LIMIT,
                price=q.entry_high, client_id=client_id, tag=sid,
            )
            self.guardian.register_own(client_id)
            try:
                entry_oid = self.broker.place_order(entry_req)
            except BrokerError as exc:
                log.error("entry order failed", extra={"id": sid, "error": str(exc)[:200]})
                self.alert("SYSTEM", f"Entry rejected: {str(exc)[:100]}", {"id": sid})
                return {"ok": False, "error": f"broker rejected: {str(exc)[:120]}"}

            self.guardian.register_own(client_id, entry_oid)
            self.orch.on_trade_opened()

            trade_id = str(uuid.uuid4())
            with session() as s:
                s.add(Trade(
                    id=trade_id, opened_at=utcnow(), session_date=session_date(),
                    origin="ai", suggestion_id=sid, instrument=q.instrument,
                    trading_symbol=q.trading_symbol, exchange=exchange, segment="FNO",
                    direction=q.direction, lots=q.lots,
                    lot_size=(inst.lot_size if inst else 0), qty=q.qty,
                    entry_price=q.entry_high, sl_price=q.stop_loss,
                    target_price=q.target, entry_order_id=entry_oid,
                    broker_order_ids=json.dumps([entry_oid]), status="OPEN",
                ))
                row = s.get(Suggestion, sid)
                if row:
                    row.status = "TAKEN"

            with self._lock:
                self._queue.pop(sid, None)

            # Confirm the fill BEFORE resting a stop. Previously the trade was booked
            # at the limit price and a full-size SL-M went out regardless, so an
            # unfilled entry produced phantom P&L and an orphan stop that could open
            # a naked short.
            filled_qty, avg_price = self._confirm_fill(entry_oid, q, client_id)
            if filled_qty <= 0:
                self._abandon_unfilled(trade_id, sid, entry_oid, q)
                return {"ok": False, "error": "entry did not fill — cancelled, slot refunded"}

            with session() as s:
                tr = s.get(Trade, trade_id)
                if tr:
                    tr.qty = filled_qty
                    tr.entry_price = avg_price
                    if tr.lot_size:
                        tr.lots = max(1, filled_qty // tr.lot_size)

            sl_oid = self._attach_stop(trade_id, q, exchange, quantity=filled_qty)
            log.warning("trade opened", extra={"trade_id": trade_id, "symbol": q.trading_symbol,
                                               "lots": q.lots, "entry_order": entry_oid,
                                               "sl_order": sl_oid})
            return {"ok": True, "trade_id": trade_id, "entry_order_id": entry_oid,
                    "sl_order_id": sl_oid, "lots": q.lots, "risk": q.risk,
                    "over_risk": q.over_risk}
        finally:
            with self._lock:
                self._approving.discard(sid)

    def _confirm_fill(self, entry_oid: str, q: QueuedSuggestion,
                      client_id: str) -> tuple[int, float]:
        """Poll the order until it fills, or give up. Returns (filled_qty, avg_price).

        Partial fills are honoured: the stop mirrors what actually filled (§8).
        """
        import time as _t
        deadline = _t.monotonic() + self.fill_wait_s
        last_qty, last_px = 0, 0.0
        first = True
        # Always inspect at least once: a MARKET order (or a LIMIT that crossed) is
        # already filled by the time we look, and a zero wait must not read as unfilled.
        while first or _t.monotonic() < deadline:
            first = False
            try:
                for o in self.broker.get_orders():
                    if o.order_id != entry_oid:
                        continue
                    last_qty = int(o.filled_quantity)
                    last_px = o.average_price or q.entry_high
                    if o.status is OrderStatus.FILLED and last_qty > 0:
                        return last_qty, last_px
                    if o.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                        return last_qty, last_px
            except BrokerError as exc:
                log.warning("fill check failed", extra={"error": str(exc)[:140]})
            _t.sleep(1.0)
        # Timed out. A partial counts as filled; nothing counts as unfilled.
        return last_qty, (last_px or q.entry_high)

    def _abandon_unfilled(self, trade_id: str, sid: str, entry_oid: str,
                          q: QueuedSuggestion) -> None:
        """Cancel the entry, delete the trade row, refund the trade slot."""
        try:
            self.broker.cancel_order(entry_oid, segment="FNO")
        except BrokerError as exc:
            log.warning("could not cancel unfilled entry",
                        extra={"order_id": entry_oid, "error": str(exc)[:140]})
        with session() as s:
            tr = s.get(Trade, trade_id)
            if tr:
                s.delete(tr)
            row = s.get(Suggestion, sid)
            if row:
                row.status = "EXPIRED"
                row.gate_reason = "entry never filled"
        self.orch.refund_trade_slot()
        log.warning("entry unfilled — cancelled and slot refunded",
                    extra={"symbol": q.trading_symbol, "order_id": entry_oid})
        self.alert("SYSTEM", (
            f"{q.trading_symbol} entry did not fill in {self.fill_wait_s}s — "
            f"cancelled, trade slot refunded."
        ), {"symbol": q.trading_symbol})

    def _attach_stop(self, trade_id: str, q: QueuedSuggestion, exchange: str,
                     *, quantity: int | None = None) -> str:
        """Place the resting SL-M immediately. A position without a stop is the
        one state this system must never sit in (§10.1)."""
        sl_client = str(uuid.uuid4())
        req = OrderRequest(
            trading_symbol=q.trading_symbol, exchange=exchange, segment="FNO",
            side=Side.SELL, quantity=int(quantity or q.qty), order_type=OrderType.SL_M,
            trigger_price=q.stop_loss, client_id=sl_client, tag=f"sl:{trade_id}",
        )
        self.guardian.register_own(sl_client)
        try:
            oid = self.broker.place_order(req)
        except BrokerError as exc:
            log.error("SL attach FAILED after entry", extra={"trade_id": trade_id,
                                                             "error": str(exc)[:200]})
            self.alert("SYSTEM", (
                f"{q.trading_symbol} is UNPROTECTED — entry filled but the stop was "
                f"rejected ({str(exc)[:70]}). Set an SL in the Groww app now."
            ), {"trade_id": trade_id})
            return ""
        self.guardian.register_own(sl_client, oid)
        with session() as s:
            t = s.get(Trade, trade_id)
            if t:
                t.sl_order_id = oid
                ids = json.loads(t.broker_order_ids or "[]")
                t.broker_order_ids = json.dumps([*ids, oid])
        return oid

    # ------------------------------------------------------------------ close out
    def close_trade(self, trade_id: str, exit_price: float, reason: str) -> dict[str, Any]:
        """Book a fill, compute net P&L with real costs, and feed the FSM."""
        with session() as s:
            t = s.get(Trade, trade_id)
            if t is None:
                return {"ok": False, "error": "unknown trade"}
            if t.status == "CLOSED":
                return {"ok": False, "error": "already closed"}
            gross, costs, net = net_pnl(t.entry_price, exit_price, t.qty,
                                        exchange=t.exchange or "NSE")
            t.exit_price = exit_price
            t.gross_pnl = gross
            t.costs = costs
            t.pnl = net
            t.exit_reason = reason[:32]
            t.closed_at = utcnow()
            t.status = "CLOSED"
            sid = t.suggestion_id
            symbol = t.trading_symbol

            if sid:
                row = s.get(Suggestion, sid)
                if row:
                    row.outcome = "WIN" if net > 0 else ("LOSS" if net < 0 else "FLAT")

        # Mark of everything still open, so closing one leg does not momentarily
        # re-price the day as realized-only (review finding #6).
        remaining = 0.0
        for p_ in self.broker.get_positions():
            if p_.is_open and p_.trading_symbol != symbol:
                remaining += p_.unrealized_pnl
        snap = self.orch.on_trade_closed(net, remaining_unrealized=remaining)
        log.warning("trade closed", extra={"trade_id": trade_id, "symbol": symbol,
                                          "gross": gross, "costs": costs, "net": net,
                                          "reason": reason, "state": snap.state})
        return {"ok": True, "gross": gross, "costs": costs, "net": net,
                "state": snap.state, "day_pnl": snap.day_pnl}

    def _exit_price(self, symbol: str, fallback: float) -> float:
        """Never book a fill at 0.0 — that fabricates a 100% loss into the FSM."""
        for p in self.broker.get_positions():
            if p.trading_symbol == symbol and p.last_price > 0:
                return p.last_price
        try:
            q = self.broker.get_quote([symbol])
            px = q.get(symbol)
            if px and px.last_price > 0:
                return px.last_price
        except Exception as exc:
            log.warning("exit price lookup failed", extra={"symbol": symbol,
                                                           "error": str(exc)[:120]})
        log.error("no exit price available — falling back to entry",
                  extra={"symbol": symbol})
        return fallback

    def settle_broker_exits(self) -> list[str]:
        """Book exits the system did not initiate. THE money-path gap.

        Nothing detects a broker-side SL fill in LIVE mode: guardian ignores SELL
        orders, `trigger_stops` is sim-only, and recon merely stamped the row CLOSED
        with pnl=0.0. So a full stop-out registered as Rs.0 realized — the daily loss
        lock could never trip, the consecutive-loss stop never fired, and the trade
        budget was silently refunded.

        Called every guardian tick. For each locally-OPEN trade the broker no longer
        holds, find the actual exit fill and route it through close_trade so the FSM
        sees the money.
        """
        settled: list[str] = []
        local = self.open_trades()
        if not local:
            return settled
        try:
            held = {p.trading_symbol for p in self.broker.get_positions() if p.is_open}
            book = self.broker.get_orders()
        except BrokerError as exc:
            log.warning("cannot settle broker exits", extra={"error": str(exc)[:160]})
            return settled

        for t_ in local:
            if t_.trading_symbol in held:
                continue
            exits = [
                o for o in book
                if o.trading_symbol == t_.trading_symbol
                and o.side is Side.SELL
                and o.status in (OrderStatus.FILLED, OrderStatus.PARTIAL)
                and o.filled_quantity > 0
            ]
            if exits:
                fill = max(exits, key=lambda o: o.created_at or datetime.min.replace(tzinfo=UTC))
                price = fill.average_price or fill.price or fill.trigger_price
                reason = ("stop_loss" if fill.order_type in (OrderType.SL, OrderType.SL_M)
                          else "broker_exit")
            else:
                price, reason = 0.0, "vanished"
            price = price or self._exit_price(t_.trading_symbol, t_.entry_price)

            out = self.close_trade(t_.id, price, reason)
            if out.get("ok"):
                settled.append(t_.id)
                log.warning("booked a broker-side exit", extra={
                    "trade_id": t_.id, "symbol": t_.trading_symbol,
                    "exit": price, "reason": reason, "net": out.get("net"),
                    "state": out.get("state"),
                })
                self.alert("GUARDIAN", (
                    f"{t_.trading_symbol} exited at {price:.2f} ({reason}) — "
                    f"net {out.get('net', 0):+,.0f}."
                ), {"trade_id": t_.id})
        return settled

    def open_trades(self) -> list[Trade]:
        with session() as s:
            return list(s.query(Trade).filter(Trade.status == "OPEN").all())

    def check_targets(self) -> list[str]:
        """Exit at target by watching the premium, per design §8.

        Deliberately NOT a resting target order. A second live exit order would sit
        alongside the trader's own stop and could double-sell into a short. Design §8
        says "tick-watch target/time-stop", and the trader's rule is: never override
        what they placed. So the target is monitored and exited with a MARKET order,
        and nothing of theirs is touched.
        """
        exited: list[str] = []
        for t_ in self.open_trades():
            if not t_.target_price or t_.target_price <= 0:
                continue
            px = self._live_price(t_.trading_symbol)
            if px <= 0 or px < t_.target_price:
                continue
            pos = next((x for x in self.broker.get_positions()
                        if x.trading_symbol == t_.trading_symbol and x.is_open), None)
            if pos is None:
                continue
            log.warning("target reached", extra={
                "trade_id": t_.id, "symbol": t_.trading_symbol,
                "target": t_.target_price, "ltp": px,
            })
            if self._market_exit(t_, pos, tag="target"):
                self.close_trade(t_.id, self._exit_price(t_.trading_symbol, px), "target")
                exited.append(t_.id)
        return exited

    def _live_price(self, symbol: str) -> float:
        for p_ in self.broker.get_positions():
            if p_.trading_symbol == symbol and p_.last_price > 0:
                return p_.last_price
        try:
            q = self.broker.get_quote([symbol]).get(symbol)
            return q.last_price if q else 0.0
        except Exception:
            return 0.0

    def _market_exit(self, trade, pos, *, tag: str) -> bool:
        """Flatten exactly this contract. Cancels only stops WE placed, never theirs."""
        self._cancel_own_stops_for(trade)
        req = OrderRequest(
            trading_symbol=trade.trading_symbol,
            exchange=pos.exchange or trade.exchange or "NSE",
            segment=pos.segment or "FNO",
            side=Side.SELL if pos.quantity > 0 else Side.BUY,
            quantity=abs(int(pos.quantity)),
            order_type=OrderType.MARKET,
            client_id=str(uuid.uuid4()), tag=f"{tag}:{trade.id}",
        )
        self.guardian.register_own(req.client_id)
        try:
            oid = self.broker.place_order(req)
        except BrokerError as exc:
            log.error("market exit failed", extra={"trade_id": trade.id,
                                                  "error": str(exc)[:180]})
            self.alert("SYSTEM", (
                f"{trade.trading_symbol} {tag} exit REJECTED ({str(exc)[:70]}). "
                f"Exit manually in the Groww app."
            ), {"trade_id": trade.id})
            return False
        self.guardian.register_own(req.client_id, oid)
        return True

    def _cancel_own_stops_for(self, trade) -> None:
        """Cancel only the stop VECTRA_QUANT rested for this trade. Yours is left alone."""
        if not trade.sl_order_id:
            return
        try:
            self.broker.cancel_order(trade.sl_order_id, segment=trade.segment or "FNO")
            log.info("cancelled our own stop before exit",
                     extra={"order_id": trade.sl_order_id})
        except BrokerError as exc:
            log.warning("could not cancel our stop before exit",
                        extra={"order_id": trade.sl_order_id, "error": str(exc)[:140]})

    def check_time_stops(self) -> list[str]:
        """Exit positions whose thesis has run out of clock (§6.2 time_stop_minutes)."""
        closed: list[str] = []
        now = datetime.now(UTC)
        for t in self.open_trades():
            with session() as s:
                row = s.get(Suggestion, t.suggestion_id) if t.suggestion_id else None
                minutes = int(row.time_stop_minutes) if row else 0
            if minutes <= 0:
                continue
            opened = t.opened_at if t.opened_at.tzinfo else t.opened_at.replace(tzinfo=UTC)
            if now - opened < timedelta(minutes=minutes):
                continue
            log.warning("time stop hit", extra={"trade_id": t.id, "minutes": minutes})
            pos = next((x for x in self.broker.get_positions()
                        if x.trading_symbol == t.trading_symbol and x.is_open), None)
            if pos is None:
                self.close_trade(t.id, t.entry_price, "time_stop_already_flat")
                closed.append(t.id)
                continue
            # Exit ONLY this contract, cancelling only our own stop.
            if not self._market_exit(t, pos, tag="timestop"):
                continue
            price = self._exit_price(t.trading_symbol, t.entry_price)
            self.close_trade(t.id, price, "time_stop")
            closed.append(t.id)
        return closed

    def in_entry_window(self) -> bool:
        t = now_ist().time()
        return self.settings.risk.entry_open <= t < self.settings.risk.entry_close

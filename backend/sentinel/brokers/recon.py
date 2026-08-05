"""Reconciler — the three-source-truth rule. Design doc §3.4, build plan §0.5.

REST order-book poll (truth, every 15s) > order stream (speed) > local DB (belief).
Any mismatch: REST wins, discrepancy logged, alert raised if positions disagree.

This single rule is what prevents "system thinks position closed, broker says open".
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sentinel.brokers.base import BrokerError, Order, OrderStatus, Position
from sentinel.db import Trade, session
from sentinel.logging_setup import get

log = get("brokers.recon")


@dataclass
class Discrepancy:
    kind: str            # ORPHAN_POSITION | GHOST_TRADE | QTY_MISMATCH | STATUS_MISMATCH
    symbol: str
    detail: str
    broker_value: str = ""
    local_value: str = ""
    critical: bool = False


@dataclass
class ReconResult:
    ts: datetime
    broker_positions: int
    open_trades: int
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.discrepancies

    @property
    def has_critical(self) -> bool:
        return any(d.critical for d in self.discrepancies)


class Reconciler:
    """Polls REST truth and corrects local belief. Never places or cancels orders."""

    def __init__(
        self,
        broker,  # noqa: ANN001 - BrokerAdapter
        *,
        on_discrepancy: Callable[[list[Discrepancy]], None] | None = None,
    ):
        self.broker = broker
        self._on_discrepancy = on_discrepancy
        self._lock = threading.Lock()
        self.last_result: ReconResult | None = None
        self.last_ok_at: datetime | None = None
        self.consecutive_failures = 0

    def run_once(self) -> ReconResult:
        with self._lock:
            try:
                positions = [p for p in self.broker.get_positions() if p.is_open]
                orders = self.broker.get_orders()
            except BrokerError as exc:
                self.consecutive_failures += 1
                log.error("recon poll failed", extra={
                    "error": str(exc)[:200], "consecutive": self.consecutive_failures,
                })
                raise

            self.consecutive_failures = 0
            self.last_ok_at = datetime.now(UTC)
            result = self._compare(positions, orders)
            self.last_result = result

            if result.discrepancies:
                for d in result.discrepancies:
                    log.warning("recon discrepancy", extra={
                        "kind": d.kind, "symbol": d.symbol, "detail": d.detail,
                        "broker": d.broker_value, "local": d.local_value,
                        "critical": d.critical,
                    })
                if self._on_discrepancy:
                    self._on_discrepancy(result.discrepancies)
            return result

    def _compare(self, positions: list[Position], orders: list[Order]) -> ReconResult:
        by_symbol = {p.trading_symbol: p for p in positions}
        order_status = {o.order_id: o for o in orders}
        discrepancies: list[Discrepancy] = []

        with session() as s:
            open_trades = list(s.query(Trade).filter(Trade.status == "OPEN").all())

            local_symbols = set()
            for t in open_trades:
                local_symbols.add(t.trading_symbol)
                broker_pos = by_symbol.get(t.trading_symbol)

                if broker_pos is None:
                    # Broker says flat, we think we hold it. REST wins.
                    discrepancies.append(Discrepancy(
                        kind="GHOST_TRADE", symbol=t.trading_symbol,
                        detail="local trade OPEN but broker reports no position; closing locally",
                        broker_value="flat", local_value=f"qty={t.qty}", critical=True,
                    ))
                    t.status = "CLOSED"
                    t.closed_at = datetime.now(UTC)
                    t.exit_reason = t.exit_reason or "recon_ghost"
                    continue

                if abs(broker_pos.quantity) != abs(t.qty):
                    discrepancies.append(Discrepancy(
                        kind="QTY_MISMATCH", symbol=t.trading_symbol,
                        detail="broker quantity differs from local; adopting broker",
                        broker_value=str(broker_pos.quantity), local_value=str(t.qty),
                        critical=True,
                    ))
                    t.qty = abs(broker_pos.quantity)
                    if t.lot_size:
                        t.lots = max(1, t.qty // t.lot_size)

                # A stop we believe is resting must actually be resting.
                if t.sl_order_id:
                    o = order_status.get(t.sl_order_id)
                    if o is not None and o.is_terminal and o.status is not OrderStatus.FILLED:
                        discrepancies.append(Discrepancy(
                            kind="STATUS_MISMATCH", symbol=t.trading_symbol,
                            detail=f"SL order is {o.status.value}; position is unprotected",
                            broker_value=o.status.value, local_value="assumed resting",
                            critical=True,
                        ))

            # Broker holds something we have no record of at all.
            for sym, pos in by_symbol.items():
                if sym not in local_symbols:
                    discrepancies.append(Discrepancy(
                        kind="ORPHAN_POSITION", symbol=sym,
                        detail="broker position with no local trade row (manual trade?)",
                        broker_value=f"qty={pos.quantity}", local_value="none",
                        critical=True,
                    ))

        return ReconResult(
            ts=datetime.now(UTC),
            broker_positions=len(positions),
            open_trades=len(open_trades),
            discrepancies=discrepancies,
        )

    def verify_flat(self, *, timeout_s: int = 10, interval_s: float = 1.0) -> bool:
        """After square-off, confirm we are actually flat via REST (§8, §10)."""
        import time as _t

        deadline = _t.monotonic() + timeout_s
        while _t.monotonic() < deadline:
            try:
                if not [p for p in self.broker.get_positions() if p.is_open]:
                    log.info("verified flat")
                    return True
            except BrokerError as exc:
                log.warning("flat check failed", extra={"error": str(exc)[:160]})
            _t.sleep(interval_s)
        log.error("NOT FLAT after square-off window", extra={"timeout_s": timeout_s})
        return False

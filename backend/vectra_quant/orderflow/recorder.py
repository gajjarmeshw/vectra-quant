"""Async recorder: connects to Dhan's 200-level depth feed for the current
NIFTY future, feeds every update into a `SessionAggregator`, and persists
raw ticks via `DepthParquetWriter`.

NOT independently tested against a live connection -- cannot be, without
real Dhan credentials and market hours. Everything it calls into
(`aggregator.py`, `storage.py`, `charts.py`, `depth_parser.py`) IS unit
tested; this module is the thin, deliberately minimal glue on top. Treat a
successful `pytest` run of this package as "the logic is right", not as
"this will work against the real feed" -- that can only be confirmed by
actually running it against Dhan during market hours and inspecting what
comes back.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from vectra_quant.core.session_clock import now_ist
from vectra_quant.orderflow.aggregator import SessionAggregator
from vectra_quant.orderflow.config import OrderFlowCfg
from vectra_quant.orderflow.contract import ResolvedContract, needs_rollover, resolve_current_nifty_future
from vectra_quant.orderflow.storage import DepthParquetWriter, DepthTickRow

log = logging.getLogger("orderflow.recorder")

NSE_FNO_EXCHANGE_CODE = 2  # matches dhanhq.FullDepth.NSE_FNO


@dataclass
class RecorderHealth:
    connected: bool = False
    last_update_at: datetime | None = None
    last_error: str | None = None
    reconnect_count: int = 0

    def is_stale(self, now: datetime, stale_after_seconds: float = 60.0) -> bool:
        if self.last_update_at is None:
            return False
        return (now - self.last_update_at).total_seconds() > stale_after_seconds


class DepthRecorder:
    """One instance per trading day (or long-running with daily reset)."""

    def __init__(
        self, dhan_context: Any, instrument_master_provider, cfg: OrderFlowCfg,
        storage_root: str, on_stale: "callable[[RecorderHealth], None] | None" = None,
    ):
        self.dhan_context = dhan_context
        self._instrument_master_provider = instrument_master_provider  # callable -> list[Instrument]
        self.cfg = cfg
        self._storage_root = Path(storage_root)
        self.writer = DepthParquetWriter(root=storage_root)
        self.aggregator = SessionAggregator(
            percentile=cfg.big_order_percentile,
            min_samples=cfg.big_order_min_samples,
            interval_seconds=cfg.imbalance_interval_seconds,
        )
        self.health = RecorderHealth()
        self._on_stale = on_stale
        self._contract: ResolvedContract | None = None
        self._stop = False
        self._ticks_since_health_write = 0

    def _health_path(self) -> Path:
        d = self._storage_root / "recorder_health"
        d.mkdir(parents=True, exist_ok=True)
        return d / "health.json"

    def _write_health(self) -> None:
        """Cross-process visibility: this recorder runs in its own script,
        separate from the main FastAPI app -- the only way the app's API
        (and the UI) can show connection state is to read this file, not
        `self.health` in memory, which belongs to a different process."""
        payload = {
            "connected": self.health.connected,
            "last_update_at": self.health.last_update_at.isoformat() if self.health.last_update_at else None,
            "last_error": self.health.last_error,
            "reconnect_count": self.health.reconnect_count,
            "contract": self._contract.instrument.trading_symbol if self._contract else None,
        }
        try:
            self._health_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _ensure_contract(self, today: date) -> ResolvedContract:
        if self._contract is None or needs_rollover(self._contract, today):
            instruments = self._instrument_master_provider()
            self._contract = resolve_current_nifty_future(instruments, today)
            log.info("Recording depth for %s (expiry %s)", self._contract.instrument.trading_symbol, self._contract.expiry)
        return self._contract

    def _within_session(self, now: time) -> bool:
        return self.cfg.recorder_start_time <= now <= self.cfg.recorder_stop_time

    def _ingest_update(self, update: dict[str, Any]) -> None:
        """`update` is one parsed depth message: the dhanhq SDK's own dict
        shape -- {"security_id", "type": "Bid"/"Ask", "depth": [{"price",
        "quantity","orders"}, ...]}."""
        now = now_ist()
        side = "BID" if update.get("type", "").lower().startswith("bid") else "ASK"
        levels = [(d["price"], d["quantity"], d["orders"]) for d in update.get("depth", [])]

        rows = [
            DepthTickRow(ts=now, security_id=update["security_id"], side=side,
                         level_idx=i, price=p, qty=q, orders=o)
            for i, (p, q, o) in enumerate(levels)
        ]
        self.writer.add_many(rows)
        self.aggregator.observe(now, side, levels)

        self.health.connected = True
        self.health.last_update_at = now

        self._ticks_since_health_write += 1
        if self._ticks_since_health_write >= 50:
            self._ticks_since_health_write = 0
            self._write_health()

    async def run_forever(self) -> None:
        """Reconnect loop with backoff. Stops itself outside the configured
        session window and on `stop()`.

        Uses `dhanhq.FullDepth` for the actual WebSocket handshake/auth/
        subscribe (officially maintained) rather than re-implementing it --
        this module owns aggregation + persistence + reconnect policy only.
        """
        if not self.cfg.allow_live_recording:
            # Dhan drops the OLDEST depth socket on the account past the
            # 5-connection cap (error 805). The EC2 capture service holds that
            # whole budget in market hours, so a recorder started elsewhere on
            # the same credentials silently kills a production feed. Opt in with
            # ORDERFLOW_ALLOW_LIVE_RECORDING=1 on the host that owns the budget.
            raise RuntimeError(
                "DepthRecorder.run_forever() refused: live depth recording is disabled "
                "(cfg.allow_live_recording=False). Set ORDERFLOW_ALLOW_LIVE_RECORDING=1 "
                "only on the host that owns the account's Dhan connection budget."
            )

        from dhanhq import FullDepth  # local import: optional dependency for callers who only need the pure logic

        backoff_idx = 0
        while not self._stop:
            now = now_ist()
            if not self._within_session(now.time()):
                await asyncio.sleep(30)
                continue

            contract = self._ensure_contract(now.date())
            security_id = contract.instrument.exchange_token

            depth = FullDepth(self.dhan_context, instruments=[(NSE_FNO_EXCHANGE_CODE, security_id)], depth_level=200)
            try:
                await depth.connect()
                self.health.connected = True
                backoff_idx = 0
                # `get_instrument_data()` is a red herring as an "async
                # generator you iterate forever": internally it calls
                # `self.ws.recv()` exactly ONCE, yields whatever messages
                # were packed into that single WS frame, then ends --
                # ws.recv() is never called again inside it. Treating one
                # exhaustion as "the remote closed the connection" (as this
                # loop used to) tore down and rebuilt the WebSocket on
                # every single frame, which both (a) produced the
                # reconnect-every-~1s storm in the logs, and (b) very
                # likely explains an observed ~20:1 bid:ask tick imbalance:
                # a fresh resubscribe's first frame is consistently the bid
                # snapshot, and the connection was being killed before the
                # matching ask frame ever arrived on it. The real, live
                # `depth.ws` connection is untouched by one call ending --
                # keep re-invoking `get_instrument_data()` on it instead of
                # recreating `depth` each time. A genuine disconnect still
                # raises out of `ws.recv()` into the except block below.
                while not self._stop and self._within_session(now_ist().time()):
                    async for update in depth.get_instrument_data():
                        if self._stop or not self._within_session(now_ist().time()):
                            break
                        self._ingest_update(update)
                        if self.health.is_stale(now_ist(), stale_after_seconds=self.cfg.ws_ping_timeout_seconds):
                            if self._on_stale:
                                self._on_stale(self.health)
            except Exception as exc:  # noqa: BLE001 -- reconnect on anything, log what it was
                self.health.connected = False
                self.health.last_error = str(exc)
                self.health.reconnect_count += 1
                delay = self.cfg.reconnect_backoff_seconds[min(backoff_idx, len(self.cfg.reconnect_backoff_seconds) - 1)]
                backoff_idx += 1
                log.warning("Depth feed disconnected (%s) -- reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)
            finally:
                try:
                    await depth.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._write_health()

        self.writer.close()

    def stop(self) -> None:
        self._stop = True

    def end_of_day_reset(self) -> None:
        """Chart 1's big-order store resets daily per the spec -- call this
        once at session start (or midnight) before the next day's data
        starts arriving."""
        self.aggregator.reset_daily()

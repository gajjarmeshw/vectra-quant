"""Multi-instrument 20-level depth recorder: one Dhan connection subscribed
to many instruments at once (a NIFTY100 equities batch, or a NIFTY
option-chain window), persisting raw ticks and maintaining a live
top-of-book snapshot per instrument for the breadth signal to read.

Dhan's 20-depth feed batches at most 50 instruments per connection (see
`dhanhq.fulldepth.FullDepth.validate_and_process_tuples`) -- a 100-stock
universe needs two of these run concurrently as separate connections,
exactly matching the topology Arjun's own capture used (verified by
successfully decoding his real capture with that assumption).

Bakes in two fixes discovered the hard way building the single-instrument
200-depth recorder (see `recorder.py`'s own history, and this session's
production-readiness pass):
  - `get_instrument_data()` is ONE `ws.recv()` call, not a persistent
    stream. Reconnecting every time it exhausts normally causes a
    reconnect-every-second storm and starves one side of the book (a
    fresh resubscribe's first frame is consistently the bid snapshot).
    Keep re-invoking it on the same live connection instead.
  - all session-boundary checks use `now_ist()`, never naive
    `datetime.now()` (reads UTC on this host, silently never matches an
    IST session window at all).

NOT independently tested against a live connection -- cannot be, without
real Dhan credentials and market hours. `_ingest_update` and the
top-of-book snapshot logic ARE unit tested; `run_forever`'s connection
handling mirrors `recorder.py`'s already-verified-live pattern exactly.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from vectra_quant.brokers.base import Instrument
from vectra_quant.core.session_clock import now_ist
from vectra_quant.orderflow.config import OrderFlowCfg
from vectra_quant.orderflow.storage import DepthParquetWriter, DepthTickRow

log = logging.getLogger("orderflow.multi_recorder")

MAX_INSTRUMENTS_PER_CONNECTION = 50  # Dhan's own 20-depth batching limit


@dataclass
class MultiRecorderHealth:
    connected: bool = False
    last_update_at: datetime | None = None
    last_error: str | None = None
    reconnect_count: int = 0
    n_instruments: int = 0


class MultiInstrumentDepthRecorder:
    def __init__(
        self, dhan_context: Any, instruments: list[Instrument], cfg: OrderFlowCfg,
        storage_root: str, connection_label: str, depth_level: int = 20,
    ):
        if len(instruments) > MAX_INSTRUMENTS_PER_CONNECTION:
            raise ValueError(
                f"{len(instruments)} instruments exceeds Dhan's {MAX_INSTRUMENTS_PER_CONNECTION}-per-connection "
                f"limit for the {depth_level}-depth feed -- split across multiple connections/labels"
            )
        self.dhan_context = dhan_context
        self.instruments = instruments
        self.depth_level = depth_level
        self.cfg = cfg
        self._storage_root = Path(storage_root)
        self._label = connection_label
        self.writer = DepthParquetWriter(root=str(self._storage_root / "depth_multi" / connection_label))
        self._sid_to_symbol: dict[int, str] = {int(i.exchange_token): i.trading_symbol for i in instruments}
        self._top_of_book: dict[str, dict[str, Any]] = {}
        self.health = MultiRecorderHealth(n_instruments=len(instruments))
        self._stop = False
        self._ticks_since_health_write = 0

    def _health_path(self) -> Path:
        d = self._storage_root / "recorder_health"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"health_{self._label}.json"

    def _write_health(self) -> None:
        payload = {
            "label": self._label,
            "connected": self.health.connected,
            "last_update_at": self.health.last_update_at.isoformat() if self.health.last_update_at else None,
            "last_error": self.health.last_error,
            "reconnect_count": self.health.reconnect_count,
            "n_instruments": self.health.n_instruments,
        }
        try:
            self._health_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _within_session(self, now: time) -> bool:
        return self.cfg.recorder_start_time <= now <= self.cfg.recorder_stop_time

    def _ingest_update(self, update: dict[str, Any]) -> None:
        """`update` is one parsed depth message: {"security_id", "type":
        "Bid"/"Ask", "depth": [{"price","quantity","orders"}, ...]}."""
        now = now_ist()
        sid = update.get("security_id")
        symbol = self._sid_to_symbol.get(sid)
        if symbol is None:
            return  # a broadcast for an instrument we didn't ask for -- ignore, don't crash
        side = "BID" if update.get("type", "").lower().startswith("bid") else "ASK"
        levels = [(d["price"], d["quantity"], d["orders"]) for d in update.get("depth", [])]

        rows = [
            DepthTickRow(ts=now, security_id=sid, side=side, level_idx=i, price=p, qty=q, orders=o)
            for i, (p, q, o) in enumerate(levels)
        ]
        self.writer.add_many(rows)

        top_price, top_qty = (levels[0][0], levels[0][1]) if levels else (0.0, 0.0)
        # A torn-down or not-yet-quoted book arrives as price 0 with a
        # quantity attached. 6% of the rows in the one real multi-stock
        # capture we have look like this, and because the breadth signal
        # reads quantities only it would take them for a genuine lean.
        if levels and top_price > 0:
            snap = self._top_of_book.setdefault(symbol, {"bid_qty": 0.0, "ask_qty": 0.0, "ts": now})
            if side == "BID":
                snap["bid_qty"] = top_qty
            else:
                snap["ask_qty"] = top_qty
            snap["ts"] = now

        self.health.connected = True
        self.health.last_update_at = now
        self._ticks_since_health_write += 1
        if self._ticks_since_health_write >= 200:
            self._ticks_since_health_write = 0
            self._write_health()

    def top_of_book_snapshot(self, max_age_s: float = 30.0) -> dict[str, tuple[float, float]]:
        """Live (bid_qty, ask_qty) at level 0 for every instrument quoting
        *now* -- what the breadth signal reads.

        Entries older than `max_age_s` are dropped. Without that this returned
        the last value ever seen for a symbol, forever: a stock that stopped
        quoting, or a whole websocket connection that died, kept contributing
        its frozen imbalance to the cross-sectional mean and kept counting
        toward `n_stocks`. Breadth would happily report "100 stocks reporting"
        off a feed that had been dead for an hour, which is the same way the
        recorder-health panel reported a twelve-hour-old file as live.
        """
        cutoff = now_ist() - timedelta(seconds=max_age_s)
        return {
            sym: (v["bid_qty"], v["ask_qty"])
            for sym, v in self._top_of_book.items()
            if v["ts"] >= cutoff
        }

    async def run_forever(self) -> None:
        from dhanhq import FullDepth  # local import: optional dependency

        exch_and_sid = [(_exchange_code(i.exchange, i.segment), i.exchange_token) for i in self.instruments]

        backoff_idx = 0
        while not self._stop:
            now = now_ist()
            if not self._within_session(now.time()):
                await asyncio.sleep(30)
                continue

            depth = FullDepth(self.dhan_context, instruments=exch_and_sid, depth_level=self.depth_level)
            try:
                await depth.connect()
                self.health.connected = True
                backoff_idx = 0
                while not self._stop and self._within_session(now_ist().time()):
                    async for update in depth.get_instrument_data():
                        if self._stop or not self._within_session(now_ist().time()):
                            break
                        self._ingest_update(update)
            except Exception as exc:  # noqa: BLE001 -- reconnect on anything, log what it was
                self.health.connected = False
                self.health.last_error = str(exc)
                self.health.reconnect_count += 1
                delay = self.cfg.reconnect_backoff_seconds[min(backoff_idx, len(self.cfg.reconnect_backoff_seconds) - 1)]
                backoff_idx += 1
                log.warning("[%s] Depth feed disconnected (%s) -- reconnecting in %.1fs", self._label, exc, delay)
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


def _exchange_code(exchange: str, segment: str) -> int:
    """dhanhq.FullDepth.NSE=1, NSE_FNO=2 -- matches the vendor SDK constants."""
    seg = (segment or exchange or "").upper()
    if "FNO" in seg:
        return 2
    return 1

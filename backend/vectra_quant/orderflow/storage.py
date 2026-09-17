"""Parquet writer for recorded depth ticks, partitioned by date.

Layout: `<root>/depth/date=YYYY-MM-DD/security_id=<id>/part-<n>.parquet`
Each row is one (timestamp, side, price, qty, orders) observation from one
depth packet update -- one row per level, not one row per packet, so a
downstream reader can filter/aggregate with plain columnar operations.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class DepthTickRow:
    ts: datetime
    security_id: int
    side: str        # "BID" or "ASK"
    level_idx: int    # 0 = best, per the order the feed sent it in
    price: float
    qty: int
    orders: int


_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us")),
    ("security_id", pa.int64()),
    ("side", pa.string()),
    ("level_idx", pa.int32()),
    ("price", pa.float64()),
    ("qty", pa.int64()),
    ("orders", pa.int64()),
])


class DepthParquetWriter:
    """Buffers rows in memory and flushes to a new Parquet part-file.

    One writer instance is meant to live for one recording session (one
    trading day); call `flush()` periodically (e.g. every N rows or every
    M seconds from the caller's event loop) and `close()` at session end.
    """

    def __init__(self, root: str | Path, flush_every: int = 5000):
        self.root = Path(root)
        self.flush_every = flush_every
        self._buffer: list[DepthTickRow] = []
        self._part_counters: dict[tuple[str, int], int] = {}

    def add(self, row: DepthTickRow) -> None:
        self._buffer.append(row)
        if len(self._buffer) >= self.flush_every:
            self.flush()

    def add_many(self, rows: list[DepthTickRow]) -> None:
        self._buffer.extend(rows)
        if len(self._buffer) >= self.flush_every:
            self.flush()

    def flush(self) -> Path | None:
        if not self._buffer:
            return None

        by_partition: dict[tuple[str, int], list[DepthTickRow]] = {}
        for row in self._buffer:
            key = (row.ts.strftime("%Y-%m-%d"), row.security_id)
            by_partition.setdefault(key, []).append(row)

        last_path: Path | None = None
        for (date_str, security_id), rows in by_partition.items():
            part_dir = self.root / "depth" / f"date={date_str}" / f"security_id={security_id}"
            part_dir.mkdir(parents=True, exist_ok=True)

            n = self._part_counters.get((date_str, security_id), 0)
            part_path = part_dir / f"part-{n:05d}.parquet"
            while part_path.exists():
                n += 1
                part_path = part_dir / f"part-{n:05d}.parquet"
            self._part_counters[(date_str, security_id)] = n + 1

            table = pa.Table.from_pylist([
                {"ts": r.ts, "security_id": r.security_id, "side": r.side,
                 "level_idx": r.level_idx, "price": r.price, "qty": r.qty, "orders": r.orders}
                for r in rows
            ], schema=_SCHEMA)
            pq.write_table(table, part_path)
            last_path = part_path

        self._buffer.clear()
        return last_path

    def close(self) -> Path | None:
        return self.flush()


def write_session_context(root: str | Path, session_date: date, context: dict[str, Any]) -> Path:
    """Persist the same-day inputs a live run used but that the raw depth
    ticks alone don't capture: the realized-vol proxy series, prior-session
    VIX, and the daily candles it was derived from. Without this, a future
    backtest replaying the recorded depth ticks would have to re-fetch these
    from a live API call that may return different values by then (or not
    exist at all once the historical window has rolled past), silently
    changing what "backtesting today's recording" would mean.

    One JSON file per day, at `<root>/context/date=<date>/context.json`.
    """
    day_dir = Path(root) / "context" / f"date={session_date.isoformat()}"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / "context.json"
    path.write_text(json.dumps(context, indent=2, default=str), encoding="utf-8")
    return path

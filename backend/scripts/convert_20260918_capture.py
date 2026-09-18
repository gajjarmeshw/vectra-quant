#!/usr/bin/env python
"""One-off converter: today's (2026-09-18) raw Dhan depth captures, pulled
from the iea-capture S3 bucket, into partitioned Parquet for backtesting.

Reuses the two ALREADY-VERIFIED parsers in this repo rather than guessing a
new binary layout:
  - Outer per-frame record structure (8-byte wall-clock ts + 4-byte payload
    length + payload) and the 20-depth packet body decode (payload_len //
    16, feed_code 41/51) -- exactly `scripts/parse_arjun_depth20.py`'s
    reverse-engineered format, reused for depth20 equities + depth20_options.
  - The 200-depth packet body decode (trusts the header's row_count field)
    -- `vectra_quant.orderflow.depth_parser`, verified against the dhanhq
    v2.2.0 SDK source -- reused for depth200_futures.

Deliberately does NOT touch marketfeed_spot_vix/conn0.bin: that feed uses
Dhan's separate "Quote mode" wire format, which nothing in this repo
documents or parses. Guessing a layout for it risks silently wrong data
being fed into a backtest -- skip it until the real format is confirmed.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectra_quant.orderflow.depth_parser import parse_depth_frame  # noqa: E402

HEADER_STRUCT = struct.Struct("<hBBiI")
LEVEL_STRUCT = struct.Struct("<dII")
HEADER_SIZE = HEADER_STRUCT.size  # 12
LEVEL_SIZE = LEVEL_STRUCT.size    # 16
FEED_CODE_BID = 41
FEED_CODE_ASK = 51

SCHEMA = pa.schema([
    ("ts", pa.timestamp("ns", tz="UTC")),
    ("symbol", pa.string()),
    ("security_id", pa.int64()),
    ("side", pa.string()),
    ("level_idx", pa.int32()),
    ("price", pa.float64()),
    ("qty", pa.int64()),
    ("orders", pa.int64()),
])
PART_SCHEMA = pa.schema([f for f in SCHEMA if f.name != "symbol"])

FLUSH_EVERY_ROWS = 500_000

RAW_ROOT = Path(__file__).resolve().parent.parent / "data" / "raw_pull" / "dhan"
OUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "orderflow" / "pulled_20260918"
DATE = "2026-09-18"


def load_instrument_map(path: Path) -> dict[int, str]:
    with open(path, encoding="utf-8") as f:
        instruments = json.load(f)
    return {int(i["security_id"]): i["symbol"] for i in instruments}


def iter_frames(buf: bytes):
    """Yield (timestamp, raw_payload_bytes) for each captured frame."""
    offset = 0
    n = len(buf)
    while offset + 12 <= n:
        ts = struct.unpack_from("<d", buf, offset)[0]
        flen = struct.unpack_from("<I", buf, offset + 8)[0]
        payload_start = offset + 12
        payload_end = payload_start + flen
        if payload_end > n:
            break  # truncated final frame (recorder killed mid-write) -- stop cleanly
        yield ts, buf[payload_start:payload_end]
        offset = payload_end


def iter_packets_20depth(payload: bytes):
    """20-depth body decode: level count = payload_len // 16 (row_count field
    is unreliable for this feed -- see parse_arjun_depth20.py)."""
    offset = 0
    n = len(payload)
    while offset + HEADER_SIZE <= n:
        msg_len, feed_code, segment, security_id, _unused = HEADER_STRUCT.unpack_from(payload, offset)
        payload_len = msg_len - HEADER_SIZE
        if payload_len < 0 or offset + msg_len > n:
            break
        n_levels = payload_len // LEVEL_SIZE
        body_start = offset + HEADER_SIZE
        levels = LEVEL_STRUCT.iter_unpack(payload[body_start:body_start + n_levels * LEVEL_SIZE])
        yield feed_code, security_id, list(levels)
        offset += msg_len


def iter_packets_200depth(payload: bytes):
    """200-depth body decode via the SDK-verified depth_parser (trusts row_count)."""
    try:
        packets = parse_depth_frame(payload)
    except Exception:
        return
    for p in packets:
        yield p.feed_code, p.security_id, [(lv.price, lv.qty, lv.orders) for lv in p.levels]


def _flush_buffer(rows: dict[str, list], out_dir: Path, part_counters: dict[str, int]) -> None:
    if not rows["ts"]:
        return
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    for symbol, group in df.groupby("symbol"):
        part_dir = out_dir / f"symbol={symbol}"
        part_dir.mkdir(parents=True, exist_ok=True)
        n = part_counters.get(symbol, 0)
        table = pa.Table.from_pandas(group.drop(columns=["symbol"]), schema=PART_SCHEMA, preserve_index=False)
        pq.write_table(table, part_dir / f"part-{n:05d}.parquet")
        part_counters[symbol] = n + 1
    for k in rows:
        rows[k].clear()


def convert(bin_path: Path, instruments_path: Path, out_dir: Path, packet_iter) -> None:
    if not bin_path.exists():
        print(f"SKIP (missing): {bin_path}")
        return
    sid_to_symbol = load_instrument_map(instruments_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: dict[str, list] = {k: [] for k in SCHEMA.names}
    part_counters: dict[str, int] = {}
    n_rows = 0
    n_frames = 0
    unknown_sids: set[int] = set()

    with open(bin_path, "rb") as f:
        buf = f.read()

    for ts, payload in iter_frames(buf):
        n_frames += 1
        for feed_code, security_id, levels in packet_iter(payload):
            symbol = sid_to_symbol.get(security_id)
            if symbol is None:
                unknown_sids.add(security_id)
                continue
            side = "BID" if feed_code == FEED_CODE_BID else "ASK"
            for i, (price, qty, orders) in enumerate(levels):
                rows["ts"].append(ts)
                rows["symbol"].append(symbol)
                rows["security_id"].append(security_id)
                rows["side"].append(side)
                rows["level_idx"].append(i)
                rows["price"].append(price)
                rows["qty"].append(qty)
                rows["orders"].append(orders)
                n_rows += 1

        if n_rows >= FLUSH_EVERY_ROWS:
            _flush_buffer(rows, out_dir, part_counters)
            n_rows = 0
            print(f"  ...flushed at frame {n_frames}", flush=True)

    if n_rows:
        _flush_buffer(rows, out_dir, part_counters)

    print(f"{bin_path.name}: {n_frames} frames, {sum(part_counters.values())} parquet parts across {len(part_counters)} symbols")
    if unknown_sids:
        print(f"  WARNING: {len(unknown_sids)} security_ids not in instrument manifest: {sorted(unknown_sids)[:20]}")


if __name__ == "__main__":
    convert(
        RAW_ROOT / "depth20" / DATE / "conn0.bin",
        RAW_ROOT / "depth20" / DATE / "conn0_instruments.json",
        OUT_ROOT / "equities" / f"date={DATE}",
        iter_packets_20depth,
    )
    convert(
        RAW_ROOT / "depth20" / DATE / "conn1.bin",
        RAW_ROOT / "depth20" / DATE / "conn1_instruments.json",
        OUT_ROOT / "equities" / f"date={DATE}",
        iter_packets_20depth,
    )
    convert(
        RAW_ROOT / "depth20_options" / DATE / "conn0.bin",
        RAW_ROOT / "depth20_options" / DATE / "conn0_instruments.json",
        OUT_ROOT / "options" / f"date={DATE}",
        iter_packets_20depth,
    )
    convert(
        RAW_ROOT / "depth200_futures" / DATE / "conn0.bin",
        RAW_ROOT / "depth200_futures" / DATE / "conn0_instruments.json",
        OUT_ROOT / "futures" / f"date={DATE}",
        iter_packets_200depth,
    )
    convert(
        RAW_ROOT / "depth200_futures" / DATE / "conn1.bin",
        RAW_ROOT / "depth200_futures" / DATE / "conn1_instruments.json",
        OUT_ROOT / "futures" / f"date={DATE}",
        iter_packets_200depth,
    )

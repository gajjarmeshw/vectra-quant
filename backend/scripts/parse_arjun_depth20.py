#!/usr/bin/env python
"""One-off converter: Arjun's raw depth20 WebSocket captures (NIFTY100
equities + NIFTY weekly option chain, 2026-09-17, ~14:40-15:30 IST) into
partitioned Parquet, usable for backtesting.

Raw file format (reverse-engineered, not documented anywhere -- the
original recorder script isn't present on this machine):
    repeated frames, each:
        8 bytes  float64  wall-clock receipt timestamp (time.time())
        4 bytes  uint32   length of the raw Dhan payload that follows
        N bytes           one or more concatenated Dhan depth packets,
                           each self-delimited exactly like the 200-depth
                           feed (see vectra_quant.orderflow.depth_parser):
                           <hBBiI> header (msg_len, feed_code, segment,
                           security_id, <unused for this 20-depth feed>)
                           then msg_len-12 bytes of <dII> levels.

Unlike the 200-depth feed, the header's 5th field is NOT a level count for
this feed (it reads 0 in this data) -- 20-depth always packs exactly
payload_len // 16 levels (verified: 320 bytes = 20 levels, matching the
feed's name), so this script computes level count itself instead of
reusing depth_parser.parse_one_packet's row_count-trusting logic.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

HEADER_STRUCT = struct.Struct("<hBBiI")
LEVEL_STRUCT = struct.Struct("<dII")
HEADER_SIZE = HEADER_STRUCT.size  # 12
LEVEL_SIZE = LEVEL_STRUCT.size    # 16
FEED_CODE_BID = 41
FEED_CODE_ASK = 51

SCHEMA = pa.schema([
    ("ts", pa.timestamp("ns", tz="UTC")),  # pd.to_datetime's native precision
    ("symbol", pa.string()),
    ("security_id", pa.int64()),
    ("side", pa.string()),
    ("level_idx", pa.int32()),
    ("price", pa.float64()),
    ("qty", pa.int64()),
    ("orders", pa.int64()),
])
# `symbol` is redundant once split across symbol=<X> partition directories.
PART_SCHEMA = pa.schema([f for f in SCHEMA if f.name != "symbol"])

FLUSH_EVERY_ROWS = 500_000


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


def iter_packets(payload: bytes):
    """Yield (feed_code, segment, security_id, levels) for each packet in
    one frame's concatenated payload. `levels` is a list of (price, qty, orders)."""
    offset = 0
    n = len(payload)
    while offset + HEADER_SIZE <= n:
        msg_len, feed_code, segment, security_id, _unused = HEADER_STRUCT.unpack_from(payload, offset)
        payload_len = msg_len - HEADER_SIZE
        if payload_len < 0 or offset + msg_len > n:
            break  # malformed/truncated -- stop this frame rather than guess
        n_levels = payload_len // LEVEL_SIZE
        body_start = offset + HEADER_SIZE
        levels = LEVEL_STRUCT.iter_unpack(payload[body_start:body_start + n_levels * LEVEL_SIZE])
        yield feed_code, segment, security_id, list(levels)
        offset += msg_len


def _flush_buffer(rows: dict[str, list], out_dir: Path, part_counters: dict[str, int]) -> None:
    if not rows["ts"]:
        return
    df = pd.DataFrame(rows)
    # rows["ts"] holds raw time.time() (UTC epoch) floats -- convert to real,
    # explicitly UTC-aware datetimes here rather than leaving pyarrow to
    # infer a plain float64 column from schema=None (what the first pass
    # did), and rather than leaving it tz-naive (ambiguous: naive UTC reads
    # as 5:30 "behind" the IST wall-clock time in the source recorder's log).
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


def convert(bin_path: Path, instruments_path: Path, out_dir: Path, date_str: str) -> None:
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
        for feed_code, segment, security_id, levels in iter_packets(payload):
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
    ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../orderflow_20260917/data/raw/dhan")
    DATE = "2026-09-17"
    OUT_ROOT = Path("data/orderflow/external/arjun_20260917")

    convert(
        ROOT / "depth20" / DATE / "conn0.bin",
        ROOT / "depth20" / DATE / "conn0_instruments.json",
        OUT_ROOT / "equities" / f"date={DATE}",
        DATE,
    )
    convert(
        ROOT / "depth20" / DATE / "conn1.bin",
        ROOT / "depth20" / DATE / "conn1_instruments.json",
        OUT_ROOT / "equities" / f"date={DATE}",
        DATE,
    )
    convert(
        ROOT / "depth20_options" / DATE / "conn0.bin",
        ROOT / "depth20_options" / DATE / "conn0_instruments.json",
        OUT_ROOT / "options" / f"date={DATE}",
        DATE,
    )

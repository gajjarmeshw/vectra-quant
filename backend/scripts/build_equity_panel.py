#!/usr/bin/env python
"""Build a proper daily OHLCV panel for the full NIFTY100 universe from the
archive's 1-minute intraday store.

WHY THIS EXISTS. `research_equity_xsection.py` ran on the archive's
`store/equity` folder, which turned out to be a PARTIAL backfill: ~22 symbols,
all alphabetically early (ABB..CGPOWER). Cross-sectional tests on 22 names with
5-stock baskets are dominated by idiosyncratic noise -- that is a data defect,
not evidence about the signals. The intraday store (`raw/dhan/intraday/i1`)
carries ~106 NIFTY100 symbols from 2021-10 onward, which is the cross-section
those tests actually needed.

This aggregates 1-minute bars to daily (open=first, high=max, low=min,
close=last, volume=sum) per symbol per session, and caches the result.

REMAINING BIAS, STATED PLAINLY: the symbol list is NIFTY100 membership as of
the archive build, so names that were delisted or dropped from the index are
absent. Any long-only number computed on this panel is therefore biased UP and
must not be read as an achievable buy-and-hold return. Long/short results are
far less affected, which is one more reason to treat the market-neutral line as
the real test.
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent / "IEA_data_20260915" / "data" / "raw" / "dhan" / "intraday" / "i1"
OUT = Path(__file__).resolve().parent.parent / "data" / "orderflow" / "_nifty100_daily.parquet"

# Index/derivative series that live in the same folder but are not equities.
SKIP_PREFIX = ("NIFTY", "BANKNIFTY", "FINNIFTY")


def daily_from_file(path: Path) -> pd.DataFrame | None:
    try:
        with gzip.open(path, "rt") as f:
            d = json.load(f)
    except Exception:
        return None
    if not d.get("timestamp"):
        return None
    ts = pd.to_datetime(d["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata")
    df = pd.DataFrame({
        "date": ts.strftime("%Y-%m-%d"),
        "open": d["open"], "high": d["high"], "low": d["low"],
        "close": d["close"], "volume": d.get("volume", [0] * len(ts)),
    })
    g = df.groupby("date", sort=True).agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
    ).reset_index()
    return g


def main() -> None:
    symbols = sorted(
        s for s in os.listdir(ROOT)
        if (ROOT / s).is_dir() and not s.startswith(SKIP_PREFIX)
    )
    print(f"Building daily panel for {len(symbols)} equity symbols from {ROOT}")

    frames = []
    for i, sym in enumerate(symbols, 1):
        parts = []
        for fn in sorted(os.listdir(ROOT / sym)):
            if not fn.endswith(".json.gz") or fn.startswith("._"):
                continue
            g = daily_from_file(ROOT / sym / fn)
            if g is not None and len(g):
                parts.append(g)
        if not parts:
            continue
        s = pd.concat(parts, ignore_index=True).drop_duplicates("date", keep="last")
        s["symbol"] = sym
        frames.append(s)
        if i % 20 == 0:
            print(f"  ...{i}/{len(symbols)} symbols", flush=True)

    panel = pd.concat(frames, ignore_index=True)
    panel = panel[(panel["close"] > 0) & panel["close"].notna()]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(OUT, index=False)

    print(f"\nPanel written to {OUT}")
    print(f"  rows      : {len(panel):,}")
    print(f"  symbols   : {panel['symbol'].nunique()}")
    print(f"  date range: {panel['date'].min()} .. {panel['date'].max()}")
    per_day = panel.groupby("date")["symbol"].nunique()
    print(f"  sessions  : {len(per_day)}   median symbols/day: {per_day.median():.0f}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
"""Cross-sectional order-flow breadth backtest, adapted from
`backtest_breadth_arjun_data.py`, against today's (2026-09-18) real pulled
depth capture -- with one methodological improvement: scored against the
REAL front-month NIFTY future's mid price, not an implied-spot proxy
derived via put-call parity (Arjun's capture had no direct index/futures
tick, so that proxy was the best available then; today's capture includes
a real futures order book, so we use it directly).

CAVEATS (same spirit as backtest_20260918_thunderbolt.py):
  - ONE partial trading day (~08:55-11:15 IST) -- this can only ever answer
    "does this look worth pursuing with more data", never "does this work".
  - `theta_z` (the z-score crossing threshold) is still an unvalidated guess
    swept across candidates, same as the original Arjun-data script.
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import time as dtime

from vectra_quant.core.session_clock import IST
from vectra_quant.orderflow.breadth_signal import compute_breadth, detect_breadth_crossing, per_stock_imbalance
from vectra_quant.orderflow.thunderbolt_signal import SignalDirection

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "orderflow" / "pulled_20260918"
DATE = "2026-09-18"
BUCKET = "15s"
FORWARD_WINDOW = pd.Timedelta(minutes=1)
THETA_CANDIDATES = [0.05, 0.10, 0.15, 0.20]
MARKET_OPEN = dtime(9, 15)  # NSE regular session open -- pre-open snapshots (08:55-09:08 here)
# can carry a degenerate price=0 "no quote posted yet" tick that a naive
# ffill can't backfill (it's the leading value), producing a fake
# ~23000pt "move" for any signal scored against it. Drop non-positive
# prices AND clip to real session open as a second, independent guard.


def load_symbol(base: Path, symbol: str) -> pd.DataFrame:
    files = glob.glob(str(base / f"symbol={symbol}" / "*.parquet"))
    if not files:
        return pd.DataFrame()
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["ts"] = df["ts"].dt.tz_convert(IST)
    return df


def top_of_book_series(df: pd.DataFrame) -> pd.DataFrame:
    top = df[df["level_idx"] == 0]
    bucketed = (
        top.set_index("ts")
        .groupby([pd.Grouper(freq=BUCKET), "side"])["qty"]
        .last()
        .unstack("side")
        .rename(columns={"BID": "bid_qty", "ASK": "ask_qty"})
    )
    return bucketed.ffill()


def build_breadth_series(equities_dir: Path, symbols: list[str]) -> pd.Series:
    per_stock: dict[str, pd.Series] = {}
    for sym in symbols:
        df = load_symbol(equities_dir, sym)
        if df.empty:
            continue
        book = top_of_book_series(df)
        per_stock[sym] = book.apply(
            lambda r: per_stock_imbalance(r.get("bid_qty", 0) or 0, r.get("ask_qty", 0) or 0), axis=1
        )
    panel = pd.DataFrame(per_stock).dropna(how="all")
    breadth = panel.apply(lambda row: compute_breadth(row.dropna().to_dict()).mean_imbalance, axis=1)
    return breadth[breadth.index.time >= MARKET_OPEN]


def build_real_futures_spot_series(futures_dir: Path) -> pd.Series:
    """Real front-month NIFTY future mid price -- the direct improvement
    over the Arjun-data script's put-call-parity implied spot."""
    df = load_symbol(futures_dir, "NIFTY-Sep2026-FUT")
    top = df[(df["level_idx"] == 0) & (df["price"] > 0)]  # drop degenerate pre-quote price=0 rows
    bucketed = (
        top.set_index("ts")
        .groupby([pd.Grouper(freq=BUCKET), "side"])["price"]
        .last()
        .unstack("side")
    )
    mid = ((bucketed.get("BID") + bucketed.get("ASK")) / 2).ffill().dropna()
    return mid[mid.index.time >= MARKET_OPEN]


def score_signal(breadth: pd.Series, spot: pd.Series, theta: float) -> dict:
    valid_start = spot.index.min()
    valid_end = spot.index.max() - FORWARD_WINDOW
    if valid_end <= valid_start:
        return {"theta": theta, "n_trades": 0, "note": "spot window shorter than the forward window"}

    breadth_in_range = breadth[(breadth.index >= valid_start) & (breadth.index <= valid_end)]
    series = list(zip(breadth_in_range.index.to_pydatetime(), breadth_in_range.to_numpy()))

    trades = []
    remaining = series[:]
    while remaining:
        trigger = detect_breadth_crossing(remaining, theta_cross=theta)
        if trigger is None:
            break
        entry_ts = trigger.ts
        exit_ts = entry_ts + FORWARD_WINDOW
        entry_spot = spot.loc[:entry_ts].iloc[-1]
        exit_candidates = spot.loc[entry_ts:exit_ts]
        exit_spot = exit_candidates.iloc[-1] if len(exit_candidates) else spot.loc[:exit_ts].iloc[-1]
        move = exit_spot - entry_spot
        signed_move = move if trigger.direction == SignalDirection.BULLISH else -move
        trades.append({"ts": entry_ts, "direction": trigger.direction.value, "move_pts": round(signed_move, 2)})
        remaining = [p for p in remaining if p[0] > entry_ts]

    if not trades:
        return {"theta": theta, "n_trades": 0}
    wins = sum(1 for t in trades if t["move_pts"] > 0)
    avg_move = sum(t["move_pts"] for t in trades) / len(trades)
    return {
        "theta": theta, "n_trades": len(trades), "win_rate": round(wins / len(trades), 2),
        "avg_move_pts": round(avg_move, 2), "trades": trades,
    }


def main() -> None:
    equities_dir = DATA_ROOT / "equities" / f"date={DATE}"
    futures_dir = DATA_ROOT / "futures" / f"date={DATE}"

    symbols = sorted(p.split("=")[1] for p in os.listdir(equities_dir))
    print(f"Loading {len(symbols)} equity symbols...")
    breadth = build_breadth_series(equities_dir, symbols)
    print(f"Breadth series: {len(breadth)} buckets, {breadth.index.min()} to {breadth.index.max()}")
    print(f"  mean={breadth.mean():.4f} std={breadth.std():.4f} min={breadth.min():.4f} max={breadth.max():.4f}")

    print("Loading real front-month futures price series...")
    spot = build_real_futures_spot_series(futures_dir)
    print(f"Futures spot series: {len(spot)} buckets, range {spot.min():.1f}-{spot.max():.1f}\n")

    print("--- Signal scoring across theta candidates (real futures price, not implied-parity proxy) ---")
    for theta in THETA_CANDIDATES:
        result = score_signal(breadth, spot, theta)
        trades_summary = result.pop("trades", [])
        print(result)
        for t in trades_summary:
            print("   ", t)


if __name__ == "__main__":
    main()

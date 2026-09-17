#!/usr/bin/env python
"""One-off backtest: does cross-sectional order-flow breadth (100 NIFTY
stocks) show any real directional structure against the NIFTY index,
using Arjun's 2026-09-17 depth20 capture (~14:40-15:29 IST, ~49 minutes)?

Methodology:
  1. Bucket each stock's top-of-book (level 0) bid/ask quantity into fixed
     intervals; compute per-stock imbalance per bucket.
  2. Aggregate all stocks' imbalance per bucket into one breadth reading
     (mean imbalance across stocks).
  3. Detect threshold crossings on the breadth series (reusing Thunderbolt's
     generic crossing detector).
  4. Score each crossing by the forward move of an IMPLIED NIFTY SPOT,
     derived from the option chain via put-call parity (spot = strike +
     CE_mid - PE_mid, median across all 21 captured strikes per bucket) --
     this capture has no direct index tick, so this is the closest proxy
     available, and skew from IV/moneyness is real, uncorrected noise.

This is ONE session, ~49 minutes. Nothing here is a validated edge --
it can only ever answer "is this worth pursuing with more data", never
"does this work".
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vectra_quant.orderflow.breadth_signal import compute_breadth, detect_breadth_crossing, per_stock_imbalance
from vectra_quant.orderflow.thunderbolt_signal import SignalDirection

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "orderflow" / "external" / "arjun_20260917"
DATE = "2026-09-17"
BUCKET = "15s"
FORWARD_WINDOW = pd.Timedelta(minutes=1)
THETA_CANDIDATES = [0.05, 0.10, 0.15, 0.20]


def load_symbol(base: Path, symbol: str) -> pd.DataFrame:
    files = glob.glob(str(base / f"symbol={symbol}" / "*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)


def top_of_book_series(df: pd.DataFrame) -> pd.DataFrame:
    """Bucket one stock's ticks and return a per-bucket (bid_qty, ask_qty) frame."""
    top = df[df["level_idx"] == 0]
    bucketed = (
        top.set_index("ts")
        .groupby([pd.Grouper(freq=BUCKET), "side"])["qty"]
        .last()  # most recent top-of-book snapshot in the bucket
        .unstack("side")
        .rename(columns={"BID": "bid_qty", "ASK": "ask_qty"})
    )
    return bucketed.ffill()  # a bucket with no fresh tick keeps the last known book


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
    return breadth


def build_implied_spot_series(options_dir: Path, strikes: list[tuple[int, str]]) -> pd.Series:
    """strikes: list of (strike_price, base_symbol_without_ce_pe)."""
    per_strike_spot: dict[int, pd.Series] = {}
    for strike, base in strikes:
        ce = load_symbol(options_dir, f"{base}-CE")
        pe = load_symbol(options_dir, f"{base}-PE")
        if ce.empty or pe.empty:
            continue
        ce_mid = _mid_price_series(ce)
        pe_mid = _mid_price_series(pe)
        joined = pd.concat([ce_mid.rename("ce"), pe_mid.rename("pe")], axis=1).ffill().dropna()
        per_strike_spot[strike] = strike + joined["ce"] - joined["pe"]
    panel = pd.DataFrame(per_strike_spot)
    return panel.median(axis=1)


def _mid_price_series(df: pd.DataFrame) -> pd.Series:
    top = df[df["level_idx"] == 0]
    bucketed = (
        top.set_index("ts")
        .groupby([pd.Grouper(freq=BUCKET), "side"])["price"]
        .last()
        .unstack("side")
    )
    return ((bucketed.get("BID") + bucketed.get("ASK")) / 2).ffill()


def score_signal(breadth: pd.Series, implied_spot: pd.Series, theta: float) -> dict:
    # The options capture (implied spot) covers a much shorter, EARLIER
    # window than the equities capture (breadth). Scoring a crossing
    # against implied_spot outside its own real coverage means comparing
    # against a stale, forward-filled value that never actually moved --
    # exactly what produced a wave of fake "0.0pt move" trades before this
    # fix. Only crossings with a genuine entry AND exit price inside
    # implied_spot's own real range are scored at all.
    valid_start = implied_spot.index.min()
    valid_end = implied_spot.index.max() - FORWARD_WINDOW
    if valid_end <= valid_start:
        return {"theta": theta, "n_trades": 0, "note": "implied-spot window shorter than the forward window"}

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
        entry_spot = implied_spot.loc[:entry_ts].iloc[-1]
        exit_candidates = implied_spot.loc[entry_ts:exit_ts]
        exit_spot = exit_candidates.iloc[-1] if len(exit_candidates) else implied_spot.loc[:exit_ts].iloc[-1]
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
    options_dir = DATA_ROOT / "options" / f"date={DATE}"

    symbols = sorted(p.split("=")[1] for p in os.listdir(equities_dir))
    print(f"Loading {len(symbols)} equity symbols...")
    breadth = build_breadth_series(equities_dir, symbols)
    print(f"Breadth series: {len(breadth)} buckets, {breadth.index.min()} to {breadth.index.max()}")
    print(f"  mean={breadth.mean():.4f} std={breadth.std():.4f} min={breadth.min():.4f} max={breadth.max():.4f}")

    opt_symbols = sorted(p.split("=")[1] for p in os.listdir(options_dir))
    strikes = sorted({int(s.split("-")[4]) for s in opt_symbols})
    strike_bases = [(k, f"NIFTY-2026-09-22-{k}") for k in strikes]
    print(f"Loading {len(strikes)} strikes for implied spot...")
    implied_spot = build_implied_spot_series(options_dir, strike_bases)
    print(f"Implied spot series: {len(implied_spot)} buckets, range {implied_spot.min():.1f}-{implied_spot.max():.1f}")

    print("\n--- Signal scoring across theta candidates ---")
    for theta in THETA_CANDIDATES:
        result = score_signal(breadth, implied_spot, theta)
        trades_summary = result.pop("trades", [])
        print(result)
        for t in trades_summary:
            print("   ", t)


if __name__ == "__main__":
    main()

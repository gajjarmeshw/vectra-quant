#!/usr/bin/env python
"""Offline backtest: hedged121 ("Nifty Thunderbolt") replayed against today's
(2026-09-18) real pulled depth capture.

IMPORTANT CAVEATS -- read before trusting any number this prints:

1. ONE DAY of data, and a PARTIAL day at that: the S3 pull happened mid-
   session, so coverage is ~08:55-11:15 IST only (pre-open through the
   first ~2 hours). The disclosed signal window is 09:16-12:00 IST and
   force-exit is 15:10 IST -- we can detect an entry trigger inside the
   covered window, but CANNOT run a real 15:10 exit. Any open position is
   marked at the LAST available tick, not a genuine exit.

2. Every theta threshold in ThunderboltCfg is a labeled RECONSTRUCTION
   GUESS (see thunderbolt_signal.py's own docstring) -- the real
   production values are proprietary and undisclosed. This script runs
   BOTH the as-shipped guessed config AND a data-grounded variant (thetas
   derived from today's own empirical |imbalance| distribution) side by
   side, so the difference between "arbitrary guess" and "at least
   consistent with today's real data" is visible rather than hidden.

3. The "medium regime inversion" filter's logic itself is undisclosed in
   shape, not just threshold -- thunderbolt_signal.py's implementation is
   explicitly labeled a best-effort guess at its shape. Any result where
   that filter fires should not be read as validated.

4. Entry/exit leg prices use the MID of best bid/ask (average of level-0
   BID and ASK price) as an LTP proxy, matching how paper_trader.py's
   opaque `quote_fn` is used elsewhere in this repo -- not a simulated
   fill against the real spread.

Nothing here places or would place a real order.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectra_quant.core.session_clock import IST
from vectra_quant.orderflow.config import DEFAULT_THUNDERBOLT_CFG
from vectra_quant.orderflow.thunderbolt_signal import ThunderboltInputs, evaluate_thunderbolt
from vectra_quant.orderflow.thunderbolt_strategy import build_thunderbolt_structure, max_loss, net_debit

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "orderflow" / "pulled_20260918"
DATE = "2026-09-18"
LOT_SIZE = 65  # NIFTY, per contract.py's verified instrument-master mapping
BUCKET = "5s"


def _to_ist(df: pd.DataFrame) -> pd.DataFrame:
    """Parquet timestamps are UTC-tz-aware; thunderbolt_signal.py's window
    checks compare .time() against IST cutoffs (it's normally fed via
    now_ist() in production) -- must localize before any time-of-day logic."""
    df = df.copy()
    df["ts"] = df["ts"].dt.tz_convert(IST)
    return df


def load_front_future() -> pd.DataFrame:
    return _to_ist(pd.read_parquet(DATA_ROOT / "futures" / f"date={DATE}" / "symbol=NIFTY-Sep2026-FUT"))


def top_of_book_imbalance_series(df: pd.DataFrame) -> pd.Series:
    """ir(i) bucketed at BUCKET, last top-of-book (level 0) reading per bucket."""
    top = df[df["level_idx"] == 0]
    bucketed = (
        top.set_index("ts")
        .groupby([pd.Grouper(freq=BUCKET), "side"])["qty"]
        .last()
        .unstack("side")
        .rename(columns={"BID": "bid_qty", "ASK": "ask_qty"})
        .dropna()
    )
    denom = bucketed["bid_qty"] + bucketed["ask_qty"]
    ir = (bucketed["bid_qty"] - bucketed["ask_qty"]) / denom.replace(0, np.nan)
    return ir.dropna()


def mid_price_series(df: pd.DataFrame) -> pd.Series:
    """Bucketed mid price from level-0 bid/ask price, for spot / option LTP proxy."""
    top = df[df["level_idx"] == 0]
    bucketed = (
        top.set_index("ts")
        .groupby([pd.Grouper(freq=BUCKET), "side"])["price"]
        .last()
        .unstack("side")
        .rename(columns={"BID": "bid_px", "ASK": "ask_px"})
        .dropna()
    )
    return ((bucketed["bid_px"] + bucketed["ask_px"]) / 2).dropna()


def nearest_value(series: pd.Series, at) -> float:
    idx = series.index.get_indexer([at], method="nearest")[0]
    return float(series.iloc[idx])


def load_option_symbol_map() -> dict[str, int]:
    import json
    path = DATA_ROOT.parent.parent / "raw_pull" / "dhan" / "depth20_options" / DATE / "conn0_instruments.json"
    with open(path, encoding="utf-8") as f:
        instruments = json.load(f)
    return {i["symbol"]: int(i["security_id"]) for i in instruments}


def option_mid_series(options_df: pd.DataFrame, security_id: int) -> pd.Series:
    sub = options_df[(options_df["security_id"] == security_id) & (options_df["level_idx"] == 0)]
    bucketed = (
        sub.set_index("ts")
        .groupby([pd.Grouper(freq=BUCKET), "side"])["price"]
        .last()
        .unstack("side")
        .rename(columns={"BID": "bid_px", "ASK": "ask_px"})
        .dropna()
    )
    return ((bucketed["bid_px"] + bucketed["ask_px"]) / 2).dropna()


def resolve_leg_symbol(symbol_map: dict[str, int], expiry: str, strike: float, right: str) -> tuple[str, int] | None:
    key = f"NIFTY-{expiry}-{int(strike)}-{right}"
    sid = symbol_map.get(key)
    if sid is None:
        return None
    return key, sid


def main() -> None:
    print(f"=== Thunderbolt backtest against real {DATE} capture (partial-day data) ===\n")

    fut = load_front_future()
    ir = top_of_book_imbalance_series(fut)
    spot_mid = mid_price_series(fut)

    print(f"Futures top-of-book imbalance series: {len(ir)} buckets, "
          f"{ir.index.min()} .. {ir.index.max()} (IST)")
    abs_ir = ir.abs()
    print("Empirical |ir(i)| distribution today (front-month futures top-of-book):")
    for pct in (50, 70, 75, 90, 95, 97, 99):
        print(f"  p{pct}: {np.percentile(abs_ir, pct):.4f}")
    print()

    # --- data-grounded threshold proposal -----------------------------------
    theta_cross_grounded = float(np.percentile(abs_ir, 75))
    over_stretch_grounded = float(np.percentile(abs_ir, 97))
    print(f"Proposed data-grounded theta_cross (p75)       = {theta_cross_grounded:.4f}  "
          f"(config guess: {DEFAULT_THUNDERBOLT_CFG.theta_cross})")
    print(f"Proposed data-grounded over_stretch_bound (p97) = {over_stretch_grounded:.4f}  "
          f"(config guess: {DEFAULT_THUNDERBOLT_CFG.over_stretch_bound})\n")

    grounded_cfg = replace(
        DEFAULT_THUNDERBOLT_CFG,
        theta_cross=theta_cross_grounded,
        over_stretch_bound=over_stretch_grounded,
    )

    series = list(zip(ir.index.to_pydatetime(), ir.to_numpy().tolist()))
    pre_open = [(t, v) for t, v in series if t.time() < DEFAULT_THUNDERBOLT_CFG.signal_window_start]
    pre_open_bid_extreme = max((v for _, v in pre_open if v > 0), default=0.0)
    pre_open_ask_extreme = max((abs(v) for _, v in pre_open if v < 0), default=0.0)

    inputs = ThunderboltInputs(
        series=series,
        recent_realized_vols=[],  # none available -> conservative HIGH fallback, per spec
        pre_open_bid_extreme=pre_open_bid_extreme,
        pre_open_ask_extreme=pre_open_ask_extreme,
    )

    for label, cfg in (("AS-SHIPPED GUESS config", DEFAULT_THUNDERBOLT_CFG), ("DATA-GROUNDED config", grounded_cfg)):
        print(f"--- {label} ---")
        trace = evaluate_thunderbolt(inputs, cfg)
        print(f"  regime               = {trace.regime.value}")
        if trace.trigger:
            print(f"  trigger              = {trace.trigger.source} {trace.trigger.direction.value} "
                  f"value={trace.trigger.value:.4f} at {trace.trigger.ts}")
        else:
            print("  trigger              = None (no qualifying crossing/breakout in covered window)")
        print(f"  reversal_flip        = {trace.reversal_flip}")
        print(f"  opposite_gate_skip   = {trace.opposite_gate_skip}")
        print(f"  pre_open_lock_skip   = {trace.pre_open_lock_skip}")
        print(f"  liquidity_reversal   = {trace.liquidity_reversal_flip}")
        print(f"  medium_regime_flip   = {trace.medium_regime_flip}  (filter SHAPE itself is a guess -- see module docstring)")
        print(f"  over_stretch_skip    = {trace.over_stretch_skip}")
        print(f"  FINAL                = {trace.final.value}\n")

        if trace.final.value == "SKIP":
            continue

        # --- price and evaluate the structure against real option data -----
        spot = nearest_value(spot_mid, trace.trigger.ts)
        legs = build_thunderbolt_structure(trace.final, spot, cfg)
        symbol_map = load_option_symbol_map()
        options_df = _to_ist(pd.read_parquet(DATA_ROOT / "options" / f"date={DATE}"))

        entry_prices = {}
        exit_prices = {}
        ok = True
        for leg in legs:
            resolved = resolve_leg_symbol(symbol_map, "2026-09-22", leg.strike, leg.right)
            if resolved is None:
                print(f"  ABORT: strike {leg.strike}{leg.right} not listed in captured chain -- "
                      f"per disclosure section 8, trade would be abandoned for the day")
                ok = False
                break
            sym, sid = resolved
            opt_series = option_mid_series(options_df, sid)
            if opt_series.empty:
                print(f"  ABORT: no captured ticks for {sym}")
                ok = False
                break
            entry_prices[leg.right + str(leg.strike)] = nearest_value(opt_series, trace.trigger.ts)
            exit_prices[leg.right + str(leg.strike)] = float(opt_series.iloc[-1])  # last available tick, NOT a real 15:10 exit

        if not ok:
            continue

        long_leg = next(lg for lg in legs if lg.action == "BUY")
        short_leg = next(lg for lg in legs if lg.action == "SELL")
        long_key, short_key = long_leg.right + str(long_leg.strike), short_leg.right + str(short_leg.strike)

        d = net_debit(entry_prices[long_key], entry_prices[short_key], cfg, LOT_SIZE)
        ml = max_loss(d, cfg, LOT_SIZE)
        print(f"  spot @ trigger       = {spot:.1f}, ATM strike used = {round(spot / cfg.strike_step) * cfg.strike_step:.0f}")
        print(f"  entry: BUY {cfg.long_lots}x {long_key} @ {entry_prices[long_key]:.2f}, "
              f"SELL {cfg.short_lots}x {short_key} @ {entry_prices[short_key]:.2f}")
        print(f"  net_debit (per unit) = {d:.2f}, recorded max_loss = {ml:.2f}")

        pnl = (
            cfg.long_lots * (exit_prices[long_key] - entry_prices[long_key])
            - cfg.short_lots * (exit_prices[short_key] - entry_prices[short_key])
        ) * LOT_SIZE
        print(f"  MARK at last captured tick (NOT a real 15:10 exit): "
              f"{long_key}={exit_prices[long_key]:.2f}, {short_key}={exit_prices[short_key]:.2f}")
        print(f"  unrealized P&L as of last tick = {pnl:.2f}\n")

    # --- sensitivity sweep: how close was today's SKIP to flipping? --------
    # Today's only trigger was blocked by the opposite-side gate. Rather
    # than treat SKIP as a single point answer, grid theta_cross and
    # opposite_gate_ratio to see whether that SKIP is robust (holds across
    # a wide parameter range) or a coin-flip near a boundary (a small
    # nudge in either guessed constant would have fired a trade).
    print("=== Sensitivity sweep: theta_cross x opposite_gate_ratio -> FINAL decision ===")
    print("(both are undisclosed/guessed constants -- this maps the decision boundary, not a calibration)\n")
    theta_grid = [0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.6667, 0.80]
    ratio_grid = [1.0, 1.25, 1.5, 1.75, 2.0]

    header = "theta_cross \\ opposite_gate_ratio".ljust(22) + "".join(f"{r:>8.2f}" for r in ratio_grid)
    print(header)
    for theta in theta_grid:
        row = f"{theta:<22.4f}"
        for ratio in ratio_grid:
            sweep_cfg = replace(DEFAULT_THUNDERBOLT_CFG, theta_cross=theta, opposite_gate_ratio=ratio)
            trace = evaluate_thunderbolt(inputs, sweep_cfg)
            row += f"{trace.final.value:>8}"
        print(row)
    print("\n(BULLISH/BEARISH = a trade would have fired; SKIP = no trade)")


if __name__ == "__main__":
    main()

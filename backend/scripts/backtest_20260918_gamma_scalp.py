#!/usr/bin/env python
"""Exploratory gamma-scalping simulation against today's (2026-09-18) real
pulled depth capture -- a long ATM straddle, continuously delta-hedged with
the front-month NIFTY future.

WHAT THIS NEEDS vs. WHAT THUNDERBOLT/BREADTH NEED: gamma scalping is a
price/Greeks strategy, not an order-flow strategy -- it needs a real
underlying price series (front futures mid) and real option premiums
(ATM CE/PE mid), NOT order-book depth/imbalance. Both are already in
today's pull, so no new data was required for this prototype.

METHODOLOGY AND ITS LIMITS (read before trusting any P&L number below):

1. NO REAL GREEKS FEED. There is no Black-Scholes / IV solve here -- delta
   for each leg is estimated empirically as a ROLLING REGRESSION SLOPE of
   that option's own price change against the futures price change over a
   trailing window. This is a legitimate definition of realized delta
   (dPrice/dUnderlying), but it is noisy over short windows and is NOT
   the same as a model-implied delta a real gamma-scalp desk would hedge
   against. Treat it as a first-pass proxy, not a production hedge signal.

2. ONE PARTIAL SESSION (~09:15-11:15 IST). Gamma scalping's entire thesis
   is "collected hedge P&L from realized volatility exceeds what you paid
   for the straddle" -- that thesis can only be judged over MANY sessions
   and a FULL trading day (the biggest realized moves are often in the
   last hour, which this data does not cover). Whatever P&L number this
   prints is not evidence the strategy works or doesn't.

3. No transaction costs, no bid/ask spread paid on rehedges (uses mid
   price for every fill), no margin/lot-size financing cost. Real costs
   would materially erode any hedge P&L shown here.

Nothing here places or would place a real order.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vectra_quant.core.session_clock import IST

DATA_ROOT = Path(__file__).resolve().parent.parent / "data" / "orderflow" / "pulled_20260918"
DATE = "2026-09-18"
LOT_SIZE = 65
BUCKET = "15s"
DELTA_WINDOW = 20          # trailing buckets (~5 min at 15s) for the rolling delta regression
REHEDGE_THRESHOLD_LOTS = 0.15   # rehedge once |net delta| exceeds this fraction of one lot


def _to_ist(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ts"] = df["ts"].dt.tz_convert(IST)
    return df


def mid_series(df: pd.DataFrame) -> pd.Series:
    top = df[(df["level_idx"] == 0) & (df["price"] > 0)]
    bucketed = (
        top.set_index("ts")
        .groupby([pd.Grouper(freq=BUCKET), "side"])["price"]
        .last()
        .unstack("side")
        .rename(columns={"BID": "bid_px", "ASK": "ask_px"})
    )
    return ((bucketed.get("bid_px") + bucketed.get("ask_px")) / 2).ffill().dropna()


def rolling_delta(option_px: pd.Series, fut_px: pd.Series, window: int) -> pd.Series:
    """Empirical delta = rolling-window regression slope of option price
    change vs. futures price change (see module docstring, limitation 1)."""
    d_opt = option_px.diff()
    d_fut = fut_px.diff()
    cov = d_opt.rolling(window).cov(d_fut)
    var = d_fut.rolling(window).var()
    return (cov / var).replace([np.inf, -np.inf], np.nan)


def main() -> None:
    print(f"=== Gamma-scalp simulation against real {DATE} capture (partial-day data) ===\n")

    fut = _to_ist(pd.read_parquet(DATA_ROOT / "futures" / f"date={DATE}" / "symbol=NIFTY-Sep2026-FUT"))
    fut_px = mid_series(fut)
    fut_px = fut_px[fut_px.index.time >= pd.Timestamp("09:15").time()]

    atm = round(fut_px.iloc[0] / 50) * 50
    print(f"Futures @ session start = {fut_px.iloc[0]:.1f} -> ATM strike = {atm:.0f}")

    import json
    with open(DATA_ROOT.parent.parent / "raw_pull" / "dhan" / "depth20_options" / DATE / "conn0_instruments.json") as f:
        instruments = json.load(f)
    sid_map = {i["symbol"]: int(i["security_id"]) for i in instruments}
    ce_sym, pe_sym = f"NIFTY-2026-09-22-{int(atm)}-CE", f"NIFTY-2026-09-22-{int(atm)}-PE"
    if ce_sym not in sid_map or pe_sym not in sid_map:
        print(f"ABORT: {ce_sym} / {pe_sym} not in captured chain")
        return

    options_df = _to_ist(pd.read_parquet(DATA_ROOT / "options" / f"date={DATE}"))
    ce_px = mid_series(options_df[options_df["security_id"] == sid_map[ce_sym]])
    pe_px = mid_series(options_df[options_df["security_id"] == sid_map[pe_sym]])

    panel = pd.concat([fut_px.rename("fut"), ce_px.rename("ce"), pe_px.rename("pe")], axis=1).ffill().dropna()
    panel = panel[panel.index.time >= pd.Timestamp("09:15").time()]
    print(f"Aligned series: {len(panel)} buckets, {panel.index.min()} .. {panel.index.max()}\n")

    delta_ce = rolling_delta(panel["ce"], panel["fut"], DELTA_WINDOW)
    delta_pe = rolling_delta(panel["pe"], panel["fut"], DELTA_WINDOW)
    net_delta_lots = (delta_ce + delta_pe).fillna(0.0)  # 1 lot long CE + 1 lot long PE

    print("Empirical net straddle delta (lots-equivalent) over the session:")
    print(f"  mean={net_delta_lots.mean():.3f} std={net_delta_lots.std():.3f} "
          f"min={net_delta_lots.min():.3f} max={net_delta_lots.max():.3f}\n")

    entry_ce, entry_pe = panel["ce"].iloc[0], panel["pe"].iloc[0]
    straddle_entry_cost = (entry_ce + entry_pe) * LOT_SIZE
    print(f"Long 1 lot ATM straddle @ entry: CE={entry_ce:.2f} + PE={entry_pe:.2f}, "
          f"cost = {straddle_entry_cost:.2f}\n")

    fut_position_lots = 0.0
    fut_entry_price = None
    hedge_pnl = 0.0
    n_rehedges = 0

    for ts, delta in net_delta_lots.items():
        fut_price = panel.loc[ts, "fut"]
        target_hedge_lots = -delta  # short futures to offset positive straddle delta, and vice versa
        if abs(target_hedge_lots - fut_position_lots) >= REHEDGE_THRESHOLD_LOTS:
            if fut_entry_price is not None:
                hedge_pnl += (fut_price - fut_entry_price) * fut_position_lots * LOT_SIZE
            fut_position_lots = target_hedge_lots
            fut_entry_price = fut_price
            n_rehedges += 1

    # close out the final open hedge at the last tick
    if fut_entry_price is not None:
        hedge_pnl += (panel["fut"].iloc[-1] - fut_entry_price) * fut_position_lots * LOT_SIZE

    exit_ce, exit_pe = panel["ce"].iloc[-1], panel["pe"].iloc[-1]
    straddle_mark = (exit_ce + exit_pe) * LOT_SIZE
    option_pnl = straddle_mark - straddle_entry_cost

    print(f"Rehedges executed: {n_rehedges} (threshold={REHEDGE_THRESHOLD_LOTS} lots, "
          f"window={DELTA_WINDOW} buckets)\n")
    print(f"Straddle mark at last captured tick (NOT a real exit): CE={exit_ce:.2f}, PE={exit_pe:.2f}")
    print(f"  option leg P&L (mark-to-market, long straddle)     = {option_pnl:.2f}")
    print(f"  hedge leg P&L (cumulative delta-hedge gains/losses) = {hedge_pnl:.2f}")
    print(f"  TOTAL simulated P&L (no costs, partial session)     = {option_pnl + hedge_pnl:.2f}\n")

    print("Reminder: no transaction costs, spread, or margin modeled; delta is a rolling-regression")
    print("proxy, not real Greeks; ~2 hours of one session only -- see module docstring caveats.")


if __name__ == "__main__":
    main()

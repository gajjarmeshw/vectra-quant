#!/usr/bin/env python
"""RESEARCH: is the NIFTY variance risk premium CONDITIONALLY positive?

BACKGROUND. `research_vol_premium_holds.py` established (after correcting a
severe survivorship bias) that selling vol UNCONDITIONALLY earns roughly zero
net of costs: +3.7 points per trade across 302 in-sample hold-to-expiry
strangles, against tails of -895 points. That kills "sell vol every week".

THE HYPOTHESIS TESTED HERE is different and has genuine prior support: the
variance risk premium is time-varying. It should be large when the market
prices a move much bigger than recent realised volatility justifies, and
absent (or negative) when implied is already cheap relative to realised. If
so, selling SELECTIVELY -- only when implied is rich -- beats selling always.

WHY THIS IS NOT CURVE FITTING. The test is a MONOTONICITY check across
quintiles of a single, economically-motivated conditioning variable, not a
search for a threshold that happens to work. A real effect should show P&L
rising steadily from Q1 to Q5. A single good bucket surrounded by noise is
data mining and will be reported as such.

CONDITIONING VARIABLE
    richness = implied_move_annualised / trailing_20d_realised_vol
where implied_move_annualised is backed out of the ATM straddle credit
(credit/spot scaled to annual terms over the holding horizon) and trailing
realised vol is computed from NIFTY daily closes BEFORE the entry date only
(no look-ahead).

Nothing here places or would place a real order.
"""
from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ARCHIVE = Path(__file__).resolve().parent.parent.parent / "IEA_data_20260915" / "data" / "store" / "options"
TRAIL_DAYS = 20
COST_PTS_PER_LEG = 1.5


def daily_spot_series() -> pd.Series:
    """One closing spot per trading date, taken from whichever expiry file
    covers it. Used only for TRAILING realised vol (never forward-looking)."""
    closes: dict[str, float] = {}
    for e in sorted(os.listdir(ARCHIVE)):
        if not e.startswith("expiry="):
            continue
        for d in os.listdir(ARCHIVE / e):
            if not d.startswith("date="):
                continue
            date_str = d.split("=", 1)[1]
            if date_str in closes:
                continue
            p = ARCHIVE / e / d / "bars.parquet"
            try:
                df = pd.read_parquet(p, columns=["ts", "spot"])
            except Exception:
                continue
            if df.empty:
                continue
            closes[date_str] = float(df.sort_values("ts")["spot"].iloc[-1])
    s = pd.Series(closes).sort_index()
    return s[s > 0]


def trailing_realised_vol(spot: pd.Series, window: int = TRAIL_DAYS) -> pd.Series:
    """Annualised realised vol (%) from daily log returns, SHIFTED so the value
    on date D uses only data strictly BEFORE D."""
    r = np.log(spot / spot.shift(1))
    rv = r.rolling(window).std() * math.sqrt(252) * 100.0
    return rv.shift(1)


def quintile_report(df: pd.DataFrame, col: str, label: str) -> None:
    g = df.dropna(subset=[col, "net_pts"]).copy()
    if len(g) < 50:
        print(f"  {label}: too few rows ({len(g)})")
        return
    g["q"] = pd.qcut(g[col], 5, labels=[1, 2, 3, 4, 5], duplicates="drop")
    print(f"\n  {label}   (n={len(g)})")
    print(f"    {'quintile':>9} {'n':>5} {'richness':>9} {'net pts':>9} {'win%':>7} "
          f"{'p5':>8} {'worst':>9}")
    means = []
    for q, h in g.groupby("q", observed=True):
        means.append(h["net_pts"].mean())
        print(f"    {str(q):>9} {len(h):>5} {h[col].mean():>9.2f} {h['net_pts'].mean():>9.1f} "
              f"{100 * (h['net_pts'] > 0).mean():>7.1f} {np.percentile(h['net_pts'], 5):>8.1f} "
              f"{h['net_pts'].min():>9.1f}")
    if len(means) == 5:
        ordered = all(means[i] <= means[i + 1] for i in range(4))
        spread = means[-1] - means[0]
        # Spearman == Pearson on ranks; computed directly since scipy is absent.
        rho = g[col].rank().corr(g["net_pts"].rank())
        print(f"    monotonic increasing: {ordered}   Q5-Q1 spread = {spread:>8.1f} pts"
              f"   spearman rho = {rho:+.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="data/orderflow/research_vol_premium.csv")
    ap.add_argument("--train-end", default="2025-06-30")
    args = ap.parse_args()

    trades = pd.read_csv(args.trades)
    print("RESEARCH: is the variance risk premium CONDITIONALLY positive?")
    print(f"Loaded {len(trades)} trade-rows from {args.trades}")

    spot = daily_spot_series()
    rv = trailing_realised_vol(spot)
    print(f"Daily spot series: {len(spot)} sessions, {spot.index.min()} .. {spot.index.max()}")

    t = trades[trades["hold"] == 99].copy()          # hold-to-expiry only
    t["net_pts"] = t["gross_pts"] - t["n_legs"] * 2.0 * COST_PTS_PER_LEG
    t["trail_rv"] = t["entry_date"].map(rv)

    # Implied move implied by the ATM straddle credit, annualised over the hold.
    atm = t[t["structure"] == "straddle_ATM"][["entry_date", "credit", "spot_in", "days_held"]]
    atm = atm.drop_duplicates("entry_date").set_index("entry_date")
    t["atm_credit"] = t["entry_date"].map(atm["credit"])
    t["implied_vol_est"] = (
        (t["atm_credit"] / t["spot_in"]) / np.sqrt(t["days_held"].clip(lower=1) / 365.0)
        * 100.0 / 0.7979            # ATM straddle ~= 0.7979 * S * sigma * sqrt(T)
    )
    t["richness"] = t["implied_vol_est"] / t["trail_rv"]
    t["iv_minus_rv"] = t["implied_vol_est"] - t["trail_rv"]

    train = t[t["entry_date"] <= args.train_end]
    test = t[t["entry_date"] > args.train_end]
    print(f"\nTrain rows {len(train)} | Test rows {len(test)}  (split at {args.train_end})")

    for sname in ["straddle_ATM", "strangle_200", "strangle_300", "IC_200w400"]:
        print(f"\n{'=' * 74}\n{sname}\n{'=' * 74}")
        for frame, tag in ((train, "TRAIN"), (test, "TEST (out-of-sample)")):
            sub = frame[frame["structure"] == sname]
            quintile_report(sub, "richness", f"{tag}: sorted by richness (IV_est / trailing RV)")

    print("\n\nINTERPRETATION GUIDE")
    print("  A real time-varying premium shows: monotonic increase Q1->Q5, a large")
    print("  positive Q5-Q1 spread, positive spearman rho, AND the same pattern")
    print("  surviving out-of-sample. One good bucket alone is noise, not an edge.")


if __name__ == "__main__":
    main()

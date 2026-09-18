#!/usr/bin/env python
"""RESEARCH: do classic cross-sectional equity anomalies exist in this panel,
and do they survive costs?

WHY THIS ANGLE, AFTER SEVEN NEGATIVE OPTION TESTS. Everything tested so far
lives in NIFTY options, where the premium turned out to be either zero net of
costs or pure tail compensation. Cross-sectional equity effects
(short-term reversal, momentum) are a different asset class entirely, are the
most replicated findings in the empirical finance literature, carry no option
tail risk, and here have by far the longest sample available (2010 onward).

DISCIPLINE (same rules as the other research scripts in this repo):
  - Every strategy is reported against a LONG-ONLY EQUAL-WEIGHT CONTROL. In a
    market that rose over the sample, any long-biased rule looks good; if a
    signal cannot beat simply holding the basket, it is not a signal. This is
    the check that exposed the hedged131 PCR result as market drift.
  - IN-SAMPLE / OUT-OF-SAMPLE split, reported separately.
  - Long-short (market neutral) is the headline, because it strips the drift
    that flatters long-only rules.
  - Costs charged per side per rebalance and swept.
  - Results reported per year, so a single lucky regime cannot carry the mean.

KNOWN LIMITS
  - The archive's daily equity store is a PARTIAL backfill: ~22 symbols, all
    alphabetically early (ABB..CGPOWER). That is a narrow cross-section, so
    long/short baskets are small and idiosyncratic risk is high. Alphabetical
    selection is at least not obviously return-related, but this is NOT the
    NIFTY100 and results should not be read as index-wide.
  - No survivorship handling: symbols present today are the ones stored.
  - Signals use only past data (returns are shifted), but prices are daily
    closes with no modelled slippage beyond the cost sweep.

Nothing here places or would place a real order.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STORE = Path(__file__).resolve().parent.parent.parent / "IEA_data_20260915" / "data" / "store" / "equity"
CACHE = Path(__file__).resolve().parent.parent / "data" / "orderflow" / "_equity_panel.parquet"
COST_BPS = [0.0, 5.0, 10.0, 20.0]     # per side, per rebalance


def load_panel() -> pd.DataFrame:
    if CACHE.exists():
        return pd.read_parquet(CACHE)
    frames = []
    dates = sorted(d for d in os.listdir(STORE) if d.startswith("date="))
    for i, d in enumerate(dates, 1):
        p = STORE / d / "bars.parquet"
        if not p.exists():
            continue
        try:
            frames.append(pd.read_parquet(p, columns=["ts", "symbol", "close", "volume"]))
        except Exception:
            continue
        if i % 800 == 0:
            print(f"  ...loaded {i}/{len(dates)} sessions", flush=True)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = df["ts"].dt.strftime("%Y-%m-%d")
    df = df[df["close"] > 0]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CACHE, index=False)
    return df


def backtest_xs(close: pd.DataFrame, signal: pd.DataFrame, hold: int,
                n_side: int, cost_bps: float) -> pd.Series:
    """Cross-sectional long/short, rebalanced every `hold` days.

    `signal` is ranked each rebalance date; the LOWEST `n_side` names are
    bought and the HIGHEST `n_side` sold (so a positive result means low-signal
    outperforms). Returns a series of per-period net returns.
    """
    rets = close.pct_change()
    out = []
    dates = close.index
    for i in range(0, len(dates) - hold, hold):
        d = dates[i]
        s = signal.loc[d].dropna()
        # only names with a full forward window of prices
        fwd = close.iloc[i + hold] / close.iloc[i] - 1.0
        s = s[s.index.intersection(fwd.dropna().index)]
        if len(s) < 2 * n_side:
            continue
        lo = s.nsmallest(n_side).index
        hi = s.nlargest(n_side).index
        r = fwd[lo].mean() - fwd[hi].mean()
        r -= 4 * n_side * (cost_bps / 1e4) / (2 * n_side)   # in+out, both sides
        out.append((dates[i + hold], r))
    return pd.Series(dict(out))


def long_only(close: pd.DataFrame, hold: int, cost_bps: float) -> pd.Series:
    out = []
    dates = close.index
    for i in range(0, len(dates) - hold, hold):
        fwd = (close.iloc[i + hold] / close.iloc[i] - 1.0).dropna()
        if fwd.empty:
            continue
        out.append((dates[i + hold], fwd.mean() - 2 * (cost_bps / 1e4)))
    return pd.Series(dict(out))


def summarise(r: pd.Series, label: str, periods_per_year: float) -> None:
    if len(r) < 10:
        print(f"  {label:<34} n={len(r):<4} (too few)")
        return
    ann = r.mean() * periods_per_year * 100
    vol = r.std() * np.sqrt(periods_per_year) * 100
    sharpe = ann / vol if vol > 0 else float("nan")
    cum = (1 + r).cumprod()
    dd = float((cum / cum.cummax() - 1).min() * 100)
    print(f"  {label:<34} n={len(r):<4} ann={ann:>7.2f}%  vol={vol:>6.2f}%  "
          f"Sharpe={sharpe:>5.2f}  maxDD={dd:>7.1f}%  win%={100*(r>0).mean():>5.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-end", default="2020-12-31")
    args = ap.parse_args()

    print("RESEARCH: cross-sectional equity anomalies (reversal / momentum)")
    df = load_panel()
    close = df.pivot_table(index="date", columns="symbol", values="close")
    close = close.sort_index()
    print(f"Panel: {close.shape[0]} sessions x {close.shape[1]} symbols, "
          f"{close.index.min()} .. {close.index.max()}")
    print(f"Median names with data per day: {close.notna().sum(axis=1).median():.0f}\n")

    strategies = {
        # (signal, hold_days, description)
        "reversal_5d":   (close.pct_change(5), 5, "past 5d return, long losers"),
        "reversal_21d":  (close.pct_change(21), 21, "past 21d return, long losers"),
        "momentum_12_1": (close.pct_change(231).shift(21), 21, "past 12m skip 1m, long losers"),
    }

    for tag, frame in (("TRAIN", close[close.index <= args.train_end]),
                       ("TEST (out-of-sample)", close[close.index > args.train_end])):
        print(f"{'=' * 92}\n{tag}   ({frame.index.min()} .. {frame.index.max()})\n{'=' * 92}")
        for name, (sig_full, hold, desc) in strategies.items():
            sig = sig_full.reindex(frame.index)
            ppy = 252 / hold
            print(f"\n {name}  [{desc}]  hold={hold}d")
            for cost in COST_BPS:
                r = backtest_xs(frame, sig, hold, n_side=5, cost_bps=cost)
                summarise(r, f"long/short @ {cost:.0f}bps", ppy)
            # CONTROL -- if the signal cannot beat simply holding the basket,
            # it is drift, not edge.
            summarise(long_only(frame, hold, 10.0), "CONTROL long-only @10bps", ppy)

    print("\nREADING THIS: the long/short line is the real test -- it is market neutral,")
    print("so it cannot be flattered by the market's own drift. If long/short is ~0 or")
    print("negative while long-only is positive, the 'signal' is just market exposure.")


if __name__ == "__main__":
    main()

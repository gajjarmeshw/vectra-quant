#!/usr/bin/env python
"""RESEARCH v2: cross-sectional equity anomalies on the full NIFTY100 panel.

WHAT CHANGED FROM v1 AND WHY. v1 (`research_equity_xsection.py`) found
short-term reversal that worked in-sample and died out-of-sample. But v1 ran on
the archive's partial `store/equity` backfill: 22 alphabetically-early symbols,
5 names per side. With baskets that small, idiosyncratic noise swamps any
cross-sectional effect, so v1 could not distinguish "no signal" from "signal
buried in noise". This version fixes the DATA and the CONSTRUCTION -- it does
not search for better parameters:

  1. 106-symbol NIFTY100 panel (built by `build_equity_panel.py`) instead of 22.
  2. 15 names per side instead of 5 -- diversifies away single-name risk.
  3. Inverse-volatility position weights, so one high-beta name cannot dominate
     the basket. Standard risk construction, not a fitted parameter.
  4. A liquidity screen, so the baskets contain names actually tradeable in size.
  5. A RANDOM-BASKET CONTROL: the same machinery picking names at random. If the
     signal's long/short spread is not clearly better than random selection, the
     apparent edge is basket-construction noise.

DISCIPLINE CARRIED OVER: long-only control, in/out-of-sample split, cost sweep,
per-year breakdown, and a t-statistic so significance is explicit rather than
eyeballed.

BIAS THAT REMAINS: the universe is NIFTY100 membership as of the archive build,
so delisted/dropped names are missing. Long-only figures are therefore biased UP
and are shown only as a benchmark, never as a proposed strategy. The
market-neutral long/short line is the honest test.

Nothing here places or would place a real order.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PANEL = Path(__file__).resolve().parent.parent / "data" / "orderflow" / "_nifty100_daily.parquet"
COST_BPS = [0.0, 5.0, 10.0, 20.0]
N_SIDE = 15
VOL_WINDOW = 20
MIN_TURNOVER_CR = 5.0       # crore/day median turnover to be considered tradeable


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    p = pd.read_parquet(PANEL)
    close = p.pivot_table(index="date", columns="symbol", values="close").sort_index()
    vol = p.pivot_table(index="date", columns="symbol", values="volume").sort_index()
    turnover_cr = (close * vol) / 1e7          # rupees -> crore
    return close, vol, turnover_cr


def inv_vol_weights(rets: pd.DataFrame, names, d_idx: int) -> pd.Series:
    """Inverse-volatility weights over a trailing window, normalised to sum 1."""
    lo = max(0, d_idx - VOL_WINDOW)
    v = rets.iloc[lo:d_idx][names].std()
    v = v.replace(0, np.nan).dropna()
    if v.empty:
        return pd.Series(1.0 / len(names), index=names)
    w = 1.0 / v
    return w / w.sum()


def run_xs(close: pd.DataFrame, turnover: pd.DataFrame, signal: pd.DataFrame,
           hold: int, cost_bps: float, long_low: bool,
           randomise: bool = False, seed: int = 7) -> pd.Series:
    """Cross-sectional long/short with inverse-vol weights, rebalanced every
    `hold` sessions. `long_low=True` buys the LOWEST signal values (reversal);
    False buys the highest (momentum). `randomise` replaces the signal ranking
    with random selection -- the control for basket-construction noise."""
    rets = close.pct_change(fill_method=None)
    rng = np.random.default_rng(seed)
    out = {}
    turn_log = []
    prev_long: set = set()
    prev_short: set = set()
    dates = close.index
    for i in range(VOL_WINDOW, len(dates) - hold, hold):
        d = dates[i]
        fwd = (close.iloc[i + hold] / close.iloc[i] - 1.0).dropna()
        liq = turnover.iloc[max(0, i - 20):i].median()
        elig = fwd.index.intersection(liq[liq >= MIN_TURNOVER_CR].index)
        s = signal.loc[d].dropna()
        s = s[s.index.intersection(elig)]
        if len(s) < 2 * N_SIDE:
            continue
        if randomise:
            picks = rng.permutation(s.index.to_numpy())
            lo_names, hi_names = list(picks[:N_SIDE]), list(picks[N_SIDE:2 * N_SIDE])
        elif long_low:
            lo_names, hi_names = list(s.nsmallest(N_SIDE).index), list(s.nlargest(N_SIDE).index)
        else:
            lo_names, hi_names = list(s.nlargest(N_SIDE).index), list(s.nsmallest(N_SIDE).index)
        wl = inv_vol_weights(rets, lo_names, i)
        ws = inv_vol_weights(rets, hi_names, i)
        r = float((fwd[wl.index] * wl).sum() - (fwd[ws.index] * ws).sum())

        # TURNOVER-AWARE COSTS. Charging a full round trip on the whole book
        # every rebalance (what this did before) is wrong: a name that stays in
        # the basket is never sold and re-bought. Only the CHANGED fraction of
        # each side is traded. For fast signals turnover approaches 100% and the
        # two are equivalent; for persistent signals the old model roughly
        # doubled the true cost and buried any slow-moving edge.
        now_long, now_short = set(lo_names), set(hi_names)
        if prev_long or prev_short:
            t_long = len(now_long - prev_long) / N_SIDE
            t_short = len(now_short - prev_short) / N_SIDE
        else:
            t_long = t_short = 1.0                    # initial build
        turnover_frac = (t_long + t_short) / 2.0
        turn_log.append(turnover_frac)
        r -= 2 * turnover_frac * (cost_bps / 1e4)     # both sides, only what traded
        prev_long, prev_short = now_long, now_short
        out[dates[i + hold]] = r
    s = pd.Series(out)
    s.attrs["turnover"] = float(np.mean(turn_log)) if turn_log else float("nan")
    return s


def long_only(close: pd.DataFrame, turnover: pd.DataFrame, hold: int, cost_bps: float) -> pd.Series:
    out = {}
    dates = close.index
    for i in range(VOL_WINDOW, len(dates) - hold, hold):
        fwd = (close.iloc[i + hold] / close.iloc[i] - 1.0).dropna()
        liq = turnover.iloc[max(0, i - 20):i].median()
        elig = fwd.index.intersection(liq[liq >= MIN_TURNOVER_CR].index)
        if len(elig) < 10:
            continue
        out[dates[i + hold]] = float(fwd[elig].mean() - 2 * (cost_bps / 1e4))
    return pd.Series(out)


def stats(r: pd.Series, label: str, ppy: float) -> dict | None:
    if len(r) < 12:
        print(f"    {label:<32} n={len(r):<4} (too few)")
        return None
    ann = r.mean() * ppy * 100
    vol = r.std() * math.sqrt(ppy) * 100
    sharpe = ann / vol if vol > 0 else float("nan")
    t = r.mean() / (r.std() / math.sqrt(len(r))) if r.std() > 0 else float("nan")
    cum = (1 + r).cumprod()
    dd = float((cum / cum.cummax() - 1).min() * 100)
    tno = r.attrs.get("turnover", float("nan"))
    tno_s = f"  turn={tno*100:>5.1f}%" if tno == tno else ""
    print(f"    {label:<32} n={len(r):<4} ann={ann:>7.2f}%  vol={vol:>6.2f}%  "
          f"Sh={sharpe:>5.2f}  t={t:>5.2f}  maxDD={dd:>7.1f}%  win%={100*(r>0).mean():>5.1f}{tno_s}")
    return {"ann": ann, "sharpe": sharpe, "t": t}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-end", default="2024-06-30")
    args = ap.parse_args()

    close, vol, turnover = load()
    print("RESEARCH v2: cross-sectional equity, full NIFTY100 panel")
    print(f"Panel: {close.shape[0]} sessions x {close.shape[1]} symbols, "
          f"{close.index.min()} .. {close.index.max()}")
    print(f"Basket: {N_SIDE}/side, inverse-vol weighted, liquidity >= Rs{MIN_TURNOVER_CR}cr/day\n")

    specs = [
        ("reversal_5d",   close.pct_change(5, fill_method=None),  5,  True),
        ("reversal_10d",  close.pct_change(10, fill_method=None), 10, True),
        ("reversal_21d",  close.pct_change(21, fill_method=None), 21, True),
        ("momentum_63d",  close.pct_change(63, fill_method=None), 21, False),
        ("momentum_12_1", close.pct_change(231, fill_method=None).shift(21), 21, False),
    ]

    for tag, frame_mask in (("TRAIN", close.index <= args.train_end),
                            ("TEST (out-of-sample)", close.index > args.train_end)):
        cl = close[frame_mask]
        tn = turnover.reindex(cl.index)
        print(f"{'=' * 100}\n{tag}   ({cl.index.min()} .. {cl.index.max()})\n{'=' * 100}")
        for name, sig_full, hold, long_low in specs:
            sig = sig_full.reindex(cl.index)
            ppy = 252 / hold
            direction = "long losers" if long_low else "long winners"
            print(f"\n  {name}  [{direction}]  hold={hold}d")
            for c in COST_BPS:
                stats(run_xs(cl, tn, sig, hold, c, long_low), f"long/short @ {c:.0f}bps", ppy)
            stats(run_xs(cl, tn, sig, hold, 10.0, long_low, randomise=True),
                  "CONTROL random baskets @10bps", ppy)
            stats(long_only(cl, tn, hold, 10.0), "CONTROL long-only @10bps", ppy)

    print("\nREADING THIS: the signal must beat BOTH controls. Beating long-only is not")
    print("enough on its own (long-only is survivorship-inflated here); and if the signal")
    print("does not beat RANDOM baskets, its 'edge' is just basket noise.")


if __name__ == "__main__":
    main()

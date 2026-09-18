#!/usr/bin/env python
"""RESEARCH: walk-forward signal selection on NIFTY100, 2024 onward.

THE QUESTION THIS ANSWERS. Earlier tests used a single train/test split and
found signals that looked excellent in-sample and died out-of-sample
(momentum_63d: Sharpe 1.65 / t=2.52 in train, -0.08 in test). The natural
objection is that a practitioner would not freeze one signal for years -- they
would keep using whatever has been working lately. This script tests exactly
that: "pick the best recent signal, deploy it next period, repeat."

That makes the FINETUNING ITSELF the strategy under test, which is the honest
way to evaluate it. If adaptive selection works, it shows up here. If picking
recent winners is just chasing noise, that shows up here too.

METHOD
  - Universe: NIFTY100 daily panel, 2024-01-01 onward (recent regime only).
  - All signals rebalance on the SAME 5-day cycle so observation counts are
    comparable and as high as the sample allows.
  - Each period, rank candidate signals by their realised return over the
    trailing LOOKBACK periods (past information only), deploy the best next
    period. No look-ahead.
  - Benchmarks it must beat: every static signal, an equal-weight BLEND of all
    signals, random baskets, and long-only.

WHY THE BLEND MATTERS. In most factor research, equal-weighting several weak
signals beats trying to time which one is hot, because selection adds variance
without adding information. If BLEND > ADAPTIVE here, that is direct evidence
against the finetuning thesis.

Costs are turnover-aware (only the changed fraction of each basket is charged).

Nothing here places or would place a real order.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

PANEL = Path(__file__).resolve().parent.parent / "data" / "orderflow" / "_nifty100_daily.parquet"
N_SIDE = 15
HOLD = 5
VOL_WINDOW = 20
MIN_TURNOVER_CR = 5.0
LOOKBACK = 12          # periods (~3 months at 5-day cycle) used to pick the hot signal


def load(from_date: str):
    p = pd.read_parquet(PANEL)
    p = p[p["date"] >= from_date]
    close = p.pivot_table(index="date", columns="symbol", values="close").sort_index()
    volu = p.pivot_table(index="date", columns="symbol", values="volume").sort_index()
    return close, (close * volu) / 1e7


def inv_vol_w(rets: pd.DataFrame, names, i: int) -> pd.Series:
    v = rets.iloc[max(0, i - VOL_WINDOW):i][names].std().replace(0, np.nan).dropna()
    if v.empty:
        return pd.Series(1.0 / len(names), index=names)
    w = 1.0 / v
    return w / w.sum()


def signal_returns(close, turnover, signal, long_low, cost_bps, randomise=False, seed=7):
    """Per-period net return series for one signal, turnover-aware costs."""
    rets = close.pct_change(fill_method=None)
    rng = np.random.default_rng(seed)
    out, prev_l, prev_s = {}, set(), set()
    dates = close.index
    for i in range(VOL_WINDOW, len(dates) - HOLD, HOLD):
        fwd = (close.iloc[i + HOLD] / close.iloc[i] - 1.0).dropna()
        liq = turnover.iloc[max(0, i - 20):i].median()
        elig = fwd.index.intersection(liq[liq >= MIN_TURNOVER_CR].index)
        s = signal.loc[dates[i]].dropna()
        s = s[s.index.intersection(elig)]
        if len(s) < 2 * N_SIDE:
            continue
        if randomise:
            pk = rng.permutation(s.index.to_numpy())
            ln, sh = list(pk[:N_SIDE]), list(pk[N_SIDE:2 * N_SIDE])
        elif long_low:
            ln, sh = list(s.nsmallest(N_SIDE).index), list(s.nlargest(N_SIDE).index)
        else:
            ln, sh = list(s.nlargest(N_SIDE).index), list(s.nsmallest(N_SIDE).index)
        wl, ws = inv_vol_w(rets, ln, i), inv_vol_w(rets, sh, i)
        r = float((fwd[wl.index] * wl).sum() - (fwd[ws.index] * ws).sum())
        t = 1.0 if not (prev_l or prev_s) else (
            len(set(ln) - prev_l) / N_SIDE + len(set(sh) - prev_s) / N_SIDE) / 2.0
        r -= 2 * t * (cost_bps / 1e4)
        prev_l, prev_s = set(ln), set(sh)
        out[dates[i + HOLD]] = r
    return pd.Series(out)


def long_only(close, turnover, cost_bps):
    out = {}
    dates = close.index
    for i in range(VOL_WINDOW, len(dates) - HOLD, HOLD):
        fwd = (close.iloc[i + HOLD] / close.iloc[i] - 1.0).dropna()
        liq = turnover.iloc[max(0, i - 20):i].median()
        elig = fwd.index.intersection(liq[liq >= MIN_TURNOVER_CR].index)
        if len(elig) < 10:
            continue
        out[dates[i + HOLD]] = float(fwd[elig].mean() - 2 * (cost_bps / 1e4))
    return pd.Series(out)


def stats(r: pd.Series, label: str):
    ppy = 252 / HOLD
    if len(r) < 10:
        print(f"  {label:<30} n={len(r):<4} (too few)")
        return
    ann = r.mean() * ppy * 100
    vol = r.std() * math.sqrt(ppy) * 100
    sh = ann / vol if vol else float("nan")
    t = r.mean() / (r.std() / math.sqrt(len(r))) if r.std() else float("nan")
    cum = (1 + r).cumprod()
    dd = float((cum / cum.cummax() - 1).min() * 100)
    print(f"  {label:<30} n={len(r):<4} ann={ann:>7.2f}%  vol={vol:>6.2f}%  Sh={sh:>5.2f}  "
          f"t={t:>5.2f}  maxDD={dd:>7.1f}%  win%={100*(r>0).mean():>5.1f}  total={100*(cum.iloc[-1]-1):>7.1f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-date", default="2024-01-01")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    args = ap.parse_args()

    close, turnover = load(args.from_date)
    print("WALK-FORWARD signal selection, NIFTY100")
    print(f"Panel: {close.shape[0]} sessions x {close.shape[1]} symbols, "
          f"{close.index.min()} .. {close.index.max()}")
    print(f"Rebalance every {HOLD}d, {N_SIDE}/side, inv-vol weighted, costs {args.cost_bps}bps "
          f"(turnover-aware), selection lookback {LOOKBACK} periods\n")

    specs = {
        "reversal_5d":   (close.pct_change(5, fill_method=None), True),
        "reversal_10d":  (close.pct_change(10, fill_method=None), True),
        "reversal_21d":  (close.pct_change(21, fill_method=None), True),
        "momentum_21d":  (close.pct_change(21, fill_method=None), False),
        "momentum_63d":  (close.pct_change(63, fill_method=None), False),
        "momentum_126d": (close.pct_change(126, fill_method=None), False),
    }

    series = {}
    for name, (sig, low) in specs.items():
        series[name] = signal_returns(close, turnover, sig, low, args.cost_bps)
    R = pd.DataFrame(series).dropna()
    print(f"Signal return matrix: {R.shape[0]} periods x {R.shape[1]} signals\n")

    print("=== STATIC signals (each held for the whole period) ===")
    for name in R.columns:
        stats(R[name], name)

    print("\n=== BENCHMARKS ===")
    stats(R.mean(axis=1), "BLEND equal-weight all")
    stats(signal_returns(close, turnover, specs["reversal_5d"][0], True,
                         args.cost_bps, randomise=True), "CONTROL random baskets")
    stats(long_only(close, turnover, args.cost_bps), "CONTROL long-only")

    # ---- the actual test: pick the recent winner, deploy it next period ----
    picks, adaptive = [], {}
    for i in range(LOOKBACK, len(R)):
        window = R.iloc[i - LOOKBACK:i]
        best = window.mean().idxmax()              # past information only
        picks.append(best)
        adaptive[R.index[i]] = R.iloc[i][best]
    adapt = pd.Series(adaptive)

    print("\n=== ADAPTIVE: 'finetune to whatever is working' ===")
    stats(adapt, f"ADAPTIVE (pick best of last {LOOKBACK})")
    # A fair comparison: the same periods, for every alternative.
    aligned = R.loc[adapt.index]
    print("\n  same-period comparison:")
    for name in aligned.columns:
        stats(aligned[name], f"  static {name}")
    stats(aligned.mean(axis=1), "  BLEND (same periods)")

    vc = pd.Series(picks).value_counts()
    print(f"\n  signals chosen: " + ", ".join(f"{k}x{v}" for k, v in vc.items()))
    hit = float(np.mean([adapt.iloc[i] > aligned.iloc[i].median()
                         for i in range(len(adapt))]))
    print(f"  adaptive beat the median signal in {100*hit:.1f}% of periods "
          f"(50% = selection adds nothing)")

    print("\nVERDICT GUIDE: if ADAPTIVE does not clearly beat BLEND and the best static")
    print("signals, then 'picking what's working' is chasing noise, not finetuning.")


if __name__ == "__main__":
    main()

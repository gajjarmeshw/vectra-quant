#!/usr/bin/env python
"""Multi-year intraday gamma-scalping backtest on the real IEA option archive,
using TRUE Black-Scholes delta computed from the archive's own implied vol.

This supersedes `backtest_20260918_gamma_scalp.py`, which could only run on
one partial session and had to estimate delta by rolling regression. Here:

  - ~605 real trading sessions (2020-10-05 .. 2026-09-11), full 09:15-15:30.
  - Each day trades the FRONT-WEEK expiry (the highest-gamma contract a real
    gamma scalper would hold), resolved per-date, not a fixed expiry.
  - Delta is real Black-Scholes delta from the archive's per-bar `iv`
    (stored in PERCENTAGE POINTS -- divided by 100 here; using it raw would
    silently produce nonsense deltas).

STRATEGY SIMULATED (one independent experiment per day):
  09:15  buy 1 lot ATM straddle (CE + PE) on the front-week expiry
  all day rehedge with the underlying whenever net delta drifts outside a
         band, booking the realised hedge P&L each time
  15:30  close everything, mark the straddle out

WHY THE OUTPUT IS A DECOMPOSITION, NOT ONE NUMBER: gamma scalping's entire
thesis is "hedge profits harvested from realised movement exceed the time
decay paid on the straddle". A single net P&L hides which half failed, so
every run reports the option leg (theta bleed) and the hedge leg (gamma
harvest) separately, plus the realised-vs-implied vol spread that predicts
their difference.

MODELLING CHOICES AND THEIR LIMITS -- read before trusting any number:

1. HEDGE INSTRUMENT. Real desks hedge with NIFTY futures; this uses the
   archive's own index `spot` series for both the delta input and the hedge
   fill. Futures carry a basis over spot, so real hedge fills would differ
   slightly. Using one consistent series avoids introducing an unmodelled
   basis, at the cost of ignoring it.

2. FRACTIONAL LOTS. Net delta drifts in fractions of a lot, and this
   simulates hedging that fraction exactly. NSE futures trade in WHOLE lots
   only, so a real implementation must round -- leaving residual directional
   risk this backtest does not charge for. Treat results as an upper bound
   on hedge precision.

3. TIME TO EXPIRY uses calendar time to 15:30 IST on expiry day over 365.
   Floored at one minute so expiry-day deltas stay finite.

4. COSTS are modelled as a flat rupee charge per lot traded (hedges) and per
   option leg (straddle entry/exit), swept across several levels because
   gamma scalping lives or dies on them. They are a stand-in for
   spread + brokerage + STT + exchange fees, not a broker-accurate schedule.

5. NO SLIPPAGE, NO MARGIN COST, NO GAP RISK between sessions (intraday only).

Nothing here places or would place a real order.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ARCHIVE = Path(__file__).resolve().parent.parent.parent / "IEA_data_20260915" / "data" / "store" / "options"
LOT_SIZE = 65
STRIKE_STEP = 50
RISK_FREE = 0.065          # India ~6.5%; negligible at weekly tenor, kept explicit
MINUTES_PER_YEAR = 365 * 24 * 60

# Swept because these two decide whether the strategy is viable at all.
BANDS = [0.05, 0.10, 0.20, 0.30, 99.0]  # rehedge when |net delta| exceeds this many lots
                                        # 99.0 == never rehedge (naked straddle, no hedging)
COSTS = [0.0, 150.0, 300.0]             # rupees per lot traded on a hedge
OPTION_LEG_COST = 40.0                  # MEASURED from real depth capture: ~0.14pt spread + brokerage + STT + exchange + GST


def norm_cdf(x: np.ndarray) -> np.ndarray:
    """Standard normal CDF via erf (scipy is not installed in this venv)."""
    return 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))


def bs_greeks(spot, strike, iv_frac, t_years, is_call) -> dict:
    """Black-Scholes delta/gamma/theta/vega. `iv_frac` is a DECIMAL fraction
    (0.103), not 10.3 -- the archive stores IV in percentage points.

    theta is per YEAR (scale by elapsed years), vega is per 1.0 of vol
    (i.e. per 100 vol points), gamma is per 1 point of underlying.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        sqrt_t = np.sqrt(t_years)
        vol_t = iv_frac * sqrt_t
        d1 = (np.log(spot / strike) + (RISK_FREE + 0.5 * iv_frac ** 2) * t_years) / vol_t
        d2 = d1 - vol_t
    pdf_d1 = np.exp(-0.5 * d1 ** 2) / math.sqrt(2.0 * math.pi)
    nd1 = norm_cdf(d1)
    disc = np.exp(-RISK_FREE * t_years)

    delta = np.where(is_call, nd1, nd1 - 1.0)
    gamma = pdf_d1 / (spot * vol_t)
    vega = spot * pdf_d1 * sqrt_t
    common_theta = -(spot * pdf_d1 * iv_frac) / (2.0 * sqrt_t)
    theta = np.where(
        is_call,
        common_theta - RISK_FREE * strike * disc * norm_cdf(d2),
        common_theta + RISK_FREE * strike * disc * norm_cdf(-d2),
    )
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def front_expiry_by_date() -> dict[str, str]:
    """For each trading date, the nearest expiry at/after it -- the front-week
    contract, which is what a gamma scalper actually holds."""
    front: dict[str, str] = {}
    for e in os.listdir(ARCHIVE):
        if not e.startswith("expiry="):
            continue
        exp = e.split("=", 1)[1]
        for d in os.listdir(ARCHIVE / e):
            if not d.startswith("date="):
                continue
            dt = d.split("=", 1)[1]
            if exp >= dt and (dt not in front or exp < front[dt]):
                front[dt] = exp
    return front


def load_atm_legs(date_str: str, expiry: str):
    """Returns (panel, atm_strike) where panel is a per-minute frame with
    ce/pe close+iv and spot, or (None, None) if the day is unusable."""
    path = ARCHIVE / f"expiry={expiry}" / f"date={date_str}" / "bars.parquet"
    if not path.exists():
        return None, None
    df = pd.read_parquet(path, columns=["ts", "strike", "right", "close", "iv", "spot"])
    if df.empty:
        return None, None

    spot0 = df.sort_values("ts")["spot"].iloc[0]
    if not np.isfinite(spot0) or spot0 <= 0:
        return None, None

    # Prefer the rounded ATM; fall back to the nearest strike that actually
    # quotes BOTH legs all day (a missing leg would silently half-hedge).
    target = round(spot0 / STRIKE_STEP) * STRIKE_STEP
    strikes = sorted(df["strike"].unique(), key=lambda k: abs(k - target))
    for strike in strikes[:6]:
        sub = df[df["strike"] == strike]
        ce = sub[sub["right"] == "CE"].set_index("ts").sort_index()
        pe = sub[sub["right"] == "PE"].set_index("ts").sort_index()
        if len(ce) < 60 or len(pe) < 60:
            continue
        panel = pd.DataFrame({
            "spot": ce["spot"],
            "ce": ce["close"], "ce_iv": ce["iv"].replace(0.0, np.nan),
            "pe": pe["close"], "pe_iv": pe["iv"].replace(0.0, np.nan),
        })
        panel[["ce_iv", "pe_iv"]] = panel[["ce_iv", "pe_iv"]].ffill().bfill()
        panel = panel.dropna()
        panel = panel[(panel["spot"] > 0) & (panel["ce"] > 0) & (panel["pe"] > 0)]
        if len(panel) < 60:
            continue
        return panel, float(strike)
    return None, None


def day_greeks_series(panel: pd.DataFrame, strike: float, expiry: str) -> pd.DataFrame:
    """Attach real BS Greeks for the long ATM straddle (1 CE + 1 PE)."""
    expiry_ts = pd.Timestamp(f"{expiry} 15:30", tz="Asia/Kolkata")
    minutes_left = (expiry_ts - panel.index).total_seconds() / 60.0
    t_years = np.maximum(minutes_left, 1.0) / MINUTES_PER_YEAR

    spot = panel["spot"].to_numpy()
    ce_iv = panel["ce_iv"].to_numpy() / 100.0
    pe_iv = panel["pe_iv"].to_numpy() / 100.0
    g_ce = bs_greeks(spot, strike, ce_iv, t_years, True)
    g_pe = bs_greeks(spot, strike, pe_iv, t_years, False)

    out = panel.copy()
    out["t_years"] = t_years
    out["straddle_iv"] = (ce_iv + pe_iv) / 2.0
    for k in ("delta", "gamma", "theta", "vega"):
        out[f"net_{k}"] = g_ce[k] + g_pe[k]
    out = out.rename(columns={"net_delta": "net_delta"})
    return out.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["net_delta", "net_gamma", "net_theta", "net_vega"]
    )


def greeks_attribution(day: pd.DataFrame) -> dict:
    """Decompose the STRADDLE's own P&L into the Greek sources that actually
    explain it. This is the decomposition that answers "which half of the
    thesis failed": gamma is the harvest (positive for long gamma), theta is
    the rent paid for it, vega is IV repricing.

    Note these are first-order attributions of a discretely-sampled path, so
    they do not sum exactly to realised option P&L; the gap is reported as an
    unexplained residual rather than silently absorbed.
    """
    spot = day["spot"].to_numpy()
    d_spot = np.diff(spot)
    d_iv = np.diff(day["straddle_iv"].to_numpy())
    d_t_elapsed = -np.diff(day["t_years"].to_numpy())   # time remaining shrinks

    delta = day["net_delta"].to_numpy()[:-1]
    gamma = day["net_gamma"].to_numpy()[:-1]
    theta = day["net_theta"].to_numpy()[:-1]
    vega = day["net_vega"].to_numpy()[:-1]

    return {
        "attr_delta": float(np.sum(delta * d_spot) * LOT_SIZE),
        "attr_gamma": float(np.sum(0.5 * gamma * d_spot ** 2) * LOT_SIZE),
        "attr_theta": float(np.sum(theta * d_t_elapsed) * LOT_SIZE),
        "attr_vega": float(np.sum(vega * d_iv) * LOT_SIZE),
    }


def simulate_day(day: pd.DataFrame, band: float, hedge_cost_per_lot: float,
                 pos: int = 1) -> dict:
    """Band-based delta hedging over one session. Returns the P&L decomposition.

    `pos` = +1 for a LONG straddle (buy vol, long gamma, pay theta) or -1 for a
    SHORT straddle (sell vol, short gamma, collect theta). The hedge always
    offsets the position's own delta, so its sign flips with `pos` too.

    Costs are ADDED in both directions -- they are a drag on the seller exactly
    as they are on the buyer, which is why a short run is NOT simply the
    negative of a long run.
    """
    spot = day["spot"].to_numpy()
    net_delta = day["net_delta"].to_numpy() * pos     # position delta, not contract delta

    hedge_lots = 0.0
    last_price = spot[0]
    hedge_pnl = 0.0
    traded_lots = 0.0
    n_rehedges = 0

    for i in range(len(day)):
        target = -net_delta[i]                      # offset the position's drift
        if abs(target - hedge_lots) >= band:
            hedge_pnl += hedge_lots * (spot[i] - last_price) * LOT_SIZE
            traded_lots += abs(target - hedge_lots)
            hedge_lots = target
            last_price = spot[i]
            n_rehedges += 1

    hedge_pnl += hedge_lots * (spot[-1] - last_price) * LOT_SIZE   # unwind at the close
    traded_lots += abs(hedge_lots)

    straddle_open = (day["ce"].iloc[0] + day["pe"].iloc[0]) * LOT_SIZE
    straddle_close = (day["ce"].iloc[-1] + day["pe"].iloc[-1]) * LOT_SIZE
    option_pnl = pos * (straddle_close - straddle_open)

    costs = traded_lots * hedge_cost_per_lot + 4 * OPTION_LEG_COST  # 2 legs in, 2 out
    return {
        "option_pnl": option_pnl, "hedge_pnl": hedge_pnl, "costs": costs,
        "net_pnl": option_pnl + hedge_pnl - costs,
        "n_rehedges": n_rehedges, "straddle_cost": straddle_open,
    }


def realised_vol(panel: pd.DataFrame) -> float:
    """Annualised realised vol of the day's 1-minute spot returns (%)."""
    r = np.diff(np.log(panel["spot"].to_numpy()))
    if len(r) < 10:
        return float("nan")
    return float(np.std(r, ddof=1) * math.sqrt(375 * 252) * 100.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-days", type=int, default=0, help="0 = all available days")
    # Default excludes pre-2024 sessions: NSE has revised index lot sizes and
    # weekly-expiry structure since, and this script applies a single current
    # LOT_SIZE throughout -- so older P&L would be scaled by the wrong contract
    # multiplier on top of being a different market regime.
    ap.add_argument("--from-date", default="2024-01-01", help="ignore sessions before this date")
    ap.add_argument("--side", choices=["long", "short"], default="long",
                    help="long = buy the straddle (classic gamma scalp); "
                         "short = sell it (harvest the vega/theta the buyer pays)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    pos = 1 if args.side == "long" else -1
    if args.out is None:
        args.out = f"data/orderflow/gamma_scalp_{args.side}_2024plus.csv"

    front = front_expiry_by_date()
    dates = [d for d in sorted(front) if d >= args.from_date]
    if args.max_days:
        dates = dates[-args.max_days:]
    if not dates:
        print(f"No sessions on/after {args.from_date}.")
        return
    side_label = "LONG straddle (buy vol)" if pos == 1 else "SHORT straddle (sell vol)"
    print(f"=== Gamma scalp [{side_label}]: {len(dates)} sessions, {dates[0]} .. {dates[-1]} ===")
    print(f"Real BS delta from archive IV | lot={LOT_SIZE} | front-week expiry per day\n")

    rows = []
    skipped = 0
    for n, date_str in enumerate(dates, 1):
        expiry = front[date_str]
        panel, strike = load_atm_legs(date_str, expiry)
        if panel is None:
            skipped += 1
            continue
        day = day_greeks_series(panel, strike, expiry)
        if len(day) < 60:
            skipped += 1
            continue

        dte = (pd.Timestamp(expiry) - pd.Timestamp(date_str)).days
        rv = realised_vol(panel)
        iv0 = float((day["ce_iv"].iloc[0] + day["pe_iv"].iloc[0]) / 2.0)

        base = {
            "date": date_str, "expiry": expiry, "dte": dte, "strike": strike,
            "entry_iv": iv0, "realised_vol": rv, "rv_minus_iv": rv - iv0,
            "spot_open": float(day["spot"].iloc[0]),
            "spot_range_pts": float(day["spot"].max() - day["spot"].min()),
            **{k: v * pos for k, v in greeks_attribution(day).items()},
        }
        for band in BANDS:
            for cost in COSTS:
                r = simulate_day(day, band, cost, pos)
                rows.append({**base, "band": band, "hedge_cost": cost, **r})

        if n % 100 == 0:
            print(f"  ...{n}/{len(dates)} sessions processed", flush=True)

    df = pd.DataFrame(rows)
    if df.empty:
        print("No usable sessions.")
        return

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    n_days = df["date"].nunique()
    print(f"\nUsable sessions: {n_days} (skipped {skipped})")
    print(f"Per-day results written to {out_path}\n")

    print("=== Realised P&L by rehedge band x hedge cost (rupees/day, 1 lot) ===")
    print("(option and hedge legs BOTH carry directional P&L that offsets between them --")
    print(" see the Greeks attribution below for what actually drives the result)")
    print(f"{'band':>6} {'cost/lot':>9} {'days':>5} {'optionLeg':>10} {'hedgeLeg':>10} "
          f"{'costs':>9} {'NET/day':>9} {'win%':>6} {'rehedges':>9}")
    for band in BANDS:
        for cost in COSTS:
            g = df[(df["band"] == band) & (df["hedge_cost"] == cost)]
            print(f"{band:>6.2f} {cost:>9.0f} {len(g):>5} {g['option_pnl'].mean():>10.0f} "
                  f"{g['hedge_pnl'].mean():>10.0f} {-g['costs'].mean():>9.0f} "
                  f"{g['net_pnl'].mean():>9.0f} {100*(g['net_pnl']>0).mean():>6.1f} "
                  f"{g['n_rehedges'].mean():>9.1f}")

    print("\n=== GREEKS ATTRIBUTION of the straddle (rupees/day, 1 lot) ===")
    print("This is the real test of the thesis: does the gamma harvest cover the theta rent?")
    per_day = df.drop_duplicates("date")
    print(f"  gamma harvest (the edge)      = {per_day['attr_gamma'].mean():>9.0f}/day")
    print(f"  theta rent (the cost)         = {per_day['attr_theta'].mean():>9.0f}/day")
    print(f"  --> gamma + theta (core edge) = {(per_day['attr_gamma'] + per_day['attr_theta']).mean():>9.0f}/day")
    print(f"  vega (IV repricing)           = {per_day['attr_vega'].mean():>9.0f}/day")
    print(f"  delta (hedged away in theory) = {per_day['attr_delta'].mean():>9.0f}/day")
    core = per_day["attr_gamma"] + per_day["attr_theta"]
    print(f"  days gamma beat theta: {int((core > 0).sum())}/{len(per_day)} "
          f"({100 * (core > 0).mean():.1f}%)")

    print("\n=== TAIL RISK (the decisive test for a short-vol book) ===")
    print("A strategy that wins most days and loses catastrophically on a few is not")
    print("viable on mean P&L alone -- these are the numbers that decide it.")
    print(f"{'band':>6} {'cost':>6} {'mean':>8} {'median':>8} {'p5':>8} {'p1':>8} "
          f"{'worst':>9} {'maxDD':>10} {'win%':>6}")
    for band in BANDS:
        for cost in (150.0,):
            g = df[(df["band"] == band) & (df["hedge_cost"] == cost)].sort_values("date")
            if g.empty:
                continue
            cum = g["net_pnl"].cumsum()
            max_dd = float((cum - cum.cummax()).min())
            label = "none" if band >= 99 else f"{band:.2f}"
            print(f"{label:>6} {cost:>6.0f} {g['net_pnl'].mean():>8.0f} "
                  f"{g['net_pnl'].median():>8.0f} {np.percentile(g['net_pnl'], 5):>8.0f} "
                  f"{np.percentile(g['net_pnl'], 1):>8.0f} {g['net_pnl'].min():>9.0f} "
                  f"{max_dd:>10.0f} {100*(g['net_pnl']>0).mean():>6.1f}")

    ref = df[(df["band"] == 0.10) & (df["hedge_cost"] == 150.0)]
    print("\n=== Reference config (band 0.10, cost 150/lot) ===")
    print(f"  mean net/day  = {ref['net_pnl'].mean():>10.0f}   median = {ref['net_pnl'].median():.0f}")
    print(f"  std  net/day  = {ref['net_pnl'].std():>10.0f}   win%   = {100*(ref['net_pnl']>0).mean():.1f}")
    print(f"  total over {len(ref)} days = {ref['net_pnl'].sum():.0f}")
    print(f"  mean straddle cost/day = {ref['straddle_cost'].mean():.0f} (capital at risk per lot)")

    print("\n=== By days-to-expiry (reference config) -- gamma is highest near expiry ===")
    print(f"{'dte':>5} {'days':>5} {'theta':>9} {'gamma':>9} {'NET/day':>9} {'win%':>6}")
    for dte, g in ref.groupby("dte"):
        if len(g) < 5:
            continue
        print(f"{dte:>5} {len(g):>5} {g['option_pnl'].mean():>9.0f} {g['hedge_pnl'].mean():>9.0f} "
              f"{g['net_pnl'].mean():>9.0f} {100*(g['net_pnl']>0).mean():>6.1f}")

    print("\n=== The edge condition: realised vol vs implied vol at entry ===")
    print("(thesis: profit when the day's realised movement exceeds what you paid in IV)")
    valid = ref.dropna(subset=["rv_minus_iv"])
    if len(valid) > 10:
        print(f"  corr(rv_minus_iv, net_pnl) = {valid['rv_minus_iv'].corr(valid['net_pnl']):.3f}")
        cheap = valid[valid["rv_minus_iv"] > 0]
        rich = valid[valid["rv_minus_iv"] <= 0]
        print(f"  days realised > implied: {len(cheap):>4}  mean net/day = {cheap['net_pnl'].mean():>8.0f}  "
              f"win% = {100*(cheap['net_pnl']>0).mean():.1f}")
        print(f"  days realised <= implied: {len(rich):>4}  mean net/day = {rich['net_pnl'].mean():>8.0f}  "
              f"win% = {100*(rich['net_pnl']>0).mean():.1f}")

    print("\nCaveats: spot used as hedge instrument (no futures basis); fractional-lot hedging")
    print("assumed (NSE trades whole lots only); costs are a flat stand-in, not a broker schedule.")


if __name__ == "__main__":
    main()

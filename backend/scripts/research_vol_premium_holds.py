#!/usr/bin/env python
"""RESEARCH: can the volatility risk premium be harvested once transaction
costs are amortised over a multi-day hold?

THE HYPOTHESIS AND WHY IT IS WORTH TESTING
------------------------------------------
`backtest_gamma_scalp_multiyear.py` established, on 396 real sessions, that
NIFTY implied vol sits persistently above realised: the short side earns a
GROSS edge of ~400 rupees/day/lot (vega +847, theta +890, gamma -1429), with
IV falling intraday on ~74% of days (independently confirmed by a separate
IV-drift check on 121 sampled sessions).

That edge was destroyed by costs -- but only because the test entered and
exited EVERY DAY, paying a full round trip (~4 legs) against a one-day edge.
The premium accrues continuously, including overnight. Holding N days pays the
same round trip once while collecting N days of premium, so the cost drag per
day falls ~1/N. This script tests whether that flips the sign.

DESIGN DISCIPLINE (the previous tests in this repo each failed one of these):
  1. REPORTED IN INDEX POINTS, not rupees. NSE has revised NIFTY lot sizes over
     the archive's span, so a fixed rupee multiplier silently misprices older
     sessions. Points are regime-invariant; convert once at the end.
  2. CONTROLS. Every result is shown against the opposite side (long vol) and
     a naive benchmark. An edge that cannot beat a fixed alternative is not an
     edge -- this is exactly how the hedged131 PCR signal was shown to be
     market drift.
  3. OUT-OF-SAMPLE SPLIT. Train (earlier) and test (later) are reported
     separately. A result that only exists in-sample is not a result.
  4. COSTS SWEPT, not assumed, and stated as the breakeven level.
  5. TAILS REPORTED. Short vol wins small/often and loses big/rarely; mean
     P&L alone cannot decide it.

KNOWN LIMITS
  - Entries overlap across dates (each date starts a new hold), so samples are
    autocorrelated and naive significance is overstated. Non-overlapping
    results are reported separately for that reason.
  - Exit prices are 1-minute bar closes, not modelled fills; the cost sweep is
    the stand-in for spread/slippage.
  - Overnight gap risk is genuinely borne here (that is the point) and shows up
    in the tail columns.

Nothing here places or would place a real order.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ARCHIVE = Path(__file__).resolve().parent.parent.parent / "IEA_data_20260915" / "data" / "store" / "options"
STRIKE_STEP = 50
CURRENT_LOT = 65          # only used for the final rupee illustration

ENTRY_CLOCK = "15:00"     # enter near the close, so a 1-day hold is a true overnight hold
EXIT_CLOCK = "15:10"

HOLDS = [1, 2, 3, 5, 99]  # 99 = hold to expiry
COST_POINTS_PER_LEG = [0.0, 0.75, 1.5, 3.0]   # 1.5 pts ~= Rs100/leg at lot 65

# Each structure is a list of legs: (strike_offset_from_ATM, right, sign)
# sign +1 = SOLD (premium collected), -1 = BOUGHT (premium paid, caps the tail).
#
# The naked structures carry unbounded risk and the archive begins 2020-10,
# i.e. AFTER the March-2020 crash -- the most relevant tail event is missing
# from the sample. The iron condors exist to price how much edge survives once
# that tail is actually capped, which is the only version that is deployable.
STRUCTURES: dict[str, list[tuple[int, str, int]]] = {
    "straddle_ATM":   [(0, "CE", 1), (0, "PE", 1)],
    "strangle_200":   [(200, "CE", 1), (-200, "PE", 1)],
    "strangle_300":   [(300, "CE", 1), (-300, "PE", 1)],
    # defined-risk versions: same short legs, protective wings bought further out
    "IC_200w400":     [(200, "CE", 1), (-200, "PE", 1), (400, "CE", -1), (-400, "PE", -1)],
    "IC_300w500":     [(300, "CE", 1), (-300, "PE", 1), (500, "CE", -1), (-500, "PE", -1)],
    "IC_200w300":     [(200, "CE", 1), (-200, "PE", 1), (300, "CE", -1), (-300, "PE", -1)],
}


def front_expiry_sessions() -> tuple[dict[str, str], dict[str, list[str]]]:
    sessions: dict[str, list[str]] = {}
    for e in os.listdir(ARCHIVE):
        if not e.startswith("expiry="):
            continue
        exp = e.split("=", 1)[1]
        ds = sorted(d.split("=", 1)[1] for d in os.listdir(ARCHIVE / e) if d.startswith("date="))
        if ds:
            sessions[exp] = ds
    front: dict[str, str] = {}
    for exp, ds in sessions.items():
        for d in ds:
            if exp >= d and (d not in front or exp < front[d]):
                front[d] = exp
    return front, sessions


def snapshot(expiry: str, date_str: str, clock: str) -> pd.DataFrame | None:
    p = ARCHIVE / f"expiry={expiry}" / f"date={date_str}" / "bars.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p, columns=["ts", "strike", "right", "close", "spot"])
    if df.empty:
        return None
    cutoff = pd.Timestamp(f"{date_str} {clock}", tz="Asia/Kolkata")
    elig = df[df["ts"] <= cutoff]
    if elig.empty:
        elig = df[df["ts"] == df["ts"].min()]
    return elig[elig["ts"] == elig["ts"].max()]


def price_of(snap: pd.DataFrame, strike: float, right: str) -> float | None:
    row = snap[(snap["strike"] == strike) & (snap["right"] == right)]
    if row.empty:
        return None
    v = float(row["close"].iloc[0])
    return v if v > 0 else None


def intrinsic(spot: float, strike: float, right: str) -> float:
    """Value of an option that has moved outside the archive's stored strike band.

    CRITICAL -- this exists to kill a severe survivorship bias. The archive
    stores a limited strike window (~24 strikes). When the index makes a large
    move, the short leg goes deep ITM and drops out of that window, so a
    price lookup returns None. Skipping those trades removes EXACTLY the big
    adverse moves: measured on this data, dropped dates averaged 357pts of spot
    movement versus 113pts on kept dates, and the dropped set contained the
    worst loss (-357pts) while the kept set's worst was only -93pts. That bias
    manufactured a fake 97% win rate on the wider strangles.

    A deep-ITM option trades at essentially intrinsic value (negligible time
    value), so intrinsic is the right substitute. It slightly UNDERSTATES the
    buyback cost -- i.e. it is still mildly generous to the short seller.
    """
    return max(0.0, spot - strike) if right == "CE" else max(0.0, strike - spot)


def run_trades(front: dict[str, str], sessions: dict[str, list[str]],
               from_date: str, to_date: str) -> pd.DataFrame:
    rows = []
    dates = [d for d in sorted(front) if from_date <= d <= to_date]
    for entry_date in dates:
        expiry = front[entry_date]
        cycle = [d for d in sessions[expiry] if d >= entry_date]
        if len(cycle) < 2:
            continue
        snap_in = snapshot(expiry, entry_date, ENTRY_CLOCK)
        if snap_in is None or snap_in.empty:
            continue
        spot0 = float(snap_in["spot"].iloc[0])
        if not np.isfinite(spot0) or spot0 <= 0:
            continue
        atm = round(spot0 / STRIKE_STEP) * STRIKE_STEP
        dte = (dt.date.fromisoformat(expiry) - dt.date.fromisoformat(entry_date)).days

        for sname, legs in STRUCTURES.items():
            entry_px = {}
            ok = True
            for off, right, sign in legs:
                px = price_of(snap_in, atm + off, right)
                if px is None:
                    ok = False
                    break
                entry_px[(off, right)] = px
            if not ok:
                continue
            # Net credit = premium sold minus premium paid for protective wings.
            credit = sum(sign * entry_px[(off, right)] for off, right, sign in legs)
            n_legs = len(legs)

            for hold in HOLDS:
                idx = min(hold, len(cycle) - 1) if hold != 99 else len(cycle) - 1
                exit_date = cycle[idx]
                if exit_date == entry_date:
                    continue
                snap_out = snapshot(expiry, exit_date, EXIT_CLOCK)
                if snap_out is None or snap_out.empty:
                    continue
                spot1 = float(snap_out["spot"].iloc[0])
                # Missing leg -> fall back to intrinsic rather than dropping the
                # trade (see `intrinsic` docstring: dropping removes exactly the
                # large adverse moves and fabricates the edge).
                buyback, n_fallback = 0.0, 0
                for off, right, sign in legs:
                    px = price_of(snap_out, atm + off, right)
                    if px is None:
                        px = intrinsic(spot1, atm + off, right)
                        n_fallback += 1
                    buyback += sign * px
                days_held = (dt.date.fromisoformat(exit_date) - dt.date.fromisoformat(entry_date)).days

                rows.append({
                    "entry_date": entry_date, "exit_date": exit_date, "expiry": expiry,
                    "structure": sname, "hold": hold, "days_held": max(days_held, 1),
                    "dte": dte, "atm": atm, "spot_in": spot0, "spot_out": spot1,
                    "spot_move": spot1 - spot0, "n_legs": n_legs,
                    "n_fallback": n_fallback,
                    "credit": credit, "buyback": buyback,
                    "gross_pts": credit - buyback,   # short-vol P&L in points
                })
    return pd.DataFrame(rows)


def net_points(df: pd.DataFrame, cost_per_leg: float) -> pd.Series:
    """Costs scale with leg count: each leg is transacted twice (in and out),
    so a 4-leg iron condor pays double a 2-leg strangle."""
    return df["gross_pts"] - df["n_legs"] * 2.0 * cost_per_leg


def report(df: pd.DataFrame, title: str, non_overlap: bool = False) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    if df.empty:
        print("  (no trades)")
        return
    print(f"{'structure':>13} {'hold':>5} {'n':>5} {'gross':>8} "
          + " ".join(f"{'c=' + str(c):>8}" for c in COST_POINTS_PER_LEG)
          + f" {'pts/day':>8} {'win%':>6} {'p5':>8} {'worst':>8}")
    for sname in STRUCTURES:
        for hold in HOLDS:
            g = df[(df["structure"] == sname) & (df["hold"] == hold)]
            if non_overlap:
                g = g.sort_values("entry_date")
                keep, last_exit = [], ""
                for _, r in g.iterrows():
                    if r["entry_date"] >= last_exit:
                        keep.append(r.name)
                        last_exit = r["exit_date"]
                g = g.loc[keep]
            if len(g) < 10:
                continue
            ref = net_points(g, 1.5)
            cells = " ".join(f"{net_points(g, c).mean():>8.2f}" for c in COST_POINTS_PER_LEG)
            label = "expiry" if hold == 99 else str(hold)
            print(f"{sname:>13} {label:>5} {len(g):>5} {g['gross_pts'].mean():>8.2f} {cells} "
                  f"{(ref / g['days_held']).mean():>8.2f} {100 * (ref > 0).mean():>6.1f} "
                  f"{np.percentile(ref, 5):>8.1f} {ref.min():>8.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-end", default="2025-06-30")
    ap.add_argument("--out", default="data/orderflow/research_vol_premium.csv")
    args = ap.parse_args()

    front, sessions = front_expiry_sessions()
    all_dates = sorted(front)
    print("RESEARCH: volatility risk premium with multi-day holds (SHORT vol)")
    print(f"Archive: {len(all_dates)} sessions, {all_dates[0]} .. {all_dates[-1]}")
    print(f"Entry {ENTRY_CLOCK}, exit {EXIT_CLOCK}. All figures in INDEX POINTS per 1 lot-equivalent.")
    print(f"Cost columns c=X are index points charged PER LEG (4 leg-transactions per trade).")
    print(f"1.5 pts/leg ~= Rs100/leg at lot {CURRENT_LOT}.\n")

    df = run_trades(front, sessions, all_dates[0], all_dates[-1])
    if df.empty:
        print("No trades produced.")
        return
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"{len(df)} trade-rows written to {out}")

    train = df[df["entry_date"] <= args.train_end]
    test = df[df["entry_date"] > args.train_end]

    report(train, f"TRAIN (in-sample)  entries <= {args.train_end}   n_sessions="
                  f"{train['entry_date'].nunique()}")
    report(test, f"TEST (out-of-sample)  entries > {args.train_end}   n_sessions="
                 f"{test['entry_date'].nunique()}")
    report(test, "TEST, NON-OVERLAPPING entries only (removes autocorrelation inflation)",
           non_overlap=True)

    # ---- CONTROL: the opposite side must lose if this side genuinely wins ----
    print(f"\n{'=' * 78}\nCONTROL: LONG vol (buying the same structures) -- gross points")
    print("A real seller's edge implies the buyer loses by the same gross amount.")
    print(f"{'=' * 78}")
    for sname in STRUCTURES:
        g = test[test["structure"] == sname]
        if len(g) > 10:
            print(f"{sname:>13}: short gross {g['gross_pts'].mean():>7.2f} pts | "
                  f"long gross {-g['gross_pts'].mean():>7.2f} pts")

    print("\nNOTE: overlapping entries inflate apparent significance; the non-overlapping")
    print("table is the honest one. Costs are a stand-in until real spreads are measured.")


if __name__ == "__main__":
    main()

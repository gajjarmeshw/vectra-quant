#!/usr/bin/env python
"""Backtest of the hedged131 ("Nifty Alpha Edge") weekly credit spread on the
real IEA option archive.

WHAT IS AND IS NOT BEING TESTED -- read this first:

The disclosed strategy picks its weekly direction from two order-flow charts
built on NIFTY's order BOOK (Chart 1: cumulative net of large resting orders;
Chart 2: running buy/sell imbalance). That data does not exist historically --
the archive holds OHLC/OI/IV bars, not depth -- so the real signal cannot be
replayed. This script substitutes PCR (put/call open-interest ratio) as the
direction source, exactly as `weekly_credit_spread_strategy.py` already does
and labels.

So this validates A VOL-SELLING STRUCTURE ON REAL DATA. It is NOT a replica of
hedged131 as deployed. A good result here does not validate the real signal,
and a bad one does not invalidate it.

WHAT IS FAITHFUL to the disclosure:
  - Structure: 200pt-wide credit spread, ATM short, protective wing bought
    FIRST (section 9). Bearish -> bear CALL spread; bullish -> bull PUT spread.
  - Both legs resolved on ONE expiry at one ATM reference; if either strike is
    not listed, the whole week is skipped (section 8).
  - Held through the week, force-exited on expiry day at 15:10 (section 14).
  - No stop-loss on the structure; risk is bounded by the bought wing.
  - Optional chained early exit when a linked futures monitor leg hits its
    weekly point target, widening late in the hold (sections 14.1/14.2).

SAMPLE SIZE CONSTRAINT: the archive only carries true WEEKLY expiries (7-day
spacing) from 2026-02-10 onward; everything earlier is monthly-only. A weekly
strategy therefore has ~30 real cycles here, not hundreds. Treat the output as
indicative, not conclusive.

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
LOT_SIZE = 65
STRIKE_STEP = 50
WING_PTS = 200.0

# Direction thresholds -- from WeeklyCreditSpreadStrategy.default_params.
PCR_BULLISH_ABOVE = 1.05
PCR_BEARISH_BELOW = 0.95

# Linked futures monitor leg (disclosure 14.1): weekly point target that widens
# late in the hold. Values from the repo's own OrderFlowCfg.
TARGET_PTS_EARLY = 40.0
TARGET_PTS_LATE = 70.0
TARGET_WIDEN_FROM_DAY = 5

ENTRY_TIME = "13:00"      # disclosure says a bounded mid-morning..mid-afternoon window
EXIT_TIME = "15:10"       # force-exit clock, section 14.2
OPTION_LEG_COST = 100.0   # rupees per leg; 2 in + 2 out


def expiry_sessions() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in os.listdir(ARCHIVE):
        if not e.startswith("expiry="):
            continue
        exp = e.split("=", 1)[1]
        ds = sorted(d.split("=", 1)[1] for d in os.listdir(ARCHIVE / e) if d.startswith("date="))
        if ds:
            out[exp] = ds
    return out


def weekly_expiries(sessions: dict[str, list[str]], from_date: str) -> list[tuple[str, str]]:
    """(previous_expiry, expiry) pairs spaced ~7 days -- i.e. genuine weeklies.

    The previous expiry matters: the archive stores every session on which a
    contract had data, which for a weekly can start 3-4 weeks before its own
    expiry (while it was still the far-week contract). A weekly cycle only
    begins the day AFTER the previous weekly expires, so entry must be bounded
    by it -- otherwise the trade is entered weeks early, at an ATM that no
    longer matches where the chain is liquid.
    """
    exps = sorted(e for e in sessions if e >= from_date)
    pairs = []
    for prev, cur in zip(exps, exps[1:]):
        gap = (dt.date.fromisoformat(cur) - dt.date.fromisoformat(prev)).days
        if 6 <= gap <= 8:
            pairs.append((prev, cur))
    return pairs


def load_session(expiry: str, date_str: str) -> pd.DataFrame | None:
    p = ARCHIVE / f"expiry={expiry}" / f"date={date_str}" / "bars.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p, columns=["ts", "strike", "right", "close", "oi", "spot"])
    return df if len(df) else None


def bar_at(df: pd.DataFrame, clock: str) -> pd.DataFrame:
    """All chain rows at the last timestamp at/before `clock` that day."""
    day = df["ts"].dt.date.iloc[0]
    cutoff = pd.Timestamp(f"{day} {clock}", tz="Asia/Kolkata")
    eligible = df[df["ts"] <= cutoff]
    if eligible.empty:
        return df[df["ts"] == df["ts"].min()]
    return eligible[eligible["ts"] == eligible["ts"].max()]


def pcr_at(snap: pd.DataFrame) -> float:
    ce = snap[snap["right"] == "CE"]["oi"].sum()
    pe = snap[snap["right"] == "PE"]["oi"].sum()
    return float(pe / ce) if ce > 0 else float("nan")


def leg_price(snap: pd.DataFrame, strike: float, right: str) -> float | None:
    row = snap[(snap["strike"] == strike) & (snap["right"] == right)]
    if row.empty:
        return None
    px = float(row["close"].iloc[0])
    return px if px > 0 else None


def run_week(prev_expiry: str, expiry: str, all_dates: list[str], use_monitor_exit: bool,
             force_direction: str | None = None) -> dict | None:
    """One weekly cycle: enter on the first session AFTER the previous weekly
    expiry (the real start of this contract's front-week), hold to expiry-day
    15:10 or an earlier monitor-target exit."""
    dates = [d for d in all_dates if prev_expiry < d <= expiry]
    if len(dates) < 2:
        return None                       # no usable holding period
    entry_date = dates[0]

    entry_df = load_session(expiry, entry_date)
    if entry_df is None:
        return None
    snap = bar_at(entry_df, ENTRY_TIME)
    if snap.empty:
        return None

    spot0 = float(snap["spot"].iloc[0])
    pcr = pcr_at(snap)
    if not np.isfinite(pcr) or not np.isfinite(spot0) or spot0 <= 0:
        return None

    if force_direction:                     # fixed-direction control, ignores PCR
        direction = force_direction
        right = "PE" if direction == "BULLISH" else "CE"
    elif pcr > PCR_BULLISH_ABOVE:
        direction, right = "BULLISH", "PE"
    elif pcr < PCR_BEARISH_BELOW:
        direction, right = "BEARISH", "CE"
    else:
        return {"expiry": expiry, "entry_date": entry_date, "pcr": pcr,
                "direction": "SKIP", "reason": "PCR_NEUTRAL"}

    atm = round(spot0 / STRIKE_STEP) * STRIKE_STEP
    wing = atm - WING_PTS if direction == "BULLISH" else atm + WING_PTS

    short_entry = leg_price(snap, atm, right)
    long_entry = leg_price(snap, wing, right)
    if short_entry is None or long_entry is None:
        # Disclosure section 8: a missing strike skips the whole week.
        return {"expiry": expiry, "entry_date": entry_date, "pcr": pcr,
                "direction": direction, "reason": "STRIKE_NOT_LISTED"}

    credit = short_entry - long_entry
    sign = 1.0 if direction == "BULLISH" else -1.0     # favourable spot direction

    # Walk forward to expiry, optionally exiting early on the monitor target.
    hold_dates = [d for d in dates if d >= entry_date]
    exit_date, exit_snap, exit_reason = None, None, "EXPIRY_FORCE_EXIT"
    for day_idx, d in enumerate(hold_dates):
        df = load_session(expiry, d)
        if df is None:
            continue
        if use_monitor_exit and d != entry_date:
            target = TARGET_PTS_LATE if day_idx >= TARGET_WIDEN_FROM_DAY else TARGET_PTS_EARLY
            intraday = df[["ts", "spot"]].drop_duplicates("ts").sort_values("ts")
            moved = sign * (intraday["spot"].to_numpy() - spot0)
            hit = np.where(moved >= target)[0]
            if len(hit):
                ts_hit = intraday["ts"].iloc[hit[0]]
                exit_snap = df[df["ts"] == ts_hit]
                exit_date, exit_reason = d, "MONITOR_TARGET"
                break
        if d == hold_dates[-1]:
            exit_snap = bar_at(df, EXIT_TIME)
            exit_date = d

    if exit_snap is None or exit_snap.empty:
        return None
    short_exit = leg_price(exit_snap, atm, right)
    long_exit = leg_price(exit_snap, wing, right)
    if short_exit is None:
        return None
    if long_exit is None:
        long_exit = 0.0                    # worthless wing at expiry is legitimate

    pnl_pts = (short_entry - short_exit) + (long_exit - long_entry)
    gross = pnl_pts * LOT_SIZE
    costs = 4 * OPTION_LEG_COST
    spot_exit = float(exit_snap["spot"].iloc[0])

    return {
        "expiry": expiry, "entry_date": entry_date, "exit_date": exit_date,
        "direction": direction, "reason": exit_reason, "pcr": pcr,
        "atm": atm, "wing": wing, "spot_entry": spot0, "spot_exit": spot_exit,
        "spot_move": spot_exit - spot0, "favourable_move": sign * (spot_exit - spot0),
        "credit": credit, "short_entry": short_entry, "long_entry": long_entry,
        "short_exit": short_exit, "long_exit": long_exit,
        "max_loss": -(WING_PTS - credit) * LOT_SIZE,
        "gross_pnl": gross, "costs": costs, "net_pnl": gross - costs,
        "hold_days": (dt.date.fromisoformat(exit_date) - dt.date.fromisoformat(entry_date)).days,
    }


def summarise(df: pd.DataFrame, label: str) -> None:
    traded = df[df["net_pnl"].notna()]
    print(f"\n=== {label} ===")
    if traded.empty:
        print("  no trades")
        return
    wins = traded[traded["net_pnl"] > 0]
    print(f"  cycles traded     : {len(traded)}   (skipped: {len(df) - len(traded)})")
    print(f"  net P&L total     : {traded['net_pnl'].sum():>10.0f}")
    print(f"  mean per cycle    : {traded['net_pnl'].mean():>10.0f}   median {traded['net_pnl'].median():.0f}")
    print(f"  win rate          : {100 * len(wins) / len(traded):>10.1f}%")
    print(f"  best / worst      : {traded['net_pnl'].max():>10.0f} / {traded['net_pnl'].min():.0f}")
    print(f"  std per cycle     : {traded['net_pnl'].std():>10.0f}")
    print(f"  mean credit (pts) : {traded['credit'].mean():>10.1f}")
    print(f"  mean max-loss cap : {traded['max_loss'].mean():>10.0f}")
    if len(traded) > 1 and traded["net_pnl"].std() > 0:
        print(f"  mean/std ratio    : {traded['net_pnl'].mean() / traded['net_pnl'].std():>10.2f}")
    print(f"  by direction      : " + ", ".join(
        f"{k}: n={len(g)} mean={g['net_pnl'].mean():.0f} win%={100*(g['net_pnl']>0).mean():.0f}"
        for k, g in traded.groupby("direction")))
    print(f"  by exit reason    : " + ", ".join(
        f"{k}: n={len(g)} mean={g['net_pnl'].mean():.0f}"
        for k, g in traded.groupby("reason")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-date", default="2026-02-01",
                    help="archive only has true weekly expiries from 2026-02-10")
    ap.add_argument("--out", default="data/orderflow/hedged131_weekly.csv")
    args = ap.parse_args()

    sessions = expiry_sessions()
    weeklies = weekly_expiries(sessions, args.from_date)
    print(f"=== hedged131 weekly credit spread (PCR direction stand-in) ===")
    if not weeklies:
        print("No true weekly expiry cycles found in range.")
        return
    print(f"True weekly expiry cycles found: {len(weeklies)}  "
          f"({weeklies[0][1]} .. {weeklies[-1][1]})")
    print(f"Structure: ATM short + {WING_PTS:.0f}pt protective wing (bought first), "
          f"lot={LOT_SIZE}, entry {ENTRY_TIME}, force-exit {EXIT_TIME}\n")

    for use_monitor in (False, True):
        rows = []
        for prev_exp, exp in weeklies:
            r = run_week(prev_exp, exp, sessions[exp], use_monitor)
            if r:
                rows.append(r)
        df = pd.DataFrame(rows)
        if df.empty:
            print("no usable cycles")
            continue
        label = "WITH monitor-leg early exit" if use_monitor else "HOLD TO EXPIRY (no early exit)"
        summarise(df, label)
        if not use_monitor:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out, index=False)
            print(f"  per-cycle rows -> {out}")
            traded = df[df["net_pnl"].notna()]
            if not traded.empty:
                print("\n  per-cycle detail (hold-to-expiry):")
                cols = ["entry_date", "exit_date", "direction", "pcr", "atm",
                        "credit", "favourable_move", "net_pnl"]
                print(traded[cols].to_string(index=False,
                      float_format=lambda v: f"{v:,.1f}"))

    # ---- CONTROL: does PCR actually beat a fixed direction? ----------------
    # NIFTY fell steadily across this window, so a mostly-bearish signal would
    # profit from directional beta alone. If PCR cannot beat always-BEARISH,
    # it is contributing nothing beyond that drift.
    print("\n\n=== CONTROL: PCR vs fixed-direction baselines (hold to expiry) ===")
    print("If PCR does not beat always-BEARISH, the result is market drift, not signal.")
    for forced in ("BEARISH", "BULLISH"):
        rows = []
        for prev_exp, exp in weeklies:
            r = run_week(prev_exp, exp, sessions[exp], False, force_direction=forced)
            if r:
                rows.append(r)
        ctrl = pd.DataFrame(rows)
        if not ctrl.empty:
            summarise(ctrl, f"ALWAYS {forced} (control)")

    print("\nCAVEAT: direction is PCR, not hedged131's real order-flow signal (no historical")
    print("depth data exists). This tests a vol-selling structure, not the deployed algo.")


if __name__ == "__main__":
    main()

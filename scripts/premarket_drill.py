#!/usr/bin/env python
"""Pre-market drill. Resolves everything that cannot be tested outside market hours.

Run it from the repo root between 08:45 and 09:20 IST:

    PYTHONPATH=backend .venv/bin/python scripts/premarket_drill.py

It is READ-ONLY except for step 6, which asks you to place one tiny order from the
Groww phone app so guardian detection (D-001) can be settled empirically. It never
places an order itself.

Results are appended to docs/DECISIONS.md.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from vectra_quant import config, logging_setup  # noqa: E402
from vectra_quant.brokers.groww import GrowwAdapter  # noqa: E402
from vectra_quant.core.session_clock import now_ist, session_date  # noqa: E402
from vectra_quant.core.sizing import size_with_cap  # noqa: E402

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(step: str, verdict: str, detail: str = "") -> None:
    results.append((step, verdict, detail))
    mark = {PASS: "\033[32m✓\033[0m", FAIL: "\033[31m✗\033[0m", WARN: "\033[33m!\033[0m"}[verdict]
    print(f" {mark} {step}" + (f" — {detail}" if detail else ""))


def main() -> int:
    logging_setup.setup("ERROR")
    s = config.get()
    print(f"\nVECTRA_QUANT pre-market drill · {now_ist().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"mode={s.mode} capital={s.capital:.0f} target={s.risk.target:.0f} "
          f"loss_limit={s.risk.loss_limit:.0f} r={s.risk.risk_per_trade:.0f}")
    print("-" * 66)

    # 1 · auth
    try:
        a = GrowwAdapter(s.secrets.groww_api_key, s.secrets.groww_totp_seed)
        _ = a.api          # forces the TOTP -> access-token round trip
        record("1 auth (TOTP -> access token)", PASS)
    except Exception as exc:
        record("1 auth", FAIL, str(exc)[:120])
        return finish()

    # 2 · instrument master
    try:
        m = a.get_instruments(force=True)
        if len(m) < 10_000:
            record("2 instrument master", FAIL, f"only {len(m)} rows")
        else:
            record("2 instrument master", PASS, f"{len(m):,} rows")
    except Exception as exc:
        record("2 instrument master", FAIL, str(exc)[:120])
        return finish()

    # 3 · expiries, lot sizes, expiry-day detection
    names = [s.instruments.primary, s.instruments.secondary]
    today = session_date()
    for name in names:
        exp = m.nearest_expiry(name, today)
        if not exp:
            record(f"3 {name} expiry", FAIL, "no expiry on or after today")
            continue
        opts = m.options(name, exp)
        lot = opts[0].lot_size if opts else 0
        flag = " EXPIRY TODAY — entries close 14:30" if exp == today else ""
        record(f"3 {name} chain", PASS if lot > 0 else FAIL,
               f"expiry {exp} · {len(opts)} contracts · lot {lot}{flag}")

    # 4 · live prices
    try:
        px = a.get_ltp_batch(["NIFTY", "SENSEX", "INDIAVIX"], segment="CASH")
        missing = [k for k in ("NIFTY", "SENSEX", "INDIAVIX") if not px.get(k)]
        record("4 live prices (REST, D-012)", FAIL if missing else PASS,
               ", ".join(f"{k} {v:.2f}" for k, v in px.items()) or f"missing {missing}")
    except Exception as exc:
        record("4 live prices", FAIL, str(exc)[:120])
        px = {}

    # 5 · ATM option quote + the sizing gate on a real contract
    primary = s.instruments.primary
    spot = px.get(primary, 0)
    if spot:
        exp = m.nearest_expiry(primary, today)
        strikes = m.strikes(primary, exp)
        atm = min(strikes, key=lambda x: abs(x - spot))
        inst = m.find_option(primary, exp, atm, "CE")
        try:
            q = a.get_quote([inst.trading_symbol], segment="FNO").get(inst.trading_symbol)
            if q and q.last_price > 0:
                sl_pts = s.risk.default_sl_premium_pts.get(primary, 35)
                sized = size_with_cap(
                    max_risk=s.risk.risk_per_trade, sl_points_premium=sl_pts,
                    lot_size=inst.lot_size, premium=q.last_price, capital=s.capital,
                    max_position_cost=s.sizing.max_position_cost,
                    min_lots=s.sizing.min_lots,
                )
                verdict = PASS if sized.allowed else WARN
                record("5 ATM quote + sizing gate", verdict,
                       f"{inst.trading_symbol} @ {q.last_price:.2f} · "
                       f"{sized.lots} lot · cost ₹{sized.cost:,.0f} · risk ₹{sized.risk:,.0f}"
                       + ("" if sized.allowed else f" · REJECTED: {sized.reason}"))
            else:
                record("5 ATM quote", FAIL, "no price returned")
        except Exception as exc:
            record("5 ATM quote", FAIL, str(exc)[:120])
    else:
        record("5 ATM quote", FAIL, "no spot price to locate ATM")

    # 6 · guardian detection — the D-001 question
    print("\n" + "-" * 66)
    print("  STEP 6 — guardian detection (settles D-001)")
    print("  Place ONE tiny order from the Groww PHONE APP now.")
    print("  Use NIFTY or SENSEX options (FNO). NOT MCX — the SDK has no commodity")
    print("  order feed, so an MCX order proves nothing about the websocket.")
    print("  For a websocket-vs-REST latency verdict, run scripts/ws_probe.py instead.")
    print("  Then press Enter. Ctrl-C to skip.")
    try:
        input("  > ")
        before = {o.order_id for o in a.get_orders()}
        print("  watching the order book for 30s...")
        import time
        seen = None
        for _ in range(10):
            time.sleep(3)
            for o in a.get_orders():
                if o.order_id not in before:
                    seen = o
                    break
            if seen:
                break
        if seen:
            record("6 guardian REST detection", PASS,
                   f"saw {seen.trading_symbol} {seen.side.value} within 30s")
            print("     REST polling works. `guardian.detection: rest_poll` is correct.")
        else:
            record("6 guardian REST detection", WARN,
                   "no new order seen — did the order actually place?")
    except KeyboardInterrupt:
        record("6 guardian detection", WARN, "skipped by operator")

    # 7 · funds + existing exposure
    try:
        f = a.get_funds()
        verdict = PASS if f.available > 0 else WARN
        record("7 funds", verdict, f"available ₹{f.available:,.0f}")
        if f.available > s.capital * 1.05:
            record("7 capital starvation (§4.1)", WARN,
                   f"broker funds ₹{f.available:,.0f} exceed configured capital "
                   f"₹{s.capital:,.0f} — park the surplus")
    except Exception as exc:
        record("7 funds", FAIL, str(exc)[:120])

    try:
        open_pos = [p for p in a.get_positions() if p.is_open]
        if open_pos:
            record("8 pre-existing positions", WARN,
                   "; ".join(f"{p.trading_symbol} qty={p.quantity}" for p in open_pos))
            print("     Guardian will adopt these and may attach stops. See RUNBOOK.")
        else:
            record("8 pre-existing positions", PASS, "flat")
    except Exception as exc:
        record("8 positions", FAIL, str(exc)[:120])

    return finish()


def finish() -> int:
    fails = [r for r in results if r[1] == FAIL]
    warns = [r for r in results if r[1] == WARN]
    print("-" * 66)
    print(f"  {len(results) - len(fails) - len(warns)} pass · {len(warns)} warn · {len(fails)} FAIL")
    verdict = "FAIL" if fails else ("PASS WITH WARNINGS" if warns else "PASS")
    print(f"  DRILL: {verdict}")
    if fails:
        print("  Do NOT start the backend until the failures above are resolved.")
    print("-" * 66 + "\n")

    log = ROOT / "docs" / "DECISIONS.md"
    with log.open("a") as fh:
        fh.write(f"\n---\n\n## Drill run · {datetime.now().strftime('%Y-%m-%d %H:%M')} IST\n\n")
        fh.write(f"Verdict: **{verdict}**\n\n")
        for step, v, detail in results:
            fh.write(f"- `{v}` {step}{f' — {detail}' if detail else ''}\n")
    print(f"  appended to {log.relative_to(ROOT)}\n")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

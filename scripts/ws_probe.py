#!/usr/bin/env python
"""Websocket order-feed probe — settles D-001 with one cancellable order.

Groww's docs do not say whether the order-update websocket reports orders placed in
the phone app. This listens to the raw feed and tells you, with latency numbers.

    PYTHONPATH=backend .venv/bin/python scripts/ws_probe.py

Then, in the Groww phone app, place a LIMIT order on a NIFTY or SENSEX option far
from the market so it cannot fill — then cancel it. Unfilled orders cost nothing.

It MUST be an FNO order. The SDK exposes order feeds for equity and derivatives
only; there is no commodity feed, so an MCX order would show nothing and look like
a failure that isn't one.

This script places no orders and modifies nothing.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from vectra_quant import config, logging_setup  # noqa: E402
from vectra_quant.brokers.groww import GrowwAdapter  # noqa: E402
from vectra_quant.core.session_clock import is_market_hours, now_ist  # noqa: E402

WATCH_SECONDS = 300

ws_events: list[tuple[float, dict]] = []
rest_first_seen: dict[str, float] = {}


def main() -> int:
    logging_setup.setup("ERROR")
    s = config.get()
    a = GrowwAdapter(s.secrets.groww_api_key, s.secrets.groww_totp_seed)
    a.get_instruments()

    print(f"\nWS order-feed probe · {now_ist().strftime('%H:%M:%S IST')}")
    if not is_market_hours():
        print("  ! Market is CLOSED. Order feeds are typically silent outside hours.")
        print("    Run this between 09:15 and 15:30 IST for a meaningful result.\n")

    # Raw feed taps — deliberately not going through Guardian, so we observe the
    # transport itself rather than any of our own filtering.
    def on_fno() -> None:
        try:
            upd = a.feed.get_fno_order_update()
        except Exception as exc:
            print(f"  [fno] read error: {str(exc)[:100]}")
            return
        if upd:
            ws_events.append((time.time(), {"feed": "fno", "payload": upd}))
            print(f"  \033[32m[WS fno]\033[0m {json.dumps(upd, default=str)[:220]}")

    def on_eq() -> None:
        try:
            upd = a.feed.get_equity_order_update()
        except Exception as exc:
            print(f"  [eq] read error: {str(exc)[:100]}")
            return
        if upd:
            ws_events.append((time.time(), {"feed": "equity", "payload": upd}))
            print(f"  \033[32m[WS equity]\033[0m {json.dumps(upd, default=str)[:220]}")

    try:
        a._feed_thread.call(a.feed.subscribe_fno_order_updates, on_data_received=on_fno)
        print("  subscribed: FNO order updates")
    except Exception as exc:
        print(f"  FAILED to subscribe FNO: {str(exc)[:140]}")
        return 1
    try:
        a._feed_thread.call(a.feed.subscribe_equity_order_updates, on_data_received=on_eq)
        print("  subscribed: equity order updates")
    except Exception as exc:
        print(f"  (equity subscribe failed, continuing: {str(exc)[:100]})")

    baseline = {o.order_id for o in a.get_orders()}
    print(f"  baselined {len(baseline)} existing orders\n")
    print("  " + "=" * 62)
    print("  NOW: place a far-from-market LIMIT order on a NIFTY/SENSEX option")
    print("       in the Groww PHONE APP, then cancel it.")
    print(f"       Watching for {WATCH_SECONDS}s. Ctrl-C to stop early.")
    print("  " + "=" * 62 + "\n")

    started = time.time()
    try:
        while time.time() - started < WATCH_SECONDS:
            time.sleep(2)
            try:
                for o in a.get_orders():
                    if o.order_id not in baseline and o.order_id not in rest_first_seen:
                        rest_first_seen[o.order_id] = time.time()
                        print(f"  \033[33m[REST]\033[0m new order {o.order_id} "
                              f"{o.trading_symbol} {o.side.value} {o.status.value} "
                              f"(+{time.time() - started:.1f}s)")
            except Exception as exc:
                print(f"  [rest] poll error: {str(exc)[:100]}")
    except KeyboardInterrupt:
        print("\n  stopped early")

    print("\n  " + "=" * 62)
    print(f"  WS events received : {len(ws_events)}")
    print(f"  REST new orders    : {len(rest_first_seen)}")
    print("  " + "=" * 62)

    if ws_events and rest_first_seen:
        first_ws = min(t for t, _ in ws_events)
        first_rest = min(rest_first_seen.values())
        lead = first_rest - first_ws
        print("\n  \033[32mWEBSOCKET WORKS for app-placed orders.\033[0m")
        print(f"  It beat REST polling by {lead:.2f}s.")
        print("  -> set `guardian.detection: ws` in config/params.yaml")
    elif ws_events:
        print("\n  \033[32mWS delivered events\033[0m (REST saw no new order — "
              "did the order cancel before a poll landed?)")
        print("  -> websocket is live; `ws` mode is viable")
    elif rest_first_seen:
        print("\n  \033[31mWEBSOCKET DELIVERED NOTHING\033[0m while REST saw the order.")
        print("  -> Groww's order feed does NOT report app-placed orders.")
        print("  -> keep `guardian.detection: hybrid` (or rest_poll); 2s worst-case lag.")
    else:
        print("\n  Nothing observed at all. Either no order was placed, or the market")
        print("  is closed. Inconclusive — rerun during market hours.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

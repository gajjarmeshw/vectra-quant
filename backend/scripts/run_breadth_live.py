#!/usr/bin/env python
"""Entrypoint: run the cross-sectional breadth recorder + paper trader for
one trading day.

    cd backend
    python scripts/run_breadth_live.py

Requires DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in the environment. Starts
three concurrent 20-depth connections (Dhan batches at most 50 instruments
per connection): two covering the 100 NIFTY100 stocks, one covering a
window of NIFTY option strikes around the current spot -- the same
topology Arjun's own capture used, chosen because we know it works (we
successfully decoded his real capture against exactly this assumption).

Every fix discovered the hard way earlier today is applied from the start
here rather than being rediscovered live:
  - all scheduling uses now_ist(), never naive datetime.now()
  - get_instrument_data() is one ws.recv() call, not a persistent stream
    (see multi_recorder.py's own docstring) -- handled inside the recorder
  - blocking Dhan HTTP calls run via asyncio.to_thread so they don't starve
    the recorder tasks
  - a top-level supervisor restarts the whole process on any unhandled
    exception instead of leaving it dead for hours

Nothing here places a real order -- paper_trader.py-family code only ever
reads quotes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time as time_module
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vectra_quant import config as _config  # noqa: F401 -- side effect: loads .env

from dhanhq import DhanContext

from vectra_quant.brokers.base import Instrument
from vectra_quant.brokers.dhan import DhanAdapter
from vectra_quant.orderflow.breadth_paper_trader import BreadthPaperTrader
from vectra_quant.orderflow.config import DEFAULT_BREADTH_CFG, DEFAULT_CFG
from vectra_quant.orderflow.contract import resolve_nifty_option_window
from vectra_quant.orderflow.multi_recorder import MAX_INSTRUMENTS_PER_CONNECTION, MultiInstrumentDepthRecorder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("run_breadth_live")

RECORDS_ROOT = os.environ.get("ORDERFLOW_RECORDS_ROOT", "./data/orderflow")
REFERENCE_PATH = Path(__file__).resolve().parent.parent / "vectra_quant" / "orderflow" / "reference" / "nifty100_instruments.json"


def load_nifty100_instruments() -> list[Instrument]:
    """Static reference list (symbol + security_id), extracted from a real
    capture's own instrument manifest -- not fetched from the live master,
    since that would require re-deriving NIFTY100 membership ourselves
    (a genuinely hard, easy-to-get-subtly-wrong problem this sidesteps)."""
    with open(REFERENCE_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return [
        Instrument(
            trading_symbol=item["symbol"], exchange="NSE", segment=item.get("exchange_segment", "NSE_EQ"),
            lot_size=1, instrument_type="EQ", name=item["symbol"], exchange_token=str(item["security_id"]),
        )
        for item in raw
    ]


async def main() -> None:
    client_id = os.environ.get("DHAN_CLIENT_ID", "")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN", "")
    if not client_id or not access_token:
        log.error("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set -- refusing to start.")
        return

    adapter = DhanAdapter(client_id, access_token)
    master = adapter.get_instruments()
    adapter.set_instruments(master)
    log.info("Loaded instrument master: %d instruments.", len(master))

    equities = load_nifty100_instruments()
    log.info("Loaded %d NIFTY100 reference instruments.", len(equities))
    if len(equities) > 2 * MAX_INSTRUMENTS_PER_CONNECTION:
        log.warning("More than %d equities listed -- only the first %d will be recorded.",
                    2 * MAX_INSTRUMENTS_PER_CONNECTION, 2 * MAX_INSTRUMENTS_PER_CONNECTION)
        equities = equities[: 2 * MAX_INSTRUMENTS_PER_CONNECTION]
    batch0, batch1 = equities[:MAX_INSTRUMENTS_PER_CONNECTION], equities[MAX_INSTRUMENTS_PER_CONNECTION:]

    def instrument_resolver(right: str, strike: float) -> str:
        today = date.today()
        expiry = master.nearest_expiry("NIFTY", on_or_after=today.isoformat())
        inst = master.find_option("NIFTY", expiry, strike, right)
        if inst is None:
            raise RuntimeError(f"No listed NIFTY {right} at strike {strike} for expiry {expiry}")
        return inst.trading_symbol

    def quote_fn(symbol: str) -> float:
        quotes = adapter.get_quote([symbol])
        q = quotes.get(symbol)
        if q is None:
            raise RuntimeError(f"No quote available for {symbol}")
        return q.last_price

    def spot_fn() -> float:
        return adapter.get_ltp("NIFTY")

    # Blocking Dhan HTTP call -- run off the event loop so it can't starve
    # the recorder tasks created below (see run_thunderbolt_live.py's own
    # note on this; discovered the hard way there).
    spot = await asyncio.to_thread(spot_fn)
    log.info("Current NIFTY spot: %.1f", spot)

    option_window = resolve_nifty_option_window(master, spot=spot, on_or_after=date.today().isoformat())
    log.info("Resolved %d option instruments around spot %.1f.", len(option_window), spot)
    if len(option_window) > MAX_INSTRUMENTS_PER_CONNECTION:
        option_window = option_window[:MAX_INSTRUMENTS_PER_CONNECTION]

    dhan_context = DhanContext(client_id, access_token)

    recorder_eq0 = MultiInstrumentDepthRecorder(dhan_context, batch0, DEFAULT_CFG, RECORDS_ROOT, "equities_conn0")
    recorder_eq1 = MultiInstrumentDepthRecorder(dhan_context, batch1, DEFAULT_CFG, RECORDS_ROOT, "equities_conn1")
    recorder_opt = MultiInstrumentDepthRecorder(dhan_context, option_window, DEFAULT_CFG, RECORDS_ROOT, "options_conn0")

    recorder_tasks = [
        asyncio.create_task(recorder_eq0.run_forever()),
        asyncio.create_task(recorder_eq1.run_forever()),
        asyncio.create_task(recorder_opt.run_forever()),
    ]

    lot_size = option_window[0].lot_size if option_window else 65
    trader = BreadthPaperTrader(
        top_of_book_providers=[recorder_eq0.top_of_book_snapshot, recorder_eq1.top_of_book_snapshot],
        quote_fn=quote_fn, instrument_resolver_fn=instrument_resolver, spot_fn=spot_fn,
        cfg=DEFAULT_BREADTH_CFG, records_root=RECORDS_ROOT, lot_size=lot_size,
    )

    rec = await trader.run_session(session_date=date.today())
    log.info("Session finished: skipped=%s reason=%s position=%s", rec.skipped, rec.skip_reason, rec.position)

    for r in (recorder_eq0, recorder_eq1, recorder_opt):
        r.stop()
    await asyncio.gather(*recorder_tasks, return_exceptions=True)


if __name__ == "__main__":
    # Same supervisor pattern as run_thunderbolt_live.py -- a single
    # unhandled exception anywhere must not leave the recorder dead for
    # hours with nothing watching it (hit live once already, today).
    while True:
        try:
            asyncio.run(main())
            break
        except Exception:
            log.exception("run_breadth_live crashed -- restarting in 10s")
            time_module.sleep(10)

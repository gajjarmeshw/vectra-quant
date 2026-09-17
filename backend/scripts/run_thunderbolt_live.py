#!/usr/bin/env python
"""Entrypoint: run the real order-flow recorder + Thunderbolt paper trader
together for one trading day. This is the script to actually run tomorrow.

    cd backend
    python scripts/run_thunderbolt_live.py

Requires DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in the environment (same as the
rest of the app). Everything this script wires together is independently
unit-tested (depth_parser, charts, aggregator, thunderbolt_signal,
thunderbolt_strategy, paper_trader); THIS FILE'S OWN WIRING IS NOT -- it has
never run against a live feed. Watch its log output closely on first use.

What it does, end to end:
  1. Downloads the real Dhan instrument master and resolves the current
     NIFTY future + the NIFTY option chain's instrument entries.
  2. Starts a `DepthRecorder` against that future (real 200-level WebSocket
     depth via the `dhanhq` SDK).
  3. Computes a simple realized-volatility proxy from recent daily candles
     (intraday range / open, NOT a proper realized-vol estimator -- a
     labeled simplification, see `_recent_realized_vols_proxy`).
  4. Runs `ThunderboltPaperTrader.run_session()` for today, which is a
     no-op if today already has a recorded call (idempotent across restarts).

Nothing here places a real order. `paper_trader.py` only ever reads
quotes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vectra_quant import config as _config  # noqa: F401 -- side effect: loads .env, same as the main app

from dhanhq import DhanContext

from vectra_quant.brokers.dhan import DhanAdapter
from vectra_quant.core.session_clock import now_ist
from vectra_quant.orderflow.aggregator import SessionAggregator
from vectra_quant.orderflow.config import DEFAULT_THUNDERBOLT_CFG
from vectra_quant.orderflow.contract import resolve_current_nifty_future
from vectra_quant.orderflow.paper_trader import ThunderboltPaperTrader
from vectra_quant.orderflow.recorder import DepthRecorder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("run_thunderbolt_live")

RECORDS_ROOT = os.environ.get("ORDERFLOW_RECORDS_ROOT", "./data/orderflow")


def _recent_realized_vols_proxy(adapter: DhanAdapter, lookback_sessions: int) -> tuple[list[float], list[dict]]:
    """SIMPLIFICATION, not a real realized-vol estimator: daily
    (high-low)/open from recent NIFTY daily candles. Good enough to bucket
    LOW/MEDIUM/HIGH at a coarse level; not calibrated against anything.

    Returns (vols, raw_candles) -- the raw candles are returned too so the
    caller can persist them alongside today's recording. A future backtest
    replaying the depth ticks needs the exact same regime inputs this live
    run used, not whatever `get_candles` happens to return if re-fetched
    later against a rolled-forward historical window.
    """
    try:
        candles = adapter.get_candles("NIFTY", tf="1d", span=lookback_sessions + 2)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch recent candles for regime classification: %s", exc)
        return [], []
    window = candles[-lookback_sessions:]
    vols = [(c.high - c.low) / c.open * 100.0 for c in window if c.open]
    raw = [{"ts": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in window]
    return vols, raw


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

    def instrument_master_provider():
        return master._all  # noqa: SLF001 -- this script owns both objects; contract.py wants the raw list

    def instrument_resolver(right: str, strike: float) -> str:
        today = now_ist().date()
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

    from vectra_quant.orderflow.config import DEFAULT_CFG

    dhan_context = DhanContext(client_id, access_token)
    recorder = DepthRecorder(
        dhan_context=dhan_context,
        instrument_master_provider=instrument_master_provider,
        cfg=DEFAULT_CFG,
        storage_root=RECORDS_ROOT,
    )
    recorder_task = asyncio.create_task(recorder.run_forever())

    current_future = resolve_current_nifty_future(master._all, today=now_ist().date())  # noqa: SLF001
    trader = ThunderboltPaperTrader(
        aggregator=recorder.aggregator, quote_fn=quote_fn, instrument_resolver_fn=instrument_resolver,
        cfg=DEFAULT_THUNDERBOLT_CFG, records_root=RECORDS_ROOT,
        lot_size=current_future.instrument.lot_size,
    )

    # These are synchronous, blocking Dhan HTTP calls. Run them in a worker
    # thread rather than directly in this coroutine -- called inline, they
    # would starve the event loop and prevent `recorder_task` from ever
    # getting a turn to run (it was created above but Python won't switch to
    # it until this coroutine hits a real await point).
    recent_vols, recent_candles = await asyncio.to_thread(
        _recent_realized_vols_proxy, adapter, DEFAULT_THUNDERBOLT_CFG.regime_lookback_sessions
    )
    try:
        prior_vix = await asyncio.to_thread(adapter.get_ltp, "INDIA VIX")
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch India VIX: %s -- the high-VIX skip will be disabled today.", exc)
        prior_vix = None

    from vectra_quant.orderflow.storage import write_session_context

    write_session_context(RECORDS_ROOT, now_ist().date(), {
        "recent_realized_vols": recent_vols,
        "recent_nifty_daily_candles": recent_candles,
        "prior_session_vix": prior_vix,
        "future_contract": current_future.instrument.trading_symbol,
        "future_expiry": current_future.expiry,
    })

    rec = await trader.run_session(
        session_date=now_ist().date(), recent_realized_vols=recent_vols,
        prior_session_vix=prior_vix, spot_fn=spot_fn,
    )
    log.info("Session finished: skipped=%s reason=%s position=%s", rec.skipped, rec.skip_reason, rec.position)

    recorder.stop()
    await recorder_task


if __name__ == "__main__":
    # A single unhandled exception anywhere in main() -- the depth recorder,
    # the paper trader, an instrument-master refresh -- used to kill this
    # whole process for the rest of the day with nothing watching it (hit
    # live: a real signal fired, a quote lookup failed, and the recorder
    # stayed dead for ~8.5 hours until someone happened to check). Restart
    # rather than exit; `already_has_call_today`'s idempotency check makes a
    # restart mid-day safe, and each recorder restart just opens a new
    # Parquet part-file, so nothing is corrupted by the gap.
    while True:
        try:
            asyncio.run(main())
            break  # main() returned normally (force-exit time reached) -- done for today
        except Exception:
            log.exception("run_thunderbolt_live crashed -- restarting in 10s")
            import time as _time
            _time.sleep(10)

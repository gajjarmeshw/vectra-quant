from __future__ import annotations

import asyncio
import json
from datetime import date, time, timedelta

import pytest

from vectra_quant.core.session_clock import now_ist
from vectra_quant.orderflow.breadth_paper_trader import BreadthPaperTrader
from vectra_quant.orderflow.config import BreadthCfg

FAKE_PRICES = {"NIFTY23300CE": 120.0, "NIFTY23300PE": 90.0}


def fake_resolver(right: str, strike: float) -> str:
    return f"NIFTY{int(strike)}{right}"


def fake_quote(symbol: str) -> float:
    return FAKE_PRICES.get(symbol, 50.0)


def fake_spot() -> float:
    return 23280.0


class TransitioningBookProvider:
    """Flat for the first few polls, then swings strongly bid-heavy --
    crossing detection needs an actual transition through theta, not a
    series that starts already above it (a constant reading never
    "crosses" anything, exactly the real pattern hit live with Thunderbolt)."""

    def __init__(self, flat_calls: int = 3):
        self._flat_calls = flat_calls
        self._calls = 0

    def __call__(self):
        self._calls += 1
        if self._calls <= self._flat_calls:
            return {f"S{i}": (100.0, 100.0) for i in range(25)}
        return {f"S{i}": (900.0, 100.0) for i in range(25)}


def flat_book_provider():
    return {f"S{i}": (100.0, 100.0) for i in range(25)}


@pytest.mark.asyncio
async def test_run_session_enters_on_strong_breadth(tmp_path):
    cfg = BreadthCfg(
        signal_window_start=time(0, 0), signal_window_end=time(23, 59),
        force_exit_time=(now_ist() + timedelta(seconds=2)).time(),
        theta_cross=0.1, min_stocks_reporting=10, bucket_seconds=0.05,
    )
    trader = BreadthPaperTrader(
        top_of_book_providers=[TransitioningBookProvider()], quote_fn=fake_quote,
        instrument_resolver_fn=fake_resolver, spot_fn=fake_spot, cfg=cfg,
        records_root=str(tmp_path),
    )
    rec = await trader.run_session(session_date=date(2026, 9, 17), poll_seconds=0.05)
    assert rec.position is not None
    assert rec.position["direction"] == "BULLISH"
    assert rec.position["leg"]["right"] == "CE"
    assert rec.position["status"] == "CLOSED"
    assert rec.position["exit_reason"] in ("FORCE_EXIT", "STOP_LOSS", "TARGET", "MAX_HOLD_TIME")


@pytest.mark.asyncio
async def test_run_session_no_signal_when_flat(tmp_path):
    cfg = BreadthCfg(
        signal_window_start=time(0, 0), signal_window_end=time(23, 59),
        force_exit_time=(now_ist() + timedelta(seconds=1)).time(),
        theta_cross=0.1, min_stocks_reporting=10, bucket_seconds=0.05,
    )
    trader = BreadthPaperTrader(
        top_of_book_providers=[flat_book_provider], quote_fn=fake_quote,
        instrument_resolver_fn=fake_resolver, spot_fn=fake_spot, cfg=cfg,
        records_root=str(tmp_path),
    )
    rec = await trader.run_session(session_date=date(2026, 9, 17), poll_seconds=0.05)
    assert rec.position is None
    assert rec.skipped is True
    assert rec.skip_reason == "NO_QUALIFYING_CROSSING_BY_WINDOW_CLOSE"


@pytest.mark.asyncio
async def test_run_session_started_after_hours_gets_distinct_skip_reason(tmp_path):
    """Starting the script after today's force_exit_time (e.g. late at
    night) must not claim a full day's evaluation happened -- it never got
    a single chance to look."""
    cfg = BreadthCfg(
        signal_window_start=time(0, 0), signal_window_end=time(23, 59),
        force_exit_time=(now_ist() - timedelta(seconds=1)).time(),  # already in the past
        theta_cross=0.1, min_stocks_reporting=10, bucket_seconds=0.05,
    )
    trader = BreadthPaperTrader(
        top_of_book_providers=[flat_book_provider], quote_fn=fake_quote,
        instrument_resolver_fn=fake_resolver, spot_fn=fake_spot, cfg=cfg,
        records_root=str(tmp_path),
    )
    rec = await trader.run_session(session_date=date(2026, 9, 17), poll_seconds=0.05)
    assert rec.position is None
    assert rec.skipped is True
    assert rec.skip_reason == "NEVER_ENTERED_SIGNAL_WINDOW"


@pytest.mark.asyncio
async def test_already_has_call_today_is_idempotent(tmp_path):
    cfg = BreadthCfg()
    trader1 = BreadthPaperTrader([flat_book_provider], fake_quote, fake_resolver, fake_spot, cfg, str(tmp_path))
    d = date(2026, 9, 17)
    rec = trader1._load_or_init_today(d)
    rec.skipped = True
    rec.skip_reason = "TEST_SKIP"
    trader1._save(rec)

    trader2 = BreadthPaperTrader([flat_book_provider], fake_quote, fake_resolver, fake_spot, cfg, str(tmp_path))
    assert trader2.already_has_call_today(d) is True


@pytest.mark.asyncio
async def test_live_status_written_every_cycle(tmp_path):
    cfg = BreadthCfg(
        signal_window_start=time(0, 0), signal_window_end=time(23, 59),
        force_exit_time=(now_ist() + timedelta(seconds=1)).time(),
        theta_cross=0.1, min_stocks_reporting=10, bucket_seconds=0.05,
    )
    trader = BreadthPaperTrader([flat_book_provider], fake_quote, fake_resolver, fake_spot, cfg, str(tmp_path))
    await trader.run_session(session_date=date(2026, 9, 17), poll_seconds=0.05)

    status_path = tmp_path / "breadth" / "date=2026-09-17" / "live_status.json"
    assert status_path.exists()
    status = json.loads(status_path.read_text())
    assert status["n_stocks_reporting"] == 25
    assert "mean_breadth" in status

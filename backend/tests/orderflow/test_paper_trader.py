from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, time, timedelta

import pytest

from vectra_quant.core.session_clock import now_ist
from vectra_quant.orderflow.aggregator import SessionAggregator
from vectra_quant.orderflow.config import ThunderboltCfg
from vectra_quant.orderflow.paper_trader import ThunderboltPaperTrader

FAKE_PRICES = {
    "NIFTY24700CE": 120.0, "NIFTY24600CE": 150.0,
    "NIFTY24400PE": 90.0, "NIFTY24600PE": 100.0,
}


def fake_resolver(right: str, strike: float) -> str:
    return f"NIFTY{int(strike)}{right}"


def fake_quote(symbol: str) -> float:
    return FAKE_PRICES.get(symbol, 50.0)


def fake_spot() -> float:
    return 24623.0


@pytest.mark.asyncio
async def test_run_session_enters_and_force_exits_a_position(tmp_path):
    cfg = ThunderboltCfg(
        signal_window_start=time(0, 0), signal_window_end=time(23, 59),
        force_exit_time=(now_ist() + timedelta(seconds=1)).time(),
        theta_cross=0.05,
    )
    agg = SessionAggregator(percentile=95, min_samples=10000, interval_seconds=60)
    trader = ThunderboltPaperTrader(
        aggregator=agg, quote_fn=fake_quote, instrument_resolver_fn=fake_resolver,
        cfg=cfg, records_root=str(tmp_path),
    )

    # seed a series that produces a clean bullish crossing on evaluation
    now = now_ist()
    agg._completed_intervals = [
        (now - timedelta(minutes=3), 0.02),
        (now - timedelta(minutes=2), 0.06),
        (now - timedelta(minutes=1), 0.10),
    ]

    rec = await trader.run_session(
        session_date=date(2026, 9, 16),  # a Wednesday, not skipped
        recent_realized_vols=[1.5, 1.6],  # HIGH regime
        prior_session_vix=15.0,
        spot_fn=fake_spot,
        poll_seconds=0.1,
    )

    assert rec.skipped is False
    assert rec.position is not None
    assert rec.position["status"] == "CLOSED"
    assert rec.position["net_pnl"] is not None
    assert len(rec.position["legs"]) == 2


@pytest.mark.asyncio
async def test_quote_failure_on_entry_is_recorded_not_raised(tmp_path):
    """Regression test for a real production incident: a genuine signal
    fired, the entry leg's quote lookup raised (an illiquid strike / Dhan
    API gap), and the exception propagated all the way out of
    `run_session()` uncaught -- killing the entire process (recorder
    included) for ~8.5 hours with nothing watching it. It must instead be
    recorded as a skip and `run_session` must return normally."""
    cfg = ThunderboltCfg(
        signal_window_start=time(0, 0), signal_window_end=time(23, 59),
        force_exit_time=(now_ist() + timedelta(seconds=1)).time(),
        theta_cross=0.05,
    )
    agg = SessionAggregator(percentile=95, min_samples=10000, interval_seconds=60)

    def failing_quote(symbol):
        raise RuntimeError(f"No quote available for {symbol}")

    trader = ThunderboltPaperTrader(
        aggregator=agg, quote_fn=failing_quote, instrument_resolver_fn=fake_resolver,
        cfg=cfg, records_root=str(tmp_path),
    )

    now = now_ist()
    agg._completed_intervals = [
        (now - timedelta(minutes=3), 0.02),
        (now - timedelta(minutes=2), 0.06),
        (now - timedelta(minutes=1), 0.10),
    ]

    rec = await trader.run_session(
        session_date=date(2026, 9, 16), recent_realized_vols=[1.5, 1.6],
        prior_session_vix=15.0, spot_fn=fake_spot, poll_seconds=0.1,
    )

    assert rec.position is None
    assert rec.skipped is True
    assert "COULD_NOT_PRICE_ENTRY" in rec.skip_reason
    assert rec.decision is not None  # the trigger itself is still preserved for audit


@pytest.mark.asyncio
async def test_run_session_skips_tuesday():
    cfg = ThunderboltCfg()
    agg = SessionAggregator(percentile=95, min_samples=10000, interval_seconds=60)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        trader = ThunderboltPaperTrader(
            aggregator=agg, quote_fn=fake_quote, instrument_resolver_fn=fake_resolver,
            cfg=cfg, records_root=tmp,
        )
        tuesday = date(2026, 9, 15)
        assert tuesday.weekday() == 1
        rec = await trader.run_session(
            session_date=tuesday, recent_realized_vols=[1.5], prior_session_vix=15.0,
            spot_fn=fake_spot, poll_seconds=0.1,
        )
        assert rec.skipped is True
        assert rec.skip_reason == "TUESDAY_SKIP"


@pytest.mark.asyncio
async def test_already_has_call_today_is_idempotent_across_instances(tmp_path):
    cfg = ThunderboltCfg()
    agg = SessionAggregator(percentile=95, min_samples=10000, interval_seconds=60)
    trader1 = ThunderboltPaperTrader(agg, fake_quote, fake_resolver, cfg, str(tmp_path))
    d = date(2026, 9, 16)
    trader1._save(trader1._load_or_init_today(d))
    trader1.already_has_call_today  # noqa: B018 -- just referencing, not calling here

    # skip the day manually to simulate a completed decision persisted to disk
    rec = trader1._load_or_init_today(d)
    rec.skipped = True
    rec.skip_reason = "HIGH_VIX_SKIP"
    trader1._save(rec)

    # a fresh trader instance (simulating a process restart) must see the
    # same persisted record and treat today as already handled
    trader2 = ThunderboltPaperTrader(agg, fake_quote, fake_resolver, cfg, str(tmp_path))
    assert trader2.already_has_call_today(d) is True


@pytest.mark.asyncio
async def test_no_qualifying_trigger_marks_skipped_with_reason(tmp_path):
    cfg = ThunderboltCfg(
        signal_window_start=time(0, 0), signal_window_end=time(23, 59),
        force_exit_time=(now_ist() + timedelta(seconds=1)).time(),
        theta_cross=5.0,  # unreachable threshold -> never crosses
    )
    agg = SessionAggregator(percentile=95, min_samples=10000, interval_seconds=60)
    trader = ThunderboltPaperTrader(agg, fake_quote, fake_resolver, cfg, str(tmp_path))

    now = now_ist()
    agg._completed_intervals = [(now - timedelta(minutes=1), 0.02)]

    rec = await trader.run_session(
        session_date=date(2026, 9, 16), recent_realized_vols=[1.5], prior_session_vix=15.0,
        spot_fn=fake_spot, poll_seconds=0.1,
    )
    assert rec.position is None
    assert rec.skipped is True
    assert rec.skip_reason == "NO_QUALIFYING_TRIGGER_BY_WINDOW_CLOSE"

    live_status_path = tmp_path / "thunderbolt" / "date=2026-09-16" / "live_status.json"
    assert live_status_path.exists(), "every poll cycle must persist its trace, not just the final outcome"
    status = json.loads(live_status_path.read_text())
    assert status["trace"]["final"] == "SKIP"
    assert status["prior_session_vix"] == 15.0
    assert status["vix_skip_at_or_above"] == cfg.vix_skip_at_or_above
    assert status["recent_realized_vols"] == [1.5]
    assert status["latest_imbalance_reading"] == 0.02


@pytest.mark.asyncio
async def test_live_status_written_for_a_day_level_skip(tmp_path):
    cfg = ThunderboltCfg(vix_skip_at_or_above=22.0)
    agg = SessionAggregator(percentile=95, min_samples=10000, interval_seconds=60)
    trader = ThunderboltPaperTrader(agg, fake_quote, fake_resolver, cfg, str(tmp_path))

    rec = await trader.run_session(
        session_date=date(2026, 9, 16), recent_realized_vols=[1.5], prior_session_vix=25.0,
        spot_fn=fake_spot, poll_seconds=0.1,
    )
    assert rec.skipped is True
    assert "HIGH_VIX_SKIP" in rec.skip_reason

    status = json.loads((tmp_path / "thunderbolt" / "date=2026-09-16" / "live_status.json").read_text())
    assert "HIGH_VIX_SKIP" in status["day_skip_reason"]
    assert status["prior_session_vix"] == 25.0

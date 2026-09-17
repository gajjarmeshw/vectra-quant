from __future__ import annotations

import asyncio
import json
from datetime import time
from unittest.mock import patch

import pytest

from vectra_quant.brokers.base import Instrument
from vectra_quant.orderflow.config import OrderFlowCfg
from vectra_quant.orderflow.multi_recorder import (
    MAX_INSTRUMENTS_PER_CONNECTION,
    MultiInstrumentDepthRecorder,
)


def make_stock(symbol: str, sid: int) -> Instrument:
    return Instrument(
        trading_symbol=symbol, exchange="NSE", segment="NSE_EQ", lot_size=1,
        instrument_type="EQ", name=symbol, exchange_token=str(sid),
    )


def test_rejects_more_than_50_instruments(tmp_path):
    instruments = [make_stock(f"S{i}", i) for i in range(51)]
    with pytest.raises(ValueError, match="exceeds Dhan's 50-per-connection"):
        MultiInstrumentDepthRecorder(
            dhan_context=object(), instruments=instruments, cfg=OrderFlowCfg(),
            storage_root=str(tmp_path), connection_label="test",
        )


def test_ingest_update_builds_top_of_book_snapshot(tmp_path):
    instruments = [make_stock("RELIANCE", 2885), make_stock("TCS", 11536)]
    recorder = MultiInstrumentDepthRecorder(
        dhan_context=object(), instruments=instruments, cfg=OrderFlowCfg(),
        storage_root=str(tmp_path), connection_label="conn0",
    )
    recorder._ingest_update({
        "security_id": 2885, "type": "Bid",
        "depth": [{"price": 1244.7, "quantity": 300, "orders": 5}, {"price": 1244.6, "quantity": 50, "orders": 2}],
    })
    recorder._ingest_update({
        "security_id": 2885, "type": "Ask",
        "depth": [{"price": 1245.0, "quantity": 100, "orders": 3}],
    })
    snap = recorder.top_of_book_snapshot()
    assert snap["RELIANCE"] == (300, 100)
    assert "TCS" not in snap  # never got an update


def test_ingest_update_ignores_unknown_security_id(tmp_path):
    instruments = [make_stock("RELIANCE", 2885)]
    recorder = MultiInstrumentDepthRecorder(
        dhan_context=object(), instruments=instruments, cfg=OrderFlowCfg(),
        storage_root=str(tmp_path), connection_label="conn0",
    )
    # must not raise -- a broadcast for an instrument we didn't subscribe to
    recorder._ingest_update({"security_id": 999999, "type": "Bid", "depth": [{"price": 1.0, "quantity": 1, "orders": 1}]})
    assert recorder.top_of_book_snapshot() == {}


def test_health_json_written(tmp_path):
    instruments = [make_stock("RELIANCE", 2885)]
    recorder = MultiInstrumentDepthRecorder(
        dhan_context=object(), instruments=instruments, cfg=OrderFlowCfg(),
        storage_root=str(tmp_path), connection_label="conn0",
    )
    recorder._write_health()
    path = tmp_path / "recorder_health" / "health_conn0.json"
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["label"] == "conn0"
    assert payload["n_instruments"] == 1


class FakeFullDepthMulti:
    """Mirrors the real dhanhq.FullDepth per-call-is-one-frame behavior:
    each get_instrument_data() call yields whatever's in `frames.pop(0)`
    then ends normally -- proving run_forever keeps reusing the same
    connection instead of reconnecting on every frame."""

    call_count = 0
    frames: list[dict] = []
    stop_after: MultiInstrumentDepthRecorder | None = None

    def __init__(self, dhan_context, instruments, depth_level=20):
        FakeFullDepthMulti.call_count += 1

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def get_instrument_data(self):
        if not FakeFullDepthMulti.frames:
            if FakeFullDepthMulti.stop_after is not None:
                FakeFullDepthMulti.stop_after.stop()
            return
        yield FakeFullDepthMulti.frames.pop(0)


@pytest.mark.asyncio
async def test_run_forever_reuses_connection_across_frames(tmp_path):
    FakeFullDepthMulti.call_count = 0
    FakeFullDepthMulti.frames = [
        {"security_id": 2885, "type": "Bid", "depth": [{"price": 1244.7, "quantity": 300, "orders": 5}]},
        {"security_id": 2885, "type": "Ask", "depth": [{"price": 1245.0, "quantity": 100, "orders": 3}]},
    ]
    instruments = [make_stock("RELIANCE", 2885)]
    cfg = OrderFlowCfg(recorder_start_time=time(0, 0), recorder_stop_time=time(23, 59))
    recorder = MultiInstrumentDepthRecorder(
        dhan_context=object(), instruments=instruments, cfg=cfg,
        storage_root=str(tmp_path), connection_label="conn0",
    )
    FakeFullDepthMulti.stop_after = recorder

    with patch("dhanhq.FullDepth", FakeFullDepthMulti):
        await asyncio.wait_for(recorder.run_forever(), timeout=5.0)

    assert FakeFullDepthMulti.call_count == 1, "must reuse the same connection across per-frame batches"
    assert recorder.health.reconnect_count == 0
    assert recorder.top_of_book_snapshot()["RELIANCE"] == (300, 100)


# ------------------------------------------------- snapshot freshness/validity
#
# `top_of_book_snapshot` fed the breadth signal the last value ever seen for a
# symbol, with no expiry. A stock that stopped quoting -- or a whole websocket
# connection that died -- kept contributing its frozen imbalance to the
# cross-sectional mean and kept counting toward `n_stocks`, so breadth could
# report "100 stocks reporting" off a dead feed. Same failure as the
# recorder-health panel calling a twelve-hour-old file live.

from datetime import timedelta

from vectra_quant.core.session_clock import now_ist


def _recorder(tmp_path, *symbols):
    return MultiInstrumentDepthRecorder(
        dhan_context=object(),
        instruments=[make_stock(s, 1000 + i) for i, s in enumerate(symbols)],
        cfg=OrderFlowCfg(), storage_root=str(tmp_path), connection_label="conn0",
    )


def _quote(recorder, sid, side, price, qty):
    recorder._ingest_update(
        {"security_id": sid, "type": side, "depth": [{"price": price, "quantity": qty, "orders": 1}]}
    )


def test_snapshot_drops_symbols_that_have_gone_quiet(tmp_path):
    rec = _recorder(tmp_path, "FRESH", "STALE")
    for sid in (1000, 1001):
        _quote(rec, sid, "Bid", 100.0, 300)
        _quote(rec, sid, "Ask", 100.5, 100)

    assert set(rec.top_of_book_snapshot()) == {"FRESH", "STALE"}

    # STALE last updated two minutes ago; FRESH a second ago.
    rec._top_of_book["STALE"]["ts"] = now_ist() - timedelta(seconds=120)

    fresh_only = rec.top_of_book_snapshot(max_age_s=30.0)
    assert set(fresh_only) == {"FRESH"}
    # and it must not merely be zeroed -- it must not be counted at all
    assert "STALE" not in fresh_only

    # A generous window still includes it, so the bound is the only thing doing the work.
    assert set(rec.top_of_book_snapshot(max_age_s=600.0)) == {"FRESH", "STALE"}


def test_a_dead_connection_empties_the_snapshot_rather_than_freezing_it(tmp_path):
    rec = _recorder(tmp_path, "A", "B", "C")
    for sid in (1000, 1001, 1002):
        _quote(rec, sid, "Bid", 100.0, 300)
        _quote(rec, sid, "Ask", 100.5, 100)
    assert len(rec.top_of_book_snapshot()) == 3

    for v in rec._top_of_book.values():
        v["ts"] = now_ist() - timedelta(hours=12)
    assert rec.top_of_book_snapshot() == {}


def test_torn_down_books_priced_at_zero_are_not_taken_as_a_lean(tmp_path):
    """~6% of top-of-book rows in the one real multi-stock capture carry
    price 0 with a live-looking quantity. Breadth reads quantities only, so
    without this guard it would take them for genuine one-sided interest."""
    rec = _recorder(tmp_path, "RELIANCE")
    _quote(rec, 1000, "Bid", 1244.7, 300)
    _quote(rec, 1000, "Ask", 1245.0, 100)
    assert rec.top_of_book_snapshot()["RELIANCE"] == (300, 100)

    # An empty book arrives: price 0, quantity still populated.
    _quote(rec, 1000, "Ask", 0.0, 9999)
    assert rec.top_of_book_snapshot()["RELIANCE"] == (300, 100)

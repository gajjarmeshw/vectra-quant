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

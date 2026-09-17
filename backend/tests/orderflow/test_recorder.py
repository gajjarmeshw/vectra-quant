"""Integration-style test for `DepthRecorder.run_forever`'s reconnect/backoff
loop -- previously the one async orchestration loop in orderflow/ with zero
test coverage (unlike `paper_trader.py`'s `run_session`, already covered).

Mocks `dhanhq.FullDepth` (the vendor SDK class) rather than re-testing the
real WebSocket handshake -- that's exactly the boundary `recorder.py` itself
draws (SDK owns the connection, this module owns reconnect policy +
aggregation + persistence).
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, time, timedelta
from unittest.mock import patch

import pytest

from vectra_quant.brokers.base import Instrument
from vectra_quant.orderflow.config import OrderFlowCfg
from vectra_quant.orderflow.recorder import DepthRecorder


def make_future_instrument() -> Instrument:
    expiry = (date.today() + timedelta(days=20)).isoformat()
    return Instrument(
        trading_symbol="NIFTY-TESTFUT", exchange="NSE", segment="FUTIDX",
        lot_size=65, instrument_type="FUT", name="NIFTY", expiry=expiry,
        exchange_token="99999",
    )


class FakeFullDepth:
    """Simulates: first connection's stream dies mid-way (forcing a
    reconnect), second connection delivers a couple of updates, then the
    test's own callback stops the recorder."""

    call_count = 0
    stop_after: DepthRecorder | None = None

    def __init__(self, dhan_context, instruments, depth_level=200):
        self.dhan_context = dhan_context
        self.instruments = instruments
        self.depth_level = depth_level
        FakeFullDepth.call_count += 1
        self.attempt = FakeFullDepth.call_count

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def get_instrument_data(self):
        if self.attempt == 1:
            # look like real data arrived, then the connection dies
            yield {"security_id": 99999, "type": "Bid", "depth": [{"price": 100.0, "quantity": 10, "orders": 1}]}
            raise ConnectionError("simulated disconnect")
        else:
            yield {"security_id": 99999, "type": "Ask", "depth": [{"price": 101.0, "quantity": 20, "orders": 2}]}
            if FakeFullDepth.stop_after is not None:
                FakeFullDepth.stop_after.stop()
            # end the generator so run_forever loops back and sees `_stop`
            return


@pytest.mark.asyncio
async def test_run_forever_reconnects_after_a_disconnect(tmp_path):
    FakeFullDepth.call_count = 0
    cfg = OrderFlowCfg(
        recorder_start_time=time(0, 0), recorder_stop_time=time(23, 59),
        reconnect_backoff_seconds=(0.01, 0.01),
    )
    instruments = [make_future_instrument()]
    recorder = DepthRecorder(
        dhan_context=object(),
        instrument_master_provider=lambda: instruments,
        cfg=cfg,
        storage_root=str(tmp_path),
    )
    FakeFullDepth.stop_after = recorder

    with patch("dhanhq.FullDepth", FakeFullDepth):
        await asyncio.wait_for(recorder.run_forever(), timeout=5.0)

    assert FakeFullDepth.call_count == 2, "should have reconnected exactly once after the simulated disconnect"
    assert recorder.health.reconnect_count == 1
    assert recorder.health.connected is True
    # both the pre-disconnect and post-reconnect updates should have reached the aggregator
    assert recorder.aggregator.chart2_series() != [] or recorder.aggregator.raw_imbalance_series() != []

    health_path = tmp_path / "recorder_health" / "health.json"
    assert health_path.exists(), "health.json must be written so a separate process (the API) can read connection state"
    written = json.loads(health_path.read_text())
    assert written["reconnect_count"] == 1
    assert written["connected"] is True
    assert written["contract"] == "NIFTY-TESTFUT"


class FakeFullDepthPerFrame:
    """Simulates the REAL `dhanhq.FullDepth.get_instrument_data()` shape:
    each call is exactly one `ws.recv()` -- it yields the messages packed
    into ONE frame, then ends normally, with no exception. A caller that
    (wrongly) treats that per-call ending as a lost connection tears down
    and reconnects on every single frame; this fake exists to prove
    `run_forever` no longer does that -- it keeps calling
    `get_instrument_data()` again on the SAME instance instead."""

    call_count = 0
    frames: list[dict] = []
    stop_after: DepthRecorder | None = None

    def __init__(self, dhan_context, instruments, depth_level=200):
        FakeFullDepthPerFrame.call_count += 1

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def get_instrument_data(self):
        if not FakeFullDepthPerFrame.frames:
            if FakeFullDepthPerFrame.stop_after is not None:
                FakeFullDepthPerFrame.stop_after.stop()
            return
        yield FakeFullDepthPerFrame.frames.pop(0)


@pytest.mark.asyncio
async def test_run_forever_does_not_reconnect_on_normal_frame_exhaustion(tmp_path):
    """Regression test for the real bug: `get_instrument_data()` ending
    normally after one frame is NOT a disconnect. Two frames (one bid, one
    ask) arriving as separate per-call batches must both reach the
    aggregator without `FullDepth` ever being reconstructed or
    `reconnect_count` incrementing."""
    FakeFullDepthPerFrame.call_count = 0
    FakeFullDepthPerFrame.frames = [
        {"security_id": 99999, "type": "Bid", "depth": [{"price": 100.0, "quantity": 10, "orders": 1}]},
        {"security_id": 99999, "type": "Ask", "depth": [{"price": 101.0, "quantity": 20, "orders": 2}]},
    ]
    cfg = OrderFlowCfg(recorder_start_time=time(0, 0), recorder_stop_time=time(23, 59))
    instruments = [make_future_instrument()]
    recorder = DepthRecorder(
        dhan_context=object(), instrument_master_provider=lambda: instruments,
        cfg=cfg, storage_root=str(tmp_path),
    )
    FakeFullDepthPerFrame.stop_after = recorder

    with patch("dhanhq.FullDepth", FakeFullDepthPerFrame):
        await asyncio.wait_for(recorder.run_forever(), timeout=5.0)

    assert FakeFullDepthPerFrame.call_count == 1, "must reuse the same connection across per-frame batches"
    assert recorder.health.reconnect_count == 0
    series = recorder.aggregator.raw_imbalance_series()
    assert series != [], "both the bid and ask frames must have reached the aggregator"


@pytest.mark.asyncio
async def test_run_forever_sleeps_outside_session_window(tmp_path):
    """Outside the configured recording window, the loop must not attempt a
    connection at all -- it should just wait and re-check. The real 30s
    poll interval is patched to instant so this test doesn't take 30s."""
    cfg = OrderFlowCfg(
        recorder_start_time=time(1, 0), recorder_stop_time=time(1, 1),  # a window almost certainly not "now"
    )
    instruments = [make_future_instrument()]
    recorder = DepthRecorder(
        dhan_context=object(), instrument_master_provider=lambda: instruments,
        cfg=cfg, storage_root=str(tmp_path),
    )

    real_sleep = asyncio.sleep
    call_count = 0

    async def fast_sleep(_seconds):
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            recorder.stop()
        await real_sleep(0)

    with patch("dhanhq.FullDepth", FakeFullDepth), patch("vectra_quant.orderflow.recorder.asyncio.sleep", fast_sleep):
        await asyncio.wait_for(recorder.run_forever(), timeout=5.0)

    assert recorder.health.connected is False
    assert call_count >= 3  # looped on the outside-session sleep repeatedly, never attempting a connection

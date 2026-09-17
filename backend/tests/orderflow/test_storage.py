from __future__ import annotations

import json
from datetime import date, datetime

import pyarrow.parquet as pq

from vectra_quant.orderflow.storage import DepthParquetWriter, DepthTickRow, write_session_context


def test_flush_writes_partitioned_parquet(tmp_path):
    writer = DepthParquetWriter(root=tmp_path, flush_every=1000)
    writer.add(DepthTickRow(datetime(2026, 9, 16, 9, 20), security_id=1234, side="BID", level_idx=0, price=24500.0, qty=65, orders=3))
    writer.add(DepthTickRow(datetime(2026, 9, 16, 9, 20), security_id=1234, side="ASK", level_idx=0, price=24505.0, qty=40, orders=2))
    writer.add(DepthTickRow(datetime(2026, 9, 17, 9, 20), security_id=1234, side="BID", level_idx=0, price=24600.0, qty=70, orders=4))
    path = writer.close()

    assert path is not None
    day1_dir = tmp_path / "depth" / "date=2026-09-16" / "security_id=1234"
    day2_dir = tmp_path / "depth" / "date=2026-09-17" / "security_id=1234"
    assert day1_dir.exists()
    assert day2_dir.exists()

    table1 = pq.read_table(day1_dir / "part-00000.parquet")
    assert table1.num_rows == 2
    assert set(table1.column("side").to_pylist()) == {"BID", "ASK"}

    table2 = pq.read_table(day2_dir / "part-00000.parquet")
    assert table2.num_rows == 1


def test_auto_flush_at_threshold(tmp_path):
    writer = DepthParquetWriter(root=tmp_path, flush_every=2)
    writer.add(DepthTickRow(datetime(2026, 9, 16, 9, 20), 1, "BID", 0, 100.0, 10, 1))
    assert writer._buffer  # not yet flushed
    writer.add(DepthTickRow(datetime(2026, 9, 16, 9, 21), 1, "BID", 0, 101.0, 11, 1))
    assert not writer._buffer  # auto-flushed at threshold


def test_multiple_flushes_increment_part_numbers(tmp_path):
    writer = DepthParquetWriter(root=tmp_path, flush_every=1000)
    writer.add(DepthTickRow(datetime(2026, 9, 16, 9, 20), 1, "BID", 0, 100.0, 10, 1))
    writer.flush()
    writer.add(DepthTickRow(datetime(2026, 9, 16, 9, 21), 1, "BID", 0, 101.0, 11, 1))
    writer.flush()

    part_dir = tmp_path / "depth" / "date=2026-09-16" / "security_id=1"
    parts = sorted(p.name for p in part_dir.glob("*.parquet"))
    assert parts == ["part-00000.parquet", "part-00001.parquet"]


def test_write_session_context_round_trips_non_json_native_types(tmp_path):
    path = write_session_context(tmp_path, date(2026, 9, 17), {
        "recent_realized_vols": [0.8, 1.1],
        "prior_session_vix": 14.2,
        "future_expiry": date(2026, 9, 29),  # not JSON-native -- must fall back via default=str
    })

    assert path == tmp_path / "context" / "date=2026-09-17" / "context.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["recent_realized_vols"] == [0.8, 1.1]
    assert loaded["prior_session_vix"] == 14.2
    assert loaded["future_expiry"] == "2026-09-29"

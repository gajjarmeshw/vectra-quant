"""Synthetic-packet tests for the depth binary parser.

These test the parser against packets WE construct with the same assumed
layout (`depth_parser.HEADER_STRUCT` / `LEVEL_STRUCT`) -- they prove the
parser is internally consistent, NOT that the assumed layout matches
Dhan's real wire format. That can only be confirmed against a live capture
(see the module docstring in depth_parser.py for the exact assumptions to
verify first).
"""
from __future__ import annotations

import pytest

from vectra_quant.orderflow.depth_parser import (
    FEED_CODE_ASK,
    FEED_CODE_BID,
    HEADER_STRUCT,
    LEVEL_STRUCT,
    DepthParseError,
    parse_depth_frame,
    parse_one_packet,
)


def build_packet(feed_code: int, segment: int, security_id: int, row_count: int,
                  levels: list[tuple[float, int, int]]) -> bytes:
    payload = b"".join(LEVEL_STRUCT.pack(price, qty, orders) for (price, qty, orders) in levels)
    total_len = HEADER_STRUCT.size + len(payload)  # msg_length includes the 12-byte header
    header = HEADER_STRUCT.pack(total_len, feed_code, segment, security_id, row_count)
    return header + payload


def test_round_trip_single_bid_packet():
    levels = [(24500.0, 65, 3), (24450.0, 130, 5)]
    buf = build_packet(FEED_CODE_BID, segment=2, security_id=1234, row_count=2, levels=levels)

    packet, offset = parse_one_packet(buf, 0)

    assert offset == len(buf)
    assert packet.side == "BID"
    assert packet.segment == 2
    assert packet.security_id == 1234
    assert packet.row_count == 2
    assert len(packet.levels) == 2
    assert packet.levels[0].price == pytest.approx(24500.0)
    assert packet.levels[0].qty == 65
    assert packet.levels[0].orders == 3
    assert packet.levels[1].price == pytest.approx(24450.0)


def test_ask_side_decoded_correctly():
    buf = build_packet(FEED_CODE_ASK, segment=2, security_id=1234, row_count=1, levels=[(24600.0, 40, 1)])
    packet, _ = parse_one_packet(buf, 0)
    assert packet.side == "ASK"


def test_unknown_feed_code_raises_on_side_access():
    buf = build_packet(99, segment=2, security_id=1234, row_count=1, levels=[(24600.0, 40, 1)])
    packet, _ = parse_one_packet(buf, 0)
    with pytest.raises(DepthParseError):
        _ = packet.side


def test_multiple_packets_stacked_in_one_frame():
    """The spec: 'several are stacked in one message; split by length.'"""
    p1 = build_packet(FEED_CODE_BID, segment=2, security_id=1111, row_count=1, levels=[(100.0, 10, 1)])
    p2 = build_packet(FEED_CODE_ASK, segment=2, security_id=1111, row_count=1, levels=[(101.0, 20, 2)])
    p3 = build_packet(FEED_CODE_BID, segment=2, security_id=1111, row_count=3, levels=[(99.0, 5, 1), (98.0, 7, 1), (97.0, 9, 1)])
    frame = p1 + p2 + p3

    packets = parse_depth_frame(frame)

    assert len(packets) == 3
    assert [p.side for p in packets] == ["BID", "ASK", "BID"]
    assert len(packets[2].levels) == 3
    assert packets[2].levels[2].price == pytest.approx(97.0)


def test_empty_frame_returns_no_packets():
    assert parse_depth_frame(b"") == []


def test_truncated_header_raises():
    with pytest.raises(DepthParseError):
        parse_one_packet(b"\x00\x01\x02", 0)  # only 3 bytes, header needs 12


def test_truncated_body_raises():
    # msg_length claims header(12) + 2 levels(32) = 44 bytes total, but only
    # the header + 1 level (28 bytes) are actually present.
    header = HEADER_STRUCT.pack(44, FEED_CODE_BID, 2, 1234, 2)
    one_level = LEVEL_STRUCT.pack(100.0, 10, 1)
    with pytest.raises(DepthParseError):
        parse_one_packet(header + one_level, 0)


def test_row_count_caps_levels_even_with_extra_trailing_bytes():
    # payload has room for 2 levels but row_count says only 1 -- the SDK
    # trusts row_count, so the second level's bytes are ignored, not an error.
    payload = LEVEL_STRUCT.pack(100.0, 10, 1) + LEVEL_STRUCT.pack(200.0, 20, 2)
    header = HEADER_STRUCT.pack(HEADER_STRUCT.size + len(payload), FEED_CODE_BID, 2, 1234, 1)
    packet, offset = parse_one_packet(header + payload, 0)
    assert len(packet.levels) == 1
    assert packet.levels[0].price == pytest.approx(100.0)
    assert offset == len(header + payload)

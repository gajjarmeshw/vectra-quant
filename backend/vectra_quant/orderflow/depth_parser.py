"""Binary parser for Dhan's 200-level full-market-depth WebSocket feed.

VERIFIED against the official `dhanhq` v2.2.0 Python SDK source
(`dhanhq.fulldepth.FullDepth.process_200_depth_data*`), installed locally —
not just the prose spec:

  - BYTE_ORDER = "<" (little-endian): matches the SDK's `struct.unpack('<hBBiI', ...)`.
  - HEADER_STRUCT = "<hBBiI" (12 bytes): int16 length, uint8 feed code,
    uint8 segment, int32 security id, uint32 row-count/sequence — identical
    to the SDK.
  - LEVEL_STRUCT = "<dII" (16 bytes): float64 price, uint32 qty, uint32
    orders — identical to the SDK's `packet_format`.
  - FEED_CODE_BID / FEED_CODE_ASK = 41 / 51 — matches the SDK's `msg_code`.
  - HEADER_LENGTH_INCLUDES_SELF = True: the SDK slices
    `data[offset:offset+msg_length]` for the WHOLE message (header + body)
    starting at `offset`, then parses the body from byte 12 of THAT slice —
    so `msg_length` counts the 12-byte header too.
  - Level count: the SDK trusts `no_of_rows` (our `row_count`) to decide how
    many levels to read, capped at 200, rather than deriving it purely from
    payload length — this parser does the same for parity.

Still NOT independently verified (the SDK source doesn't settle these,
only a live capture can): whether Dhan's live traffic ever sends a
`row_count` that disagrees with the payload's actual byte length (e.g.
padding), and real endianness/format stability across future API versions.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

BYTE_ORDER = "<"  # UNVERIFIED — flip to ">" if parsed values look wrong
HEADER_STRUCT = struct.Struct(BYTE_ORDER + "hBBiI")  # len(int16), feed_code(u8), segment(u8), security_id(i32), row_count(u32)
LEVEL_STRUCT = struct.Struct(BYTE_ORDER + "dII")     # price(f64), qty(u32), orders(u32)
HEADER_LENGTH_BYTES = HEADER_STRUCT.size  # 12
LEVEL_LENGTH_BYTES = LEVEL_STRUCT.size    # 16

FEED_CODE_BID = 41
FEED_CODE_ASK = 51

# Confirmed against the dhanhq SDK source: msg_length counts the full
# message including the 12-byte header (see module docstring).
HEADER_LENGTH_INCLUDES_SELF = True


class DepthParseError(ValueError):
    """Raised on a malformed or truncated depth frame."""


@dataclass(frozen=True)
class DepthLevel:
    price: float
    qty: int
    orders: int


@dataclass(frozen=True)
class DepthPacket:
    feed_code: int          # FEED_CODE_BID or FEED_CODE_ASK
    segment: int
    security_id: int
    row_count: int          # row count (200-level) / sequence number (20-level) per the spec
    levels: tuple[DepthLevel, ...]
    received_at_wall: float = field(default_factory=time.time)
    received_at_monotonic: float = field(default_factory=time.monotonic)

    @property
    def side(self) -> str:
        if self.feed_code == FEED_CODE_BID:
            return "BID"
        if self.feed_code == FEED_CODE_ASK:
            return "ASK"
        raise DepthParseError(f"unknown feed_code {self.feed_code}")


MAX_LEVELS_PER_PACKET = 200  # matches the SDK's safety cap


def parse_one_packet(buf: bytes, offset: int) -> tuple[DepthPacket, int]:
    """Parse one packet starting at `offset`. Returns (packet, next_offset)."""
    if offset + HEADER_LENGTH_BYTES > len(buf):
        raise DepthParseError(f"truncated header at offset {offset} (buf len {len(buf)})")

    msg_len, feed_code, segment, security_id, row_count = HEADER_STRUCT.unpack_from(buf, offset)

    if HEADER_LENGTH_INCLUDES_SELF:
        payload_len = msg_len - HEADER_LENGTH_BYTES
    else:
        payload_len = msg_len

    if payload_len < 0:
        raise DepthParseError(
            f"negative payload_len={payload_len} (msg_len={msg_len}) -- "
            f"HEADER_LENGTH_INCLUDES_SELF or BYTE_ORDER assumption is likely wrong"
        )

    body_start = offset + HEADER_LENGTH_BYTES
    body_end = offset + msg_len
    if body_end > len(buf):
        raise DepthParseError(f"truncated body: need {body_end} bytes, have {len(buf)}")

    # Trust `row_count` for how many levels to read (capped at 200), same as
    # the reference SDK -- payload_len may include trailing bytes beyond the
    # declared rows.
    n_levels = min(row_count, MAX_LEVELS_PER_PACKET, payload_len // LEVEL_LENGTH_BYTES)
    levels_end = body_start + n_levels * LEVEL_LENGTH_BYTES
    levels = tuple(
        DepthLevel(price=p, qty=q, orders=o)
        for (p, q, o) in LEVEL_STRUCT.iter_unpack(buf[body_start:levels_end])
    )

    packet = DepthPacket(
        feed_code=feed_code, segment=segment, security_id=security_id,
        row_count=row_count, levels=levels,
    )
    return packet, body_end


def parse_depth_frame(buf: bytes) -> list[DepthPacket]:
    """Split one WebSocket binary frame into its stacked depth packets.

    Multiple bid/ask packets can arrive concatenated in a single frame; each
    is parsed independently and the loop advances by each packet's own
    declared length until the buffer is exhausted.
    """
    packets: list[DepthPacket] = []
    offset = 0
    while offset < len(buf):
        packet, offset = parse_one_packet(buf, offset)
        packets.append(packet)
    return packets

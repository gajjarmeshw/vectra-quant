"""Option chain service: snapshots, OI diffing, LLM prompt formatting."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sentinel.data.instruments import InstrumentService
from sentinel.db import ChainSnapshot, session
from sentinel.logging_setup import get

log = get("data.chain")


@dataclass
class StrikeData:
    strike: float
    ce_ltp: float = 0.0
    ce_oi: float = 0.0
    ce_volume: float = 0.0
    pe_ltp: float = 0.0
    pe_oi: float = 0.0
    pe_volume: float = 0.0


class ChainService:
    def __init__(
        self,
        broker: Any,
        instruments: InstrumentService,
        depth: int = 5,
        oi_lookback_min: int = 30,
    ):
        self.broker = broker
        self.instruments = instruments
        self.depth = depth
        self.oi_lookback_min = oi_lookback_min

        self._last_snapshot_time: dict[str, float] = {}
        self._snapshots: dict[str, list[StrikeData]] = {}

    def is_fresh(self, name: str, max_age_s: float = 90.0) -> bool:
        t = self._last_snapshot_time.get(name, 0.0)
        return (time.time() - t) <= max_age_s

    def snapshot(self, name: str, spot_price: float) -> list[StrikeData]:
        if spot_price <= 0:
            return []

        expiry = self.instruments.current_expiry(name)
        strikes = self.instruments.master.strikes(name, expiry)
        if not strikes:
            return []

        atm = min(strikes, key=lambda s: abs(s - spot_price))
        atm_idx = strikes.index(atm)

        start = max(0, atm_idx - self.depth)
        end = min(len(strikes), atm_idx + self.depth + 1)
        target_strikes = strikes[start:end]

        chain_rows: list[StrikeData] = []
        now_dt = datetime.now(UTC)

        with session() as s:
            for strike in target_strikes:
                ce = self.instruments.resolve_strike(name, expiry, strike, "CE")
                pe = self.instruments.resolve_strike(name, expiry, strike, "PE")

                ce_ltp = self.broker.get_ltp(ce.trading_symbol) if ce else 0.0
                pe_ltp = self.broker.get_ltp(pe.trading_symbol) if pe else 0.0

                sd = StrikeData(strike=strike, ce_ltp=ce_ltp, pe_ltp=pe_ltp)
                chain_rows.append(sd)

                if ce:
                    s.add(ChainSnapshot(
                        ts=now_dt,
                        instrument=name,
                        expiry=expiry,
                        strike=strike,
                        side="CE",
                        ltp=ce_ltp,
                    ))
                if pe:
                    s.add(ChainSnapshot(
                        ts=now_dt,
                        instrument=name,
                        expiry=expiry,
                        strike=strike,
                        side="PE",
                        ltp=pe_ltp,
                    ))

        self._snapshots[name] = chain_rows
        self._last_snapshot_time[name] = time.time()
        return chain_rows

    def big_oi_shifts(self, name: str, threshold_pct: float = 20.0) -> list[dict[str, Any]]:
        return []

    def compact_for_llm(self, name: str) -> list[dict[str, Any]]:
        rows = self._snapshots.get(name, [])
        return [
            {
                "strike": r.strike,
                "ce_ltp": r.ce_ltp,
                "pe_ltp": r.pe_ltp,
            }
            for r in rows
        ]

    def purge_old(self) -> None:
        pass

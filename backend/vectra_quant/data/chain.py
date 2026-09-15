from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vectra_quant.data.instruments import InstrumentService
from vectra_quant.data.regimes import (
    OptionWalls,
    PriceOIRegime,
    compute_option_walls,
    compute_price_oi_regime,
)
from vectra_quant.db import ChainSnapshot, session
from vectra_quant.logging_setup import get

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
        self._walls: dict[str, OptionWalls] = {}
        self._regimes: dict[str, PriceOIRegime] = {}

    def is_fresh(self, name: str, max_age_s: float = 90.0) -> bool:
        t = self._last_snapshot_time.get(name, 0.0)
        return (time.time() - t) <= max_age_s

    def get_walls(self, name: str) -> OptionWalls:
        return self._walls.get(name) or OptionWalls()

    def get_regime(self, name: str) -> PriceOIRegime:
        return self._regimes.get(name, PriceOIRegime.NEUTRAL)

    def snapshot(self, name: str, spot_price: float, spot_change_pct: float = 0.0) -> list[StrikeData]:
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

        # Collect symbols to query
        ce_syms: dict[float, str] = {}
        pe_syms: dict[float, str] = {}
        all_syms: list[str] = []

        for strike in target_strikes:
            ce = self.instruments.resolve_strike(name, expiry, strike, "CE")
            pe = self.instruments.resolve_strike(name, expiry, strike, "PE")
            if ce:
                ce_syms[strike] = ce.trading_symbol
                all_syms.append(ce.trading_symbol)
            if pe:
                pe_syms[strike] = pe.trading_symbol
                all_syms.append(pe.trading_symbol)

        quotes: dict[str, Any] = {}
        if hasattr(self.broker, "get_quote") and all_syms:
            try:
                quotes = self.broker.get_quote(all_syms) or {}
            except Exception as exc:
                log.warning("batch get_quote failed in chain snapshot, falling back to get_ltp", extra={"err": str(exc)})

        with session() as s:
            for strike in target_strikes:
                ce_sym = ce_syms.get(strike)
                pe_sym = pe_syms.get(strike)

                ce_quote = quotes.get(ce_sym) if ce_sym else None
                pe_quote = quotes.get(pe_sym) if pe_sym else None

                if ce_quote is not None:
                    ce_ltp = getattr(ce_quote, "last_price", 0.0)
                    ce_oi = getattr(ce_quote, "open_interest", 0.0)
                    ce_vol = getattr(ce_quote, "volume", 0.0)
                else:
                    ce_ltp = self.broker.get_ltp(ce_sym) if ce_sym else 0.0
                    ce_oi = 0.0
                    ce_vol = 0.0

                if pe_quote is not None:
                    pe_ltp = getattr(pe_quote, "last_price", 0.0)
                    pe_oi = getattr(pe_quote, "open_interest", 0.0)
                    pe_vol = getattr(pe_quote, "volume", 0.0)
                else:
                    pe_ltp = self.broker.get_ltp(pe_sym) if pe_sym else 0.0
                    pe_oi = 0.0
                    pe_vol = 0.0

                sd = StrikeData(
                    strike=strike,
                    ce_ltp=ce_ltp,
                    ce_oi=ce_oi,
                    ce_volume=ce_vol,
                    pe_ltp=pe_ltp,
                    pe_oi=pe_oi,
                    pe_volume=pe_vol,
                )
                chain_rows.append(sd)

                if ce_sym:
                    s.add(ChainSnapshot(
                        ts=now_dt,
                        instrument=name,
                        expiry=expiry,
                        strike=strike,
                        side="CE",
                        ltp=ce_ltp,
                        oi=ce_oi,
                        volume=ce_vol,
                    ))
                if pe_sym:
                    s.add(ChainSnapshot(
                        ts=now_dt,
                        instrument=name,
                        expiry=expiry,
                        strike=strike,
                        side="PE",
                        ltp=pe_ltp,
                        oi=pe_oi,
                        volume=pe_vol,
                    ))

        self._snapshots[name] = chain_rows
        self._last_snapshot_time[name] = time.time()

        # Derive institutional option walls and PCR
        walls = compute_option_walls(chain_rows, spot_price)
        self._walls[name] = walls

        # Derive Price + OI regime if total OI change can be estimated
        prev_walls = self._walls.get(name)
        tot_oi = walls.total_call_oi + walls.total_put_oi
        prev_tot_oi = (prev_walls.total_call_oi + prev_walls.total_put_oi) if prev_walls else 0.0
        oi_change_pct = ((tot_oi - prev_tot_oi) / prev_tot_oi * 100.0) if prev_tot_oi > 0 else 0.0

        self._regimes[name] = compute_price_oi_regime(spot_change_pct, oi_change_pct)
        return chain_rows

    def big_oi_shifts(self, name: str, threshold_pct: float = 20.0) -> list[dict[str, Any]]:
        return []

    def compact_for_llm(self, name: str) -> list[dict[str, Any]]:
        rows = self._snapshots.get(name, [])
        return [
            {
                "strike": r.strike,
                "ce_ltp": r.ce_ltp,
                "ce_oi": r.ce_oi,
                "pe_ltp": r.pe_ltp,
                "pe_oi": r.pe_oi,
            }
            for r in rows
        ]

    def get_institutional_context(self, name: str) -> dict[str, Any]:
        walls = self.get_walls(name)
        regime = self.get_regime(name)
        return {
            "regime": regime.value,
            "call_wall": walls.call_wall,
            "put_wall": walls.put_wall,
            "expected_range": list(walls.expected_range),
            "pcr": walls.pcr,
            "pcr_sentiment": walls.pcr_sentiment,
            "dist_call_wall_pct": walls.dist_call_wall_pct,
            "dist_put_wall_pct": walls.dist_put_wall_pct,
        }

    def purge_old(self) -> None:
        pass

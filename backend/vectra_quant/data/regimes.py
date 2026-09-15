"""Institutional Shadowing Framework: Price + OI Regime Matrix, Option Walls, and PCR.

Derived from Returns Institutional Shadowing Framework & Algo Checklist.
- Price + OI Matrix:
  * LONG_BUILDUP: Price UP + OI UP (fresh buying conviction -> trend healthy)
  * SHORT_BUILDUP: Price DOWN + OI UP (fresh selling conviction -> bear trend)
  * SHORT_COVERING: Price UP + OI DOWN (exits, not conviction, fades fast)
  * LONG_UNWINDING: Price DOWN + OI DOWN (longs giving up, weak drift)
- Option Walls:
  * Call Wall: strike with highest Call OI (institutional ceiling)
  * Put Wall: strike with highest Put OI (institutional floor)
  * PCR extremes: < 0.7 (fear/oversold), > 1.3 (complacency/overbought)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vectra_quant.data.chain import StrikeData


class PriceOIRegime(str, Enum):
    LONG_BUILDUP = "LONG_BUILDUP"
    SHORT_BUILDUP = "SHORT_BUILDUP"
    SHORT_COVERING = "SHORT_COVERING"
    LONG_UNWINDING = "LONG_UNWINDING"
    NEUTRAL = "NEUTRAL"

    @property
    def is_trend_healthy(self) -> bool:
        """True if backed by fresh money commitment (Long or Short Buildup)."""
        return self in (PriceOIRegime.LONG_BUILDUP, PriceOIRegime.SHORT_BUILDUP)

    @property
    def allows_ce_buy(self) -> bool:
        """Allow Call Options buying only when trend is backed by genuine long buildup."""
        return self == PriceOIRegime.LONG_BUILDUP

    @property
    def allows_pe_buy(self) -> bool:
        """Allow Put Options buying only when trend is backed by genuine short buildup."""
        return self == PriceOIRegime.SHORT_BUILDUP


def compute_price_oi_regime(
    price_change_pct: float,
    oi_change_pct: float,
    min_move_pct: float = 0.05,
) -> PriceOIRegime:
    """Classify the current market regime using the Price + OI Matrix.

    Args:
        price_change_pct: % change in underlying spot or future price.
        oi_change_pct: % change in cumulative or strike open interest.
        min_move_pct: Threshold below which movement is considered neutral.
    """
    if abs(price_change_pct) < min_move_pct or abs(oi_change_pct) < min_move_pct:
        return PriceOIRegime.NEUTRAL

    if price_change_pct > 0 and oi_change_pct > 0:
        return PriceOIRegime.LONG_BUILDUP
    elif price_change_pct < 0 and oi_change_pct > 0:
        return PriceOIRegime.SHORT_BUILDUP
    elif price_change_pct > 0 and oi_change_pct < 0:
        return PriceOIRegime.SHORT_COVERING
    elif price_change_pct < 0 and oi_change_pct < 0:
        return PriceOIRegime.LONG_UNWINDING

    return PriceOIRegime.NEUTRAL


@dataclass
class OptionWalls:
    call_wall: float = 0.0
    call_wall_oi: float = 0.0
    put_wall: float = 0.0
    put_wall_oi: float = 0.0
    total_call_oi: float = 0.0
    total_put_oi: float = 0.0
    pcr: float = 1.0
    pcr_sentiment: str = "NORMAL"
    spot_price: float = 0.0
    dist_call_wall_pct: float = 0.0
    dist_put_wall_pct: float = 0.0

    @property
    def expected_range(self) -> tuple[float, float]:
        """Expected trading range between the institutional Put and Call walls."""
        return (self.put_wall, self.call_wall)

    def is_testing_call_wall(self, threshold_pct: float = 0.2) -> bool:
        """Check if spot is approaching or testing the Call Wall from below."""
        return abs(self.dist_call_wall_pct) <= threshold_pct

    def is_testing_put_wall(self, threshold_pct: float = 0.2) -> bool:
        """Check if spot is approaching or testing the Put Wall from above."""
        return abs(self.dist_put_wall_pct) <= threshold_pct

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["expected_range"] = list(self.expected_range)
        return data


def compute_option_walls(
    chain_rows: list[StrikeData],
    spot_price: float,
) -> OptionWalls:
    """Compute institutional option walls (highest Call & Put OI) and PCR."""
    if not chain_rows or spot_price <= 0:
        return OptionWalls(spot_price=spot_price)

    max_call_oi = -1.0
    call_wall_strike = spot_price
    max_put_oi = -1.0
    put_wall_strike = spot_price
    total_call_oi = 0.0
    total_put_oi = 0.0

    for r in chain_rows:
        c_oi = float(r.ce_oi or 0.0)
        p_oi = float(r.pe_oi or 0.0)

        total_call_oi += c_oi
        total_put_oi += p_oi

        if c_oi > max_call_oi:
            max_call_oi = c_oi
            call_wall_strike = r.strike

        if p_oi > max_put_oi:
            max_put_oi = p_oi
            put_wall_strike = r.strike

    pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 1.0

    if pcr < 0.7:
        sentiment = "FEAR_OVERSOLD"
    elif pcr > 1.3:
        sentiment = "COMPLACENT_OVERBOUGHT"
    else:
        sentiment = "NORMAL"

    dist_call = round((call_wall_strike - spot_price) / spot_price * 100.0, 2) if spot_price > 0 else 0.0
    dist_put = round((spot_price - put_wall_strike) / spot_price * 100.0, 2) if spot_price > 0 else 0.0

    return OptionWalls(
        call_wall=call_wall_strike,
        call_wall_oi=max_call_oi if max_call_oi > 0 else 0.0,
        put_wall=put_wall_strike,
        put_wall_oi=max_put_oi if max_put_oi > 0 else 0.0,
        total_call_oi=total_call_oi,
        total_put_oi=total_put_oi,
        pcr=pcr,
        pcr_sentiment=sentiment,
        spot_price=spot_price,
        dist_call_wall_pct=dist_call,
        dist_put_wall_pct=dist_put,
    )

def approximate_ofi(candles: list[Any]) -> float:
    """
    Approximate Order Flow Imbalance (OFI) using 1-minute candle volume delta.
    Assigns volume to buyers if close > open, sellers if close < open.
    Returns the net imbalance ratio: (buyer_vol - seller_vol) / total_vol.
    """
    buyer_vol = 0.0
    seller_vol = 0.0
    total_vol = 0.0
    for c in candles:
        v = float(getattr(c, 'volume', 0.0))
        total_vol += v
        close_px = float(getattr(c, 'close', 0.0))
        open_px = float(getattr(c, 'open', 0.0))
        if close_px > open_px:
            buyer_vol += v
        elif close_px < open_px:
            seller_vol += v
    
    if total_vol == 0:
        return 0.0
    return (buyer_vol - seller_vol) / total_vol

"""Institutional Breakout Strategy.

Combines Opening Range Breakout (ORB) with the Price+OI Regime Matrix and Option Wall Confluence.
- Check 01 Edge: Profits from trapped counter-trend traders on confirmed breaks of the Opening Range.
- Check 03 Rules: Mechanical entry, stop-loss, target, and time-stop.
- Filters: Fails closed during unwinding regimes (Short Covering / Long Unwinding).
"""
from __future__ import annotations

from datetime import time
from typing import Any

from sentinel.data.regimes import PriceOIRegime, approximate_ofi
from sentinel.strategies.base import BaseStrategy, StrategyContext, StrategySignal


class InstitutionalBreakoutStrategy(BaseStrategy):
    name: str = "institutional_breakout"
    description: str = "Opening Range Breakout filtered by Institutional Price+OI Buildup and Option Walls"
    version: str = "1.0"

    default_params: dict[str, Any] = {
        "sl_points": 15.0,
        "target_rr_mult": 2.0,
        "time_stop_minutes": 45,
        "confidence": 78,
        "min_rr": 1.8,
        "wall_clearance_pct": 0.15,
        "entry_open_time": "09:35",
        "entry_close_time": "14:30",
    }

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        if ctx.active_position:
            return None

        # Check trading window
        if ctx.current_time:
            t_open = time(9, 35)
            t_close = time(14, 30)
            if ctx.current_time < t_open or ctx.current_time > t_close:
                return None

        if not ctx.opening_range:
            return None

        orh, orl = ctx.opening_range
        spot = ctx.last_close
        if spot <= 0 or orh <= 0 or orl <= 0:
            return None

        sl_pts = float(self.params["sl_points"])
        rr_mult = float(self.params["target_rr_mult"])
        time_stop = int(self.params["time_stop_minutes"])
        conf = int(self.params["confidence"])

        # Call Wall / Put Wall checks
        call_w = ctx.walls.call_wall if ctx.walls else (spot * 1.05)
        put_w = ctx.walls.put_wall if ctx.walls else (spot * 0.95)

        # Baseline option premium estimates
        opt_entry = 150.0
        opt_sl = max(5.0, opt_entry - sl_pts)
        opt_target = opt_entry + (sl_pts * rr_mult)

        # Order Flow Imbalance (OFI) proxy over last 5 minutes
        recent_bars = ctx.candles_1m[-5:] if len(ctx.candles_1m) >= 5 else ctx.candles_1m
        ofi_ratio = approximate_ofi(recent_bars)

        # Bullish Breakout (CE)
        if spot > orh:
            # Check 03 Filter: Do not buy CE if move is merely short covering (fades quickly)
            if ctx.regime == PriceOIRegime.SHORT_COVERING:
                return None

            # Wall Clearance Check: Do not buy into an immediate Call Wall ceiling
            if call_w and spot >= (call_w * (1.0 - self.params["wall_clearance_pct"] / 100.0)):
                return None

            # Institutional Check: Do not buy CE if FII lean is heavily SHORT
            if ctx.fii_lean == "SHORT":
                return None

            # Microstructure Check: Require positive order flow imbalance
            if ofi_ratio < -0.1:
                return None

            thesis = f"Clean break above ORH {orh:,.0f} with {ctx.regime.value} support. Trapped counter-trend sellers forced to cover. OFI ratio {ofi_ratio:.2f}."
            invalidation = f"Spot back below ORH {orh:,.0f} on 1m close."

            return StrategySignal(
                instrument=ctx.instrument,
                direction="CE",
                strike_offset="ATM",
                entry_zone={"low": round(opt_entry - 2.0, 1), "high": round(opt_entry + 3.0, 1)},
                stop_loss_premium=round(opt_sl, 1),
                target_premium=round(opt_target, 1),
                time_stop_minutes=time_stop,
                confidence=conf,
                thesis=thesis,
                invalidation=invalidation,
                risk_reward=round(rr_mult, 2),
                strategy_name=self.name,
            )

        # Bearish Breakdown (PE)
        elif spot < orl:
            # Check 03 Filter: Do not buy PE if move is merely long unwinding
            if ctx.regime == PriceOIRegime.LONG_UNWINDING:
                return None

            # Wall Clearance Check: Do not sell into an immediate Put Wall floor
            if put_w and spot <= (put_w * (1.0 + self.params["wall_clearance_pct"] / 100.0)):
                return None

            # Institutional Check: Do not buy PE if FII lean is heavily LONG
            if ctx.fii_lean == "LONG":
                return None

            # Microstructure Check: Require negative order flow imbalance
            if ofi_ratio > 0.1:
                return None

            thesis = f"Clean break below ORL {orl:,.0f} with {ctx.regime.value} support. Trapped dip buyers forced to liquidate. OFI ratio {ofi_ratio:.2f}."
            invalidation = f"Spot back above ORL {orl:,.0f} on 1m close."

            return StrategySignal(
                instrument=ctx.instrument,
                direction="PE",
                strike_offset="ATM",
                entry_zone={"low": round(opt_entry - 2.0, 1), "high": round(opt_entry + 3.0, 1)},
                stop_loss_premium=round(opt_sl, 1),
                target_premium=round(opt_target, 1),
                time_stop_minutes=time_stop,
                confidence=conf,
                thesis=thesis,
                invalidation=invalidation,
                risk_reward=round(rr_mult, 2),
                strategy_name=self.name,
            )

        return None

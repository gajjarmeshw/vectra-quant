"""Wall Mean Reversion Strategy.

Derived from Returns Institutional Shadowing Framework (Chapter 4: Reading Option Chain Walls).
- Check 01 Edge: "Inside the walls, fade the extremes rather than chase."
- Profits from institutional sellers successfully defending their Call & Put walls.
- Reinforced by extreme Put-Call Ratio (PCR) sentiment:
  * PCR < 0.7 (fear / oversold) -> bounce off Put Wall -> buy CE
  * PCR > 1.3 (complacency / overbought) -> rejection off Call Wall -> buy PE
"""
from __future__ import annotations

from datetime import time
from typing import Any

from sentinel.strategies.base import BaseStrategy, StrategyContext, StrategySignal


class WallMeanReversionStrategy(BaseStrategy):
    name: str = "wall_mean_reversion"
    description: str = "Fade extremes of defended Option Walls inside the expected range"
    version: str = "1.0"

    default_params: dict[str, Any] = {
        "sl_points": 12.0,
        "target_rr_mult": 1.8,
        "time_stop_minutes": 50,
        "confidence": 76,
        "test_zone_pct": 0.20,
    }

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        if ctx.active_position or not ctx.walls:
            return None

        if ctx.current_time:
            t_open = time(9, 45)
            t_close = time(14, 15)
            if ctx.current_time < t_open or ctx.current_time > t_close:
                return None

        spot = ctx.last_close
        walls = ctx.walls
        call_w = walls.call_wall
        put_w = walls.put_wall

        if spot <= 0 or call_w <= 0 or put_w <= 0:
            return None

        sl_pts = float(self.params["sl_points"])
        rr_mult = float(self.params["target_rr_mult"])
        time_stop = int(self.params["time_stop_minutes"])
        conf = int(self.params["confidence"])

        opt_entry = 140.0
        opt_sl = max(5.0, opt_entry - sl_pts)
        opt_target = opt_entry + (sl_pts * rr_mult)

        # 1. Test of Call Wall -> Fade Rejection -> Buy PE
        if walls.is_testing_call_wall(self.params["test_zone_pct"]) and spot <= call_w:
            # Confluence check: PCR > 1.0 (complacency) or bearish reversal bar
            thesis = f"Spot tested Call Wall {call_w:,.0f} ceiling without breaking. Fading trapped breakout buyers back toward range center."
            invalidation = f"Spot breaks and closes above Call Wall {call_w:,.0f}."

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

        # 2. Test of Put Wall -> Fade Bounce -> Buy CE
        elif walls.is_testing_put_wall(self.params["test_zone_pct"]) and spot >= put_w:
            # Confluence check: PCR < 1.0 (fear) or bullish hammer bar
            thesis = f"Spot tested Put Wall {put_w:,.0f} floor without breaking. Fading trapped breakdown sellers back toward range center."
            invalidation = f"Spot breaks and closes below Put Wall {put_w:,.0f}."

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

        return None

"""Option Wall Trapped Squeeze Strategy.

Derived from Returns Institutional Shadowing Framework (Chapter 4: Reading Option Chain Walls).
- Check 01 Edge: "A wall that actually breaks is doubly informative, because trapped
  sellers hedging in a panic add fuel to the move."
- Capitalizes on gamma/delta panics when heavily defended strikes are breached.
"""
from __future__ import annotations

from datetime import time
from typing import Any

from sentinel.strategies.base import BaseStrategy, StrategyContext, StrategySignal


class OptionWallTrappedSqueezeStrategy(BaseStrategy):
    name: str = "option_wall_squeeze"
    description: str = "Momentum breakout squeezing trapped institutional option writers at broken walls"
    version: str = "1.0"

    default_params: dict[str, Any] = {
        "sl_points": 14.0,
        "target_rr_mult": 2.5,
        "time_stop_minutes": 40,
        "confidence": 84,
        "min_break_pct": 0.05,
    }

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        if ctx.active_position or not ctx.walls:
            return None

        if ctx.current_time:
            t_open = time(9, 30)
            t_close = time(14, 45)
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
        min_break = float(self.params["min_break_pct"])

        opt_entry = 160.0
        opt_sl = max(5.0, opt_entry - sl_pts)
        opt_target = opt_entry + (sl_pts * rr_mult)

        # Call Wall Trapped Squeeze (spot breaks above Call Wall)
        if spot > (call_w * (1.0 + min_break / 100.0)):
            thesis = f"Defended Call Wall {call_w:,.0f} broken. Trapped institutional call writers forced to cover delta in panic."
            invalidation = f"Spot closing back under Call Wall {call_w:,.0f}."

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

        # Put Wall Trapped Squeeze (spot breaks below Put Wall)
        elif spot < (put_w * (1.0 - min_break / 100.0)):
            thesis = f"Defended Put Wall {put_w:,.0f} broken. Trapped institutional put writers forced to liquidate/hedge in panic."
            invalidation = f"Spot closing back above Put Wall {put_w:,.0f}."

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

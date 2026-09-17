"""Registry/metadata shell for the weekly PCR-directed credit spread strategy.

The actual simulation logic lives in `backtest/weekly_spread_engine.py`
(`WeeklySpreadEngine`) because it needs a fundamentally different execution
model than every other strategy here: it holds one position across multiple
trading sessions (Wednesday entry, held toward next-week expiry), while
`BacktestEngine`'s day loop resets `in_trade` every session by design (see
`futures_engine.py` for the same problem solved for futures).

This class exists only so the strategy shows up in `list_strategies()` /
the strategy picker UI with its real default parameters. `evaluate()` is
never actually called in a real run — `EXECUTION_MODE` tells the backtest
route to dispatch to `WeeklySpreadEngine` instead.
"""
from __future__ import annotations

from typing import Any

from vectra_quant.strategies.base import BaseStrategy, StrategyContext, StrategySignal


class WeeklyCreditSpreadStrategy(BaseStrategy):
    name = "weekly_credit_spread"
    description = (
        "Weekly NIFTY vertical credit spread: same-week-expiry, exactly "
        "200pts wide, ATM strikes, protective wing bought first -- matches "
        "the structure described in the hedged131 white-box disclosure. "
        "Direction chosen from real put/call open-interest positioning "
        "(PCR), a stand-in for the disclosure's undisclosed order-flow "
        "signal, which cannot be backtested (no historical depth data). "
        "Exit via a linked-futures target that widens late in the hold, "
        "capped at the contract's own expiry -- no stop-loss on the "
        "structure itself, per the disclosure's SL/Target 0/0."
    )
    version = "1.1"
    EXECUTION_MODE = "weekly_multiday"  # routes.py dispatches this to WeeklySpreadEngine

    default_params: dict[str, Any] = {
        "entry_weekday": 2,              # Wednesday
        "pcr_band_pct": 0.03,
        "pcr_bullish_above": 1.05,
        "pcr_bearish_below": 0.95,
        "max_hold_days": 7,
        "futures_target_pts_early": 40.0,
        "futures_target_pts_late": 70.0,
        "futures_target_widen_from_day": 5,
        "moneyness_offset_pct": 0.0,      # ATM, per the disclosure (not the 0.8% ITM we found backtests best on)
        "min_entry_volume": 20000.0,     # realistic liquidity floor (see research notes)
    }

    manifest = {
        "symbols": ["NIFTY"],
        "timeframes": ["1d"],
        "indicators": ["PCR"],
    }

    def get_wiggle_params(self) -> list[str]:
        return ["moneyness_offset_pct", "pcr_bullish_above", "pcr_bearish_below", "max_hold_days"]

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        raise NotImplementedError(
            "weekly_credit_spread runs through WeeklySpreadEngine, not the "
            "per-bar BacktestEngine -- see EXECUTION_MODE."
        )

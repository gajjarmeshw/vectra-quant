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

    # Descriptive only -- documents existing behaviour for the Strategies tab.
    mechanics = {
        # The deployed product reads live order flow; the version that RUNS here
        # substitutes PCR precisely because order-book history does not exist, so
        # honestly this backtestable form needs no order flow at all.
        "needs_orderflow": False,
        "direction": (
            "Open interest in the option chain — the put/call ratio. Far more puts "
            "open than calls is read as bullish, far more calls as bearish, and "
            "anything in between means no trade that week. NOTE: the deployed "
            "hedged131 product picks direction from live order flow instead; this "
            "is a documented stand-in, because no historical order-book depth "
            "exists to replay that real signal against."
        ),
        "trigger": (
            "The calendar, not a price event: it evaluates once on Wednesday (or "
            "Thursday if Wednesday was a holiday) and takes at most one trade a week."
        ),
        "filters": [
            "Neutral band — if the put/call ratio sits between the thresholds, the "
            "week is skipped entirely rather than forcing a trade.",
            "Liquidity floor — strikes without enough traded volume are not used.",
            "Missing strike — if either leg is not listed, the whole week is skipped "
            "rather than substituting a different structure.",
        ],
        "sizing": (
            "Units = investment amount divided by margin required, rounded down; "
            "both legs scale together so the spread width is always preserved."
        ),
        "exit": (
            "No stop-loss — the bought wing already caps the loss. It exits early if "
            "a linked futures position hits its point target (a target that widens "
            "later in the hold), otherwise everything is force-closed Tuesday 15:10."
        ),
    }

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

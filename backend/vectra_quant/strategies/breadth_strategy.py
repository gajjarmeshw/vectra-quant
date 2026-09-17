"""Registry/metadata shell for the cross-sectional order-flow breadth
signal (100 NIFTY100 stocks -> one market-wide reading).

This is our own signal, invented for this project -- not from any
disclosure, unlike thunderbolt/weekly_credit_spread. Backtested once
against a real ~50-minute capture (5-10 trades, 60-80% win rate) which is
explicitly noise-level sample size, not evidence of an edge. Genuinely
paper-trade-only for the same reason as Thunderbolt: `EXECUTION_MODE =
"live_only"` tells the backtest route to explain rather than fabricate.
"""
from __future__ import annotations

from typing import Any

from vectra_quant.strategies.base import BaseStrategy, StrategyContext, StrategySignal


class BreadthStrategy(BaseStrategy):
    name = "breadth"
    description = (
        "Cross-sectional order-flow breadth: per-stock top-of-book imbalance "
        "across the 100 NIFTY100 constituents, aggregated into one market-wide "
        "reading. On a threshold crossing, buys a single ATM option in the "
        "signal direction (CE bullish, PE bearish) -- a project-designed "
        "structure, not a disclosed one. Exits on a stop/target premium move "
        "or a max hold time. Backtested once against a real ~50-minute, "
        "100-stock capture: 5-10 trades, 60-80% win rate -- noise-level "
        "sample size, not evidence of an edge. Requires its own live 20-depth "
        "recorder covering the NIFTY100 + a NIFTY option-chain window "
        "(scripts/run_breadth_live.py), separate from the single-future "
        "recorder thunderbolt used."
    )
    version = "1.0"
    EXECUTION_MODE = "live_only"

    default_params: dict[str, Any] = {
        "signal_window_start": "09:20",
        "signal_window_end": "15:00",
        "force_exit_time": "15:10",
        "bucket_seconds": 15,
        "bullish_stock_threshold": 0.1,
        "bearish_stock_threshold": -0.1,
        "theta_z": 2.0,
        "min_stocks_reporting": 20,
        "stop_loss_pct_premium": 0.30,
        "target_pct_premium": 0.50,
        "max_hold_minutes": 15,
    }

    manifest = {
        "symbols": ["NIFTY100"],
        "timeframes": ["15s"],
        "indicators": ["CROSS_SECTIONAL_ORDER_FLOW_BREADTH"],
    }

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        raise NotImplementedError(
            "breadth runs through its own live multi-instrument recorder + "
            "paper trading loop, not any backtest engine -- see EXECUTION_MODE."
        )

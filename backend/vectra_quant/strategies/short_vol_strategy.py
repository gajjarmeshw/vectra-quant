"""Registry/metadata shell for the intraday short-volatility straddle.

The simulation lives in `backtest/short_vol_engine.py` because this structure
needs option-chain replay with its own entry/exit clocks and per-leg costs,
which `BacktestEngine`'s candle loop cannot express -- the same reason
`weekly_credit_spread_strategy.py` delegates to `WeeklySpreadEngine`.

This class exists so the strategy appears in `list_strategies()` and the
strategy picker with its real default parameters. `evaluate()` is never called
in a real run; `EXECUTION_MODE` routes the backtest to the dedicated engine.
"""
from __future__ import annotations

from typing import Any

from vectra_quant.strategies.base import BaseStrategy, StrategyContext, StrategySignal


class ShortVolStrategy(BaseStrategy):
    name = "short_vol"
    description = (
        "Intraday short volatility: sell the ATM NIFTY straddle shortly after "
        "the open and buy it back before the close, same session, no overnight "
        "exposure. Harvests the variance risk premium -- implied vol is priced "
        "above what the session actually realises roughly 74% of the time. "
        "Measured over 396 sessions (2024+) on real chain data: +Rs193/day/lot "
        "mean, 62% win rate, Sharpe 0.83. Carries a FAT LEFT TAIL: worst "
        "session -Rs23,245, max drawdown -Rs51,290, and one losing year in "
        "three (2024). Loss is unbounded in principle and the sample excludes "
        "the March-2020 crash -- size positions small."
    )
    version = "1.0"
    EXECUTION_MODE = "intraday_short_vol"   # routes.py dispatches to ShortVolEngine

    default_params: dict[str, Any] = {
        "lots": 1,
        # Rs/leg, MEASURED from the captured depth book (ATM half-spread ~0.14
        # index points ~= Rs9, plus brokerage, STT on sold premium, exchange
        # charges and GST). An earlier Rs100 guess inverted the conclusion.
        "cost_per_leg": 40.0,
        "entry_time": "09:20:00",
        "exit_time": "15:15:00",
        # 0 = trade every session. DTE=0 measured worst in research (expiry-day
        # gamma), but per-DTE samples are thin, so no filter is applied by default.
        "min_dte": 0,
    }

    manifest = {
        "symbols": ["NIFTY"],
        "timeframes": ["1m"],
        "indicators": ["ATM straddle premium", "implied volatility"],
    }

    mechanics = {
        "needs_orderflow": False,
        "direction": (
            "None — this strategy takes no directional view at all. It sells BOTH "
            "the call and the put at the same strike, so it makes money when the "
            "index stays put and loses when it moves sharply, in either direction."
        ),
        "trigger": (
            "The clock, not a signal. It enters every trading session at 09:20, "
            "selling the strike nearest to where NIFTY is trading at that moment."
        ),
        # Deliberately ONE entry, not two. Listing the optional filter separately
        # made the UI read "Filters · 2 · applied in order" above a list whose
        # own text said nothing is applied -- the count contradicted the content.
        "filters": [
            "None are applied — it trades every session. One optional filter exists "
            "('min_dte'): skip sessions close to expiry. Expiry day measured worst in "
            "testing (violent last-day price swings), but the per-day samples are "
            "small, so it stays off unless you turn it on.",
        ],
        "sizing": (
            "Fixed lot count that you set ('lots'); it does not vary with conviction "
            "or account size. Because the sold options are naked, margin required is "
            "large relative to the premium collected — size small."
        ),
        "exit": (
            "The clock again: everything is bought back at 15:15 the same day. There "
            "is no stop-loss and no profit target, and nothing is ever held overnight."
        ),
    }

    def get_wiggle_params(self) -> list[str]:
        return ["cost_per_leg", "min_dte", "lots"]

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        # Never invoked: EXECUTION_MODE sends backtests to ShortVolEngine.
        return None

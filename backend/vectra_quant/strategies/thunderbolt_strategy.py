"""Registry/metadata shell for hedged121 ("Nifty Thunderbolt").

Unlike `weekly_credit_spread` (which has a real backtestable stand-in
signal, PCR, computed from historical option-chain OI), Thunderbolt's
signal is the INSTANTANEOUS order-book imbalance series -- there is no
historical proxy for this at all, not even an approximate one, because
the archive has no order-book depth of any kind for any past date. This
strategy is genuinely paper-trade-only: `EXECUTION_MODE = "live_only"`
tells the backtest route to return a clear explanation instead of
attempting to fabricate a result.
"""
from __future__ import annotations

from typing import Any

from vectra_quant.strategies.base import BaseStrategy, StrategyContext, StrategySignal


class ThunderboltStrategy(BaseStrategy):
    name = "thunderbolt"
    description = (
        "Intraday NIFTY 1x2 ratio backspread (hedged121 / 'Nifty Thunderbolt'): "
        "SELL 1 lot ATM, BUY 2 lots 100pts out in the signal direction (longs "
        "placed first). Net long gamma -- bounded loss, open-ended gain beyond "
        "the long strike. Direction from the instantaneous order-book imbalance "
        "series (crossing/reversal in MEDIUM/HIGH regimes, swing breakout in "
        "LOW), through a 5-stage filter chain. Skips Tuesdays and any day where "
        "the prior session's India VIX was >= 22. Force-exits 15:10, no "
        "intraday stop or target. GENUINELY NOT BACKTESTABLE: there is no "
        "historical order-book depth of any kind in the archive, not even an "
        "approximate stand-in -- this can only be paper-traded going forward."
    )
    version = "1.0"
    EXECUTION_MODE = "live_only"  # routes.py returns an explanatory response instead of attempting a backtest

    # Descriptive only -- documents the behaviour implemented in
    # thunderbolt_signal.py for the Strategies tab. Changes nothing at runtime.
    mechanics = {
        "needs_orderflow": True,
        "direction": (
            "Live order-book imbalance on NIFTY: how much resting buy quantity sits "
            "in the book versus sell quantity, moment to moment. More buyers waiting "
            "than sellers reads bullish, the reverse reads bearish. A reversal check "
            "can FLIP that read -- if the opposite side was even stronger earlier in "
            "the session, the crossing is treated as that other side exhausting."
        ),
        "trigger": (
            "The FIRST time the imbalance crosses a conviction level after 09:16, on "
            "normal and high-volatility days. On quiet days it instead waits for the "
            "reading to break out past a confirmed earlier swing extreme."
        ),
        "filters": [
            "Opposite-side gate — if the other side showed real strength earlier, the "
            "signal must clearly dominate it or the day is skipped.",
            "Pre-open lock — an extreme reading before the open blocks trades against "
            "it; extremes on both sides block the day entirely.",
            "Liquidity-reversal flip — heavy one-way interest early, answered by a "
            "meaningful push the other way, inverts the direction.",
            "Medium-regime flip — on medium-volatility days a specific early pattern "
            "inverts the direction.",
            "Over-stretch veto — a reading already at an extreme is skipped: the "
            "strategy wants the START of a move, not its climax.",
            "Calendar: never trades Tuesday, and skips any day after a session where "
            "India VIX closed at or above 22.",
        ],
        "sizing": (
            "Units = investment amount divided by the margin one structure needs, "
            "rounded down. Both legs scale together so the 1:2 ratio always holds."
        ),
        "exit": (
            "No stop-loss and no profit target during the day — the shape of the "
            "trade already caps the loss. Everything is force-closed at 15:10."
        ),
    }

    default_params: dict[str, Any] = {
        "signal_window_start": "09:16",
        "signal_window_end": "15:00",
        "force_exit_time": "15:10",
        "skip_tuesday": True,
        "vix_skip_at_or_above": 22.0,
        "theta_cross": 0.15,
        "over_stretch_bound": 0.6,
        "long_offset_pts": 100.0,
        "long_lots": 2,
        "short_lots": 1,
    }

    manifest = {
        "symbols": ["NIFTY"],
        "timeframes": ["1m"],
        "indicators": ["ORDER_BOOK_IMBALANCE"],
    }

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        raise NotImplementedError(
            "thunderbolt runs through the live order-flow recorder + paper "
            "trading loop, not any backtest engine -- see EXECUTION_MODE."
        )

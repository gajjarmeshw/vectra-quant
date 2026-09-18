"""Base abstractions for the VectraQuant Dynamic Strategy & Algo Engine."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import time
from typing import TYPE_CHECKING, Any

from vectra_quant.data.candles import Candle
from vectra_quant.data.regimes import OptionWalls, PriceOIRegime

if TYPE_CHECKING:
    pass


@dataclass
class StrategyContext:
    """Market context passed to each strategy on every evaluation interval."""
    instrument: str
    spot: float
    candles_1m: list[Candle] = field(default_factory=list)
    candles_5m: list[Candle] = field(default_factory=list)
    opening_range: tuple[float, float] | None = None
    prev_day_range: tuple[float, float] | None = None
    atr: float | None = None
    walls: OptionWalls | None = None
    regime: PriceOIRegime = PriceOIRegime.NEUTRAL
    vix: float | None = None
    current_time: time | None = None
    is_expiry_day: bool = False
    active_position: bool = False
    fii_lean: str = "FLAT"

    @property
    def last_close(self) -> float:
        if self.candles_1m:
            return self.candles_1m[-1].close
        return self.spot


@dataclass
class StrategySignal:
    """A formal trade proposal emitted by an algo strategy.

    Matches the strict JSON schema expected by VectraQuant's lifecycle gatekeeper.
    """
    direction: str                     # "CE" | "PE"
    action: str = "BUY"                # "BUY" | "SELL"
    strike_offset: str = "ATM"         # "ATM", "ITM1", "OTM1", etc.
    entry_zone: dict[str, float] = field(default_factory=dict)
    stop_loss_premium: float = 0.0
    target_premium: float = 0.0
    time_stop_minutes: int = 45
    confidence: int = 75
    thesis: str = ""
    invalidation: str = ""
    risk_reward: float = 1.8
    strategy_name: str = ""
    instrument: str = ""

    def to_suggestion_payload(self) -> dict[str, Any]:
        """Format as a suggestion dictionary compatible with lifecycle.apply_gates."""
        mid_entry = (self.entry_zone.get("low", 0.0) + self.entry_zone.get("high", 0.0)) / 2.0
        risk = max(1.0, mid_entry - self.stop_loss_premium)
        reward = max(1.0, self.target_premium - mid_entry)
        computed_rr = round(reward / risk, 2)

        return {
            "action": "SUGGEST",
            "trade_action": self.action,
            "instrument": self.instrument,
            "direction": self.direction,
            "strike_offset": self.strike_offset,
            "entry_zone": self.entry_zone,
            "stop_loss_premium": round(self.stop_loss_premium, 2),
            "target_premium": round(self.target_premium, 2),
            "time_stop_minutes": self.time_stop_minutes,
            "confidence": self.confidence,
            "thesis": self.thesis[:240],
            "invalidation": self.invalidation,
            "risk_reward": computed_rr,
            "strategy": self.strategy_name,
        }


class BaseStrategy(ABC):
    """Abstract base class for all pluggable VectraQuant algo strategies."""

    name: str = "base"
    description: str = "Base Strategy"
    version: str = "1.0"
    default_params: dict[str, Any] = {}

    # True for strategies that drive trades by calling self._emit_signal(...)
    # from on_candle() (the live/production pattern) instead of returning a
    # StrategySignal from evaluate(). The backtest engine drives these by
    # installing a signal collector and calling on_candle() every bar.
    USES_PUSH_SIGNALS: bool = False

    # Declarative data requirements for the DataOrchestrator
    manifest: dict[str, list[str]] = {
        "symbols": [],
        "timeframes": [],
        "indicators": []
    }

    # Plain-language description of HOW the strategy actually decides, surfaced
    # in the Strategies tab. It lives on the class (not in the frontend) so the
    # explanation ships with the logic it describes and cannot silently drift
    # away from it. Every field is prose aimed at a reader who has not read the
    # code; `needs_orderflow` is the one the UI keys on, because whether a
    # strategy depends on the live order book decides whether it can run at all
    # without the depth recorder.
    mechanics: dict[str, Any] = {
        "needs_orderflow": False,
        "direction": "",   # what picks bullish vs bearish (or states it is non-directional)
        "trigger": "",     # what actually fires the entry
        "filters": [],     # list[str]: what can veto or flip the raw signal, in order
        "sizing": "",      # how position size is decided
        "exit": "",        # how and when the position is closed
    }

    def __init__(self, params: dict[str, Any] | None = None):
        self.params: dict[str, Any] = dict(self.default_params)
        if params:
            self.params.update(params)

    def on_tick(self, symbol: str, tick: dict[str, Any]) -> None:
        """Triggered on live WebSocket tick."""
        pass

    def on_candle(self, symbol: str, timeframe: str, candle: Candle) -> None:
        """Triggered automatically when a new candle closes."""
        pass

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        """Evaluate current market context and return a StrategySignal if entry conditions are met."""
        ...

    def get_wiggle_params(self) -> list[str]:
        """Return list of numeric parameter keys eligible for ±20% parameter wiggle testing."""
        return [k for k, v in self.params.items() if isinstance(v, (int, float))]

    def clone_with(self, **overrides: Any) -> BaseStrategy:
        """Create a new instance with overridden parameters."""
        new_params = dict(self.params)
        new_params.update(overrides)
        return self.__class__(new_params)

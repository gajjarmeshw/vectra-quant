"""Sentinel Pluggable Dynamic Strategies Package."""
from sentinel.strategies.base import BaseStrategy, StrategyContext, StrategySignal
from sentinel.strategies.institutional_breakout import InstitutionalBreakoutStrategy
from sentinel.strategies.option_wall_squeeze import OptionWallTrappedSqueezeStrategy
from sentinel.strategies.registry import get_strategy, list_strategies, register_strategy
from sentinel.strategies.wall_mean_reversion import WallMeanReversionStrategy

__all__ = [
    "BaseStrategy",
    "StrategyContext",
    "StrategySignal",
    "InstitutionalBreakoutStrategy",
    "OptionWallTrappedSqueezeStrategy",
    "WallMeanReversionStrategy",
    "get_strategy",
    "list_strategies",
    "register_strategy",
]

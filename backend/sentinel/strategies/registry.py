"""Dynamic Strategy Registry for Sentinel Algo Engine."""
from __future__ import annotations

from typing import Any

from sentinel.strategies.base import BaseStrategy
from sentinel.strategies.institutional_breakout import InstitutionalBreakoutStrategy
from sentinel.strategies.option_wall_squeeze import OptionWallTrappedSqueezeStrategy
from sentinel.strategies.wall_mean_reversion import WallMeanReversionStrategy

_STRATEGIES: dict[str, type[BaseStrategy]] = {
    InstitutionalBreakoutStrategy.name: InstitutionalBreakoutStrategy,
    OptionWallTrappedSqueezeStrategy.name: OptionWallTrappedSqueezeStrategy,
    WallMeanReversionStrategy.name: WallMeanReversionStrategy,
}


def register_strategy(cls: type[BaseStrategy]) -> None:
    """Register a custom trading strategy class."""
    _STRATEGIES[cls.name] = cls


def get_strategy(name: str, params: dict[str, Any] | None = None) -> BaseStrategy:
    """Instantiate a registered strategy by name with optional parameter overrides."""
    strat_cls = _STRATEGIES.get(name.lower().strip())
    if not strat_cls:
        raise ValueError(f"Unknown strategy '{name}'. Available: {list(_STRATEGIES.keys())}")
    return strat_cls(params)


def list_strategies() -> list[dict[str, Any]]:
    """Return metadata and default parameter definitions for all registered strategies."""
    result: list[dict[str, Any]] = []
    display_names = {
        "institutional_breakout": "Institutional Breakout (ORB + Walls)",
        "option_wall_squeeze": "Trapped Option Wall Squeeze",
        "wall_mean_reversion": "Defended Range Mean Reversion",
    }
    for name, cls in _STRATEGIES.items():
        result.append({
            "name": name,
            "display_name": display_names.get(name, name.replace("_", " ").title()),
            "instrument_focus": ["NIFTY", "SENSEX", "BANKNIFTY"],
            "description": cls.description,
            "version": cls.version,
            "default_params": cls.default_params,
            "wiggle_params": [k for k, v in cls.default_params.items() if isinstance(v, (int, float))],
        })
    return result

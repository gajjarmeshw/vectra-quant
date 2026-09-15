"""Sizing gate and strike selection.

Wraps `risk_engine.size_position` additively — the engine is never edited (§0.1).
Two jobs:

1. `size_with_cap`  — apply the hard Rs.14,000 position-cost ceiling (D-003) and fix
   the engine's unaffordable-branch reporting bug, where `risk` and `cost` describe
   the pre-reduction lot count.
2. `select_strike`  — rank chain candidates toward a Rs.7,000-12,000 one-lot cost with
   a real chance of moving (D-010). Ranking only; it cannot create a trade or
   loosen a gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vectra_quant.risk_engine import size_position

# ---------------------------------------------------------------- sizing


@dataclass(frozen=True)
class SizedOrder:
    lots: int
    qty: int
    risk: float                # rupees at risk if the stop fills
    cost: float                # rupees to open the position
    over_risk: bool            # 1-lot risk exceeds max_risk (user accepts, flagged)
    allowed: bool
    reason: str = ""
    lot_size: int = 0
    premium: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "lots": self.lots, "qty": self.qty, "risk": round(self.risk, 2),
            "cost": round(self.cost, 2), "over_risk": self.over_risk,
            "allowed": self.allowed, "reason": self.reason,
        }


def size_with_cap(
    *,
    max_risk: float,
    sl_points_premium: float,
    lot_size: int,
    premium: float,
    capital: float,
    max_position_cost: float,
    min_lots: int = 1,
    max_lots: int = 0,
) -> SizedOrder:
    """Size a long-option entry, then enforce the absolute cost ceiling.

    The engine's own rule stands: at least 1 lot is always *offered*, flagged
    `over_risk` when a single lot exceeds `max_risk`. This wrapper is what can
    still refuse the trade, and it refuses on cost, never by shrinking below 1 lot.
    """
    if lot_size <= 0:
        return SizedOrder(0, 0, 0.0, 0.0, False, False,
                          "lot size unknown — refusing to guess (§0.3)")
    if premium <= 0:
        return SizedOrder(0, 0, 0.0, 0.0, False, False, "no premium available")
    if sl_points_premium <= 0:
        return SizedOrder(0, 0, 0.0, 0.0, False, False, "stop distance not set")

    raw = size_position(
        max_risk=max_risk,
        sl_points_premium=sl_points_premium,
        lot_size=lot_size,
        premium=premium,
        capital=capital,
    )
    lots = max(int(min_lots), int(raw["lots"]))
    if max_lots > 0:
        lots = min(lots, int(max_lots))     # per-instrument ceiling

    # Recompute from the FINAL lot count. The engine reports risk/cost from its
    # pre-affordability figure, which would mislead the margin check (blocker B5).
    risk_per_lot = sl_points_premium * lot_size
    cost_per_lot = premium * lot_size

    # Trim to the cost ceiling where more than one lot is affordable.
    while lots > min_lots and cost_per_lot * lots > max_position_cost:
        lots -= 1

    cost = cost_per_lot * lots
    risk = risk_per_lot * lots
    over_risk = risk_per_lot > max_risk + 1e-9

    if cost > max_position_cost + 1e-9:
        return SizedOrder(
            0, 0, risk, cost, over_risk, False,
            f"one lot costs {cost_per_lot:,.0f} > cap {max_position_cost:,.0f}",
            lot_size=lot_size, premium=premium,
        )
    if cost > capital + 1e-9:
        return SizedOrder(
            0, 0, risk, cost, over_risk, False,
            f"cost {cost:,.0f} exceeds capital {capital:,.0f}",
            lot_size=lot_size, premium=premium,
        )

    return SizedOrder(
        lots=lots, qty=lots * lot_size, risk=risk, cost=cost,
        over_risk=over_risk, allowed=True, reason="", lot_size=lot_size, premium=premium,
    )


# ---------------------------------------------------------------- strike selection


@dataclass
class Candidate:
    trading_symbol: str
    strike: float
    side: str                  # CE | PE
    premium: float
    lot_size: int
    atm_offset: int = 0        # 0 = ATM, 1 = one strike away, ...
    open_interest: float = 0.0
    volume: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    exchange: str = ""
    expiry: str = ""
    score: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def one_lot_cost(self) -> float:
        return self.premium * self.lot_size

    @property
    def spread_pct(self) -> float:
        if self.bid <= 0 or self.ask <= 0 or self.premium <= 0:
            return 0.0
        return (self.ask - self.bid) / self.premium


DEFAULT_WEIGHTS = {
    "cost_band": 3.0,
    "atm_proximity": 2.0,
    "open_interest": 1.5,
    "volume": 1.0,
    "spread_penalty": 2.0,
}


def score_candidate(
    c: Candidate,
    *,
    preferred_min: float,
    preferred_max: float,
    max_position_cost: float,
    max_atm_offset: int,
    weights: dict[str, float] | None = None,
    oi_reference: float = 0.0,
    volume_reference: float = 0.0,
) -> float:
    """Higher is better. Returns -inf for candidates that must never be chosen."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    if c.one_lot_cost > max_position_cost:
        c.notes.append("above hard cost cap")
        return float("-inf")
    if c.atm_offset > max_atm_offset:
        c.notes.append("too far from ATM")
        return float("-inf")
    if c.premium <= 0:
        return float("-inf")

    score = 0.0

    # Cost band: full marks inside, decaying outside rather than cliff-edged.
    cost = c.one_lot_cost
    if preferred_min <= cost <= preferred_max:
        score += w["cost_band"]
        c.notes.append("in preferred cost band")
    else:
        edge = preferred_min if cost < preferred_min else preferred_max
        span = max(preferred_max - preferred_min, 1.0)
        score += w["cost_band"] * max(0.0, 1.0 - abs(cost - edge) / span) * 0.5

    # ATM proximity: nearer strikes have tighter spreads and more delta.
    score += w["atm_proximity"] * (1.0 - c.atm_offset / max(max_atm_offset, 1))

    if oi_reference > 0:
        score += w["open_interest"] * min(1.0, c.open_interest / oi_reference)
    if volume_reference > 0:
        score += w["volume"] * min(1.0, c.volume / volume_reference)

    if c.spread_pct > 0:
        score -= w["spread_penalty"] * min(1.0, c.spread_pct / 0.10)
        if c.spread_pct > 0.10:
            c.notes.append(f"wide spread {c.spread_pct:.1%}")

    c.score = score
    return score


def select_strike(
    candidates: list[Candidate],
    *,
    preferred_min: float = 7_000.0,
    preferred_max: float = 12_000.0,
    max_position_cost: float = 14_000.0,
    max_atm_offset: int = 2,
    weights: dict[str, float] | None = None,
    min_open_interest: float = 0.0,
    min_volume: float = 0.0,
) -> Candidate | None:
    """Best contract, or None when nothing is tradable. None means no trade, never a stretch."""
    pool = [
        c for c in candidates
        if c.lot_size > 0
        and c.premium > 0
        and c.open_interest >= min_open_interest
        and c.volume >= min_volume
    ]
    if not pool:
        return None

    oi_ref = max((c.open_interest for c in pool), default=0.0)
    vol_ref = max((c.volume for c in pool), default=0.0)

    scored: list[Candidate] = []
    for c in pool:
        s = score_candidate(
            c,
            preferred_min=preferred_min,
            preferred_max=preferred_max,
            max_position_cost=max_position_cost,
            max_atm_offset=max_atm_offset,
            weights=weights,
            oi_reference=oi_ref,
            volume_reference=vol_ref,
        )
        if s != float("-inf"):
            c.score = s
            scored.append(c)

    if not scored:
        return None
    # Tie-break toward the nearer strike, then the cheaper contract.
    scored.sort(key=lambda c: (-c.score, c.atm_offset, c.one_lot_cost))
    return scored[0]


def nearest_atm(strikes: list[float], spot: float) -> float | None:
    return min(strikes, key=lambda s: abs(s - spot)) if strikes else None


def atm_offset(strike: float, atm: float, step: float) -> int:
    return 0 if step <= 0 else int(round(abs(strike - atm) / step))


def strike_step(strikes: list[float]) -> float:
    """Modal gap between consecutive strikes — robust to gaps in illiquid tails."""
    if len(strikes) < 2:
        return 0.0
    ordered = sorted(strikes)
    gaps: dict[float, int] = {}
    for a, b in zip(ordered, ordered[1:], strict=False):
        g = round(b - a, 2)
        if g > 0:
            gaps[g] = gaps.get(g, 0) + 1
    return max(gaps.items(), key=lambda kv: kv[1])[0] if gaps else 0.0

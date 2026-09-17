"""Daily-cadence order-flow credit spread -- paper-trade only, not backtestable.

This is this project's own extension, not part of the disclosed hedged131
product: hedged131 trades once a week (Wednesday), which means a 6-day
wait between paper-trading data points. Evaluating and entering the SAME
structure every trading day instead gives ~250 data points/year rather
than ~50, so calibration and "does this even work" feedback arrives in
days, not months -- at the cost of no longer being what hedged131 actually
does. Structure (ATM/200pt, protective leg first) is kept faithful to the
disclosure; cadence and the "always enter" fallback are not.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vectra_quant.orderflow.charts import (
    Direction,
    FinalBias,
    apply_low_vix_contrarian_filter,
    decision_to_bullish_bearish,
    resolve_direction,
    sign_to_direction,
    signed_area_read,
    value_read,
)
from vectra_quant.orderflow.config import OrderFlowCfg


@dataclass(frozen=True)
class DailyDecision:
    final_bias: FinalBias
    chart1_dir: Direction
    chart2_dir: Direction
    used_fallback: bool   # True when the base resolution was SKIP and "always enter" picked a side anyway
    chart2_value_at_decision: float | None


def decide_daily_direction(
    chart1_series: list[tuple[datetime, float]],
    chart2_series: list[tuple[datetime, float]],
    cutoff: datetime,
    vix: float | None,
    cfg: OrderFlowCfg,
    fallback_side: FinalBias = FinalBias.BEARISH,
) -> DailyDecision:
    """Value-read Chart 1, signed-area-read Chart 2 at `cutoff`, resolve a
    direction, apply the low-VIX filter, and -- because this variant always
    enters -- fall back to `fallback_side` if the base decision is SKIP."""
    c1_value = value_read(chart1_series, cutoff)
    c2_area = signed_area_read(chart2_series, cutoff)

    chart1_dir = sign_to_direction(c1_value or 0.0, cfg.eps_neutral)
    chart2_dir = sign_to_direction(c2_area, cfg.eps_neutral)

    decision = resolve_direction(chart1_dir, chart2_dir)
    base_bias = decision_to_bullish_bearish(decision, chart1_dir, chart2_dir)

    is_weak = chart1_dir != chart2_dir or abs(c2_area) < cfg.theta_conv
    if base_bias == FinalBias.SKIP:
        filtered = FinalBias.SKIP
    else:
        filtered = apply_low_vix_contrarian_filter(
            base_bias, vix, cfg.theta_vix, is_weak, enabled=cfg.vix_filter_enabled,
        )

    used_fallback = False
    final = filtered
    if final == FinalBias.SKIP and cfg.daily_always_enter:
        # No clear read -- lean on Chart 2's raw sign rather than discard the
        # day entirely; an exact tie falls back to the configured default.
        final = FinalBias.BULLISH if c2_area > 0 else (FinalBias.BEARISH if c2_area < 0 else fallback_side)
        used_fallback = True

    return DailyDecision(
        final_bias=final, chart1_dir=chart1_dir, chart2_dir=chart2_dir,
        used_fallback=used_fallback, chart2_value_at_decision=c2_area,
    )


@dataclass(frozen=True)
class SpreadLeg:
    action: str    # "BUY" or "SELL"
    right: str     # "CE" or "PE"
    strike: float
    order: int     # 1 = placed first (protective leg), 2 = placed second


def build_daily_structure(bias: FinalBias, spot: float, cfg: OrderFlowCfg) -> list[SpreadLeg]:
    """ATM = nearest listed strike (step 50); protective wing 200pts out,
    placed FIRST, then the ATM leg sold -- matches the disclosed structure
    exactly (section 9 of the hedged131 white-box disclosure)."""
    if bias == FinalBias.SKIP:
        return []

    atm = round(spot / cfg.strike_step) * cfg.strike_step
    if bias == FinalBias.BEARISH:
        return [
            SpreadLeg(action="BUY", right="CE", strike=atm + cfg.wing_width_pts, order=1),
            SpreadLeg(action="SELL", right="CE", strike=atm, order=2),
        ]
    return [
        SpreadLeg(action="BUY", right="PE", strike=atm - cfg.wing_width_pts, order=1),
        SpreadLeg(action="SELL", right="PE", strike=atm, order=2),
    ]

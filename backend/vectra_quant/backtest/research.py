"""Research harness — the anti-self-deception layer for strategy validation.

Phase 1 fixed the accounting (position-level, not per-leg). This module
answers a harder question honestly: does the SIGNAL have a real edge, and
does the evidence for it survive scrutiny?

Two things motivate its design, both found by hand while investigating the
Renko strategy:

1. **Instrument friction can hide a real edge.** In 2025, box=75 captured
   +18.0 underlying points/signal — genuine edge — and the credit-spread
   wrapper still lost -Rs669. Measuring P&L alone would have called that
   signal worthless. `measure_signal_edge` measures the underlying's point
   move captured per trade, independent of whatever instrument later wraps
   it, so a real signal is never mistaken for a bad one (or vice versa).

2. **A single in-sample number is not evidence.** box=75 on 2026 alone
   looked like a discovery (+36.7 pts, t=1.82); only pooling with 2024/2025
   and demanding every period be independently positive made it credible
   (barely — no single year alone reaches t>=2). `walk_forward_test` and
   `plateau_gate` encode that discipline so it can't be skipped by mistake
   under a deadline.

Every function here returns numbers with `n` and a t-stat attached — never
a bare rupee figure — because a strategy's "profit" is not evidence of an
edge until you know how many trades it came from and how noisy they were.
"""
from __future__ import annotations

import math
import statistics as st
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from vectra_quant.backtest import iea_data
from vectra_quant.backtest.engine import BacktestEngine, BacktestResult
from vectra_quant.strategies.registry import get_strategy

MIN_TRUSTWORTHY_N = 30
MIN_T_STAT = 2.0
MIN_FRICTION_RATIO = 3.0
PLATEAU_PERTURB_PCT = 0.20


@dataclass
class SignalStats:
    """Underlying points captured per signal — instrument-independent truth."""
    period_label: str
    n: int
    mean_pts: float = 0.0
    stdev_pts: float = 0.0
    t_stat: float = 0.0
    hit_rate: float = 0.0
    avg_win_pts: float = 0.0
    avg_loss_pts: float = 0.0
    total_pts: float = 0.0

    @property
    def significant(self) -> bool:
        """t>=2 AND enough trades that the t-stat itself is meaningful."""
        return self.n >= MIN_TRUSTWORTHY_N and abs(self.t_stat) >= MIN_T_STAT

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period_label, "n": self.n, "mean_pts": self.mean_pts,
            "stdev_pts": self.stdev_pts, "t_stat": self.t_stat, "hit_rate": self.hit_rate,
            "avg_win_pts": self.avg_win_pts, "avg_loss_pts": self.avg_loss_pts,
            "total_pts": self.total_pts, "significant": self.significant,
        }


def _spot_lookup(instrument: str, from_date: str, to_date: str) -> dict[str, float]:
    spot: dict[str, float] = {}
    for _day, bars in iea_data.load_index_window(instrument, from_date=from_date, to_date=to_date).items():
        for b in bars:
            spot[b.timestamp[:16]] = b.close
    return spot


def _group_positions(res: BacktestResult) -> dict[str, list]:
    groups: dict[str, list] = defaultdict(list)
    for t in res.trades:
        groups[t.position_id or t.id].append(t)
    return groups


def measure_signal_edge(
    strategy_name: str,
    instrument: str,
    from_date: str,
    to_date: str,
    params: dict[str, Any] | None = None,
    period_label: str = "",
) -> SignalStats:
    """Run one backtest and measure underlying points captured per position.

    Direction sign convention (matches renko_strategy's credit spreads): a
    PE-leg position is the bullish side (sells puts, wants price up), a
    CE-leg position is bearish (sells calls, wants price down) — so the
    signed move is `+1 * delta` for PE and `-1 * delta` for CE.
    """
    strat = get_strategy(strategy_name, params)
    res = BacktestEngine().run_strategy(strategy=strat, instrument=instrument, from_date=from_date, to_date=to_date)
    return _signal_stats_from_result(res, instrument, from_date, to_date, period_label)


def _signal_stats_from_result(res: BacktestResult, instrument: str, from_date: str, to_date: str, period_label: str) -> SignalStats:
    spot = _spot_lookup(instrument, from_date, to_date)
    moves: list[float] = []
    for legs in _group_positions(res).values():
        first = legs[0]
        s0, s1 = spot.get((first.opened_at or "")[:16]), spot.get((first.closed_at or "")[:16])
        if s0 is None or s1 is None:
            continue
        sign = 1.0 if first.direction == "PE" else -1.0
        moves.append(sign * (s1 - s0))

    n = len(moves)
    if n == 0:
        return SignalStats(period_label, 0)

    mean = st.mean(moves)
    stdev = st.pstdev(moves) if n > 1 else 0.0
    t_stat = (mean / (stdev / math.sqrt(n))) if stdev > 0 else 0.0
    wins = [m for m in moves if m > 0]
    losses = [m for m in moves if m <= 0]

    return SignalStats(
        period_label=period_label, n=n, mean_pts=round(mean, 2), stdev_pts=round(stdev, 2),
        t_stat=round(t_stat, 2), hit_rate=round(len(wins) / n * 100.0, 1),
        avg_win_pts=round(st.mean(wins), 2) if wins else 0.0,
        avg_loss_pts=round(st.mean(losses), 2) if losses else 0.0,
        total_pts=round(sum(moves), 1),
    )


def pool_signal_stats(stats_list: list[SignalStats], label: str = "pooled") -> SignalStats:
    """Combine several periods' SignalStats into one pooled statistic.

    Uses the proper pooled-variance formula (within-period variance PLUS
    between-period variance, since the periods' means differ) rather than a
    naive re-average of means/stdevs — getting this wrong is exactly the
    kind of self-deception this module exists to prevent.
    """
    total_n = sum(s.n for s in stats_list)
    if total_n == 0:
        return SignalStats(label, 0)

    pooled_mean = sum(s.n * s.mean_pts for s in stats_list) / total_n
    within = sum(s.n * (s.stdev_pts ** 2) for s in stats_list)
    between = sum(s.n * (s.mean_pts - pooled_mean) ** 2 for s in stats_list)
    pooled_var = (within + between) / total_n
    pooled_stdev = math.sqrt(pooled_var)
    t_stat = (pooled_mean / (pooled_stdev / math.sqrt(total_n))) if pooled_stdev > 0 else 0.0
    hit_rate = round(sum(s.n * s.hit_rate for s in stats_list) / total_n, 1) if total_n else 0.0

    return SignalStats(
        period_label=label, n=total_n, mean_pts=round(pooled_mean, 2), stdev_pts=round(pooled_stdev, 2),
        t_stat=round(t_stat, 2), hit_rate=hit_rate, total_pts=round(sum(s.total_pts for s in stats_list), 1),
    )


def sweep_param_grid(
    strategy_name: str,
    instrument: str,
    param_name: str,
    values: list[float],
    periods: list[tuple[str, str]],
    base_params: dict[str, Any] | None = None,
) -> dict[float, dict[str, Any]]:
    """Run `measure_signal_edge` for every (value, period) pair.

    This is the tool for "is there a broad positive region, or one lucky
    spike?" across BOTH the parameter axis and the time axis simultaneously
    — a value that's only positive in one period, or only at one exact
    setting, fails this even if either check alone would have passed it.
    """
    out: dict[float, dict[str, Any]] = {}
    for v in values:
        params = dict(base_params or {})
        params[param_name] = v
        per_period = [
            measure_signal_edge(strategy_name, instrument, f, t, params, f"{param_name}={v}:{f}..{t}")
            for (f, t) in periods
        ]
        pooled = pool_signal_stats(per_period, label=f"{param_name}={v}:pooled")
        out[v] = {
            "per_period": [s.as_dict() for s in per_period],
            "pooled": pooled.as_dict(),
            "all_periods_positive": all(s.mean_pts > 0 for s in per_period if s.n > 0),
        }
    return out


@dataclass
class WalkForwardResult:
    train: SignalStats
    validate: SignalStats
    holdout: SignalStats
    verdict: str  # PROMOTABLE | REJECTED
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "train": self.train.as_dict(), "validate": self.validate.as_dict(),
            "holdout": self.holdout.as_dict(), "verdict": self.verdict, "reasons": self.reasons,
        }


def walk_forward_test(
    strategy_name: str,
    instrument: str,
    params: dict[str, Any] | None = None,
    train: tuple[str, str] = ("2021-01-01", "2024-12-31"),
    validate: tuple[str, str] = ("2025-01-01", "2025-12-31"),
    holdout: tuple[str, str] = ("2026-01-01", "2026-09-15"),
) -> WalkForwardResult:
    """A parameter choice is never allowed to be judged on the data it was picked from.

    `train` is where you may look while choosing params. `validate` and
    `holdout` must stay positive and, between them, reach significance —
    train alone reaching significance proves nothing (that's what train is
    for: finding candidates, not confirming them).
    """
    tr = measure_signal_edge(strategy_name, instrument, *train, params, "train")
    va = measure_signal_edge(strategy_name, instrument, *validate, params, "validate")
    ho = measure_signal_edge(strategy_name, instrument, *holdout, params, "holdout")

    reasons: list[str] = []
    for label, s in (("train", tr), ("validate", va), ("holdout", ho)):
        if s.n < MIN_TRUSTWORTHY_N:
            reasons.append(f"{label}: only {s.n} signals (<{MIN_TRUSTWORTHY_N}) — too small to trust")
        if s.n > 0 and s.mean_pts <= 0:
            reasons.append(f"{label}: mean edge is non-positive ({s.mean_pts} pts/signal)")

    if not reasons and not (va.significant or ho.significant):
        reasons.append(
            f"neither validate (t={va.t_stat}, n={va.n}) nor holdout (t={ho.t_stat}, n={ho.n}) "
            f"reaches significance (t>={MIN_T_STAT}) — positive but indistinguishable from luck"
        )

    verdict = "PROMOTABLE" if not reasons else "REJECTED"
    if not reasons:
        reasons = ["all periods positive; validate and/or holdout reach significance"]
    return WalkForwardResult(tr, va, ho, verdict, reasons)


@dataclass
class PlateauResult:
    base: SignalStats
    neighbours: dict[str, SignalStats]
    verdict: str  # PLATEAU | NEEDLE
    failing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "base": self.base.as_dict(),
            "neighbours": {k: v.as_dict() for k, v in self.neighbours.items()},
            "verdict": self.verdict, "failing": self.failing,
        }


def plateau_gate(
    strategy_name: str,
    instrument: str,
    base_params: dict[str, Any],
    period: tuple[str, str],
    perturb_pct: float = PLATEAU_PERTURB_PCT,
) -> PlateauResult:
    """Check 17 done properly, and extended to push-mode strategies.

    A real edge shows a broad positive region around the chosen parameters.
    Perturbs every numeric param +-perturb_pct one at a time; if ANY
    neighbour goes non-positive in the SAME period, the base config is a
    needle (a curve-fit spike), not a plateau — regardless of how good the
    base number itself looks.
    """
    base = measure_signal_edge(strategy_name, instrument, *period, base_params, "base")
    neighbours: dict[str, SignalStats] = {}
    failing: list[str] = []

    for key, val in base_params.items():
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        for direction, mult in (("down", 1 - perturb_pct), ("up", 1 + perturb_pct)):
            variant = dict(base_params)
            variant[key] = val * mult
            label = f"{key}_{direction}"
            stat = measure_signal_edge(strategy_name, instrument, *period, variant, label)
            neighbours[label] = stat
            if stat.n > 0 and stat.mean_pts <= 0:
                failing.append(label)

    verdict = "NEEDLE" if failing else "PLATEAU"
    return PlateauResult(base, neighbours, verdict, failing)


@dataclass
class FrictionResult:
    n: int
    gross_per_position: float
    friction_per_position: float
    ratio: float
    verdict: str  # PASS | FAIL | NO_DATA

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "gross_per_position": self.gross_per_position,
            "friction_per_position": self.friction_per_position,
            "ratio": self.ratio, "verdict": self.verdict,
        }


def friction_gate(res: BacktestResult, min_ratio: float = MIN_FRICTION_RATIO) -> FrictionResult:
    """Reject any config whose gross edge per position doesn't clear ~3x friction.

    This is the gate that would have caught the original bug immediately:
    box=20's gross edge was ~0.2x its own friction — no amount of "more
    capital" or "better UI" fixes a ratio that upside down.
    """
    positions = _group_positions(res)
    n = len(positions)
    if n == 0:
        return FrictionResult(0, 0.0, 0.0, 0.0, "NO_DATA")

    gross_total = sum(sum(l.gross_pnl for l in legs) for legs in positions.values())
    friction_total = sum(sum(l.costs + l.slippage_cost for l in legs) for legs in positions.values())
    gross_per_pos = gross_total / n
    friction_per_pos = friction_total / n
    ratio = (gross_per_pos / friction_per_pos) if friction_per_pos else float("inf")

    return FrictionResult(
        n=n, gross_per_position=round(gross_per_pos, 1), friction_per_position=round(friction_per_pos, 1),
        ratio=round(ratio, 2), verdict="PASS" if ratio >= min_ratio else "FAIL",
    )


def friction_gate_futures(res, min_ratio: float = MIN_FRICTION_RATIO) -> FrictionResult:
    """Same gate, for a `FuturesBacktestResult` — one `FuturesTrade` row IS
    one full position already (no legs to group, unlike the options path)."""
    n = len(res.trades)
    if n == 0:
        return FrictionResult(0, 0.0, 0.0, 0.0, "NO_DATA")

    gross_per_pos = sum(t.gross_pnl for t in res.trades) / n
    friction_per_pos = sum(t.costs for t in res.trades) / n
    ratio = (gross_per_pos / friction_per_pos) if friction_per_pos else float("inf")

    return FrictionResult(
        n=n, gross_per_position=round(gross_per_pos, 1), friction_per_position=round(friction_per_pos, 1),
        ratio=round(ratio, 2), verdict="PASS" if ratio >= min_ratio else "FAIL",
    )


@dataclass
class GauntletVerdict:
    """Combines walk-forward, plateau and friction into one promotion decision."""
    walk_forward: WalkForwardResult
    plateau: PlateauResult
    friction: FrictionResult
    promotable: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "walk_forward": self.walk_forward.as_dict(),
            "plateau": self.plateau.as_dict(),
            "friction": self.friction.as_dict(),
            "promotable": self.promotable,
            "reasons": self.reasons,
        }


def run_gauntlet(
    strategy_name: str,
    instrument: str,
    params: dict[str, Any],
    train: tuple[str, str] = ("2021-01-01", "2024-12-31"),
    validate: tuple[str, str] = ("2025-01-01", "2025-12-31"),
    holdout: tuple[str, str] = ("2026-01-01", "2026-09-15"),
    engine_mode: str = "options",
) -> GauntletVerdict:
    """The full research gauntlet for one candidate config. Nothing is called
    "profitable" on the strength of a single backtest run again.

    `engine_mode="futures"` runs the friction gate through the index-proxy
    futures path instead of the options credit-spread wrapper — use this to
    check whether a signal that fails on options (edge swamped by 2-4 legs
    of spread-crossing cost) actually clears the bar once it captures the
    full underlying move instead of an option's partial delta.
    """
    wf = walk_forward_test(strategy_name, instrument, params, train, validate, holdout)
    plateau = plateau_gate(strategy_name, instrument, params, validate)

    strat = get_strategy(strategy_name, params)
    if engine_mode == "futures":
        from vectra_quant.backtest.futures_engine import FuturesBacktestEngine
        val_res = FuturesBacktestEngine().run_strategy(strategy=strat, instrument=instrument, from_date=validate[0], to_date=validate[1])
        friction = friction_gate_futures(val_res)
    else:
        val_res = BacktestEngine().run_strategy(strategy=strat, instrument=instrument, from_date=validate[0], to_date=validate[1])
        friction = friction_gate(val_res)

    reasons = list(wf.reasons)
    if plateau.verdict == "NEEDLE":
        reasons.append(f"parameter neighbourhood is a needle, not a plateau: {plateau.failing}")
    if friction.verdict != "PASS":
        reasons.append(f"gross edge only {friction.ratio}x friction (need >={MIN_FRICTION_RATIO}x)")

    promotable = wf.verdict == "PROMOTABLE" and plateau.verdict == "PLATEAU" and friction.verdict == "PASS"
    if not reasons:
        reasons = ["passes walk-forward significance, plateau, and friction gates"]

    return GauntletVerdict(wf, plateau, friction, promotable, reasons)

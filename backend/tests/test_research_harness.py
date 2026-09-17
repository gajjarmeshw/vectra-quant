"""Tests for the Phase 2 research harness (backtest/research.py).

Most tests here exercise the GATE LOGIC in isolation (via monkeypatched
`measure_signal_edge` or hand-built `SignalStats`/`BacktestResult` fixtures)
so the suite stays fast and deterministic — the harness's whole point is to
be trustworthy plumbing, so its own logic must be pinned without depending
on the (slow, archive-dependent) engine underneath it.

One real end-to-end smoke test at the bottom uses a short window against
the actual archive, skipped if it isn't present.
"""
from __future__ import annotations

import pytest

from vectra_quant.backtest import iea_data, research
from vectra_quant.backtest.engine import BacktestResult, BacktestTrade
from vectra_quant.backtest.research import (
    SignalStats,
    friction_gate,
    plateau_gate,
    run_gauntlet,
    walk_forward_test,
)


def _stats(n, mean_pts, t_stat, label="p"):
    return SignalStats(period_label=label, n=n, mean_pts=mean_pts, stdev_pts=1.0, t_stat=t_stat, hit_rate=50.0)


# ---------------------------------------------------------------- walk_forward_test

def test_walk_forward_rejects_small_sample(monkeypatch):
    monkeypatch.setattr(research, "measure_signal_edge", lambda *a, **k: _stats(10, 5.0, 3.0, k.get("period_label", a[-1] if a else "")))
    result = walk_forward_test("renko_strategy", "NIFTY")
    assert result.verdict == "REJECTED"
    assert any("too small to trust" in r for r in result.reasons)


def test_walk_forward_rejects_non_positive_period(monkeypatch):
    def fake(strategy, inst, from_d, to_d, params, label):
        return _stats(50, -3.0, -2.5, label) if label == "validate" else _stats(50, 10.0, 3.0, label)
    monkeypatch.setattr(research, "measure_signal_edge", fake)
    result = walk_forward_test("renko_strategy", "NIFTY")
    assert result.verdict == "REJECTED"
    assert any("non-positive" in r for r in result.reasons)


def test_walk_forward_rejects_when_not_significant(monkeypatch):
    """All positive, but neither validate nor holdout clears t>=2 — must not promote."""
    monkeypatch.setattr(research, "measure_signal_edge", lambda strategy, inst, f, t, params, label: _stats(40, 5.0, 1.5, label))
    result = walk_forward_test("renko_strategy", "NIFTY")
    assert result.verdict == "REJECTED"
    assert any("indistinguishable from luck" in r for r in result.reasons)


def test_walk_forward_promotes_when_evidence_is_real(monkeypatch):
    def fake(strategy, inst, f, t, params, label):
        return {"train": _stats(150, 8.0, 2.5, label), "validate": _stats(50, 6.0, 2.2, label), "holdout": _stats(40, 7.0, 2.1, label)}[label]
    monkeypatch.setattr(research, "measure_signal_edge", fake)
    result = walk_forward_test("renko_strategy", "NIFTY")
    assert result.verdict == "PROMOTABLE"


# ---------------------------------------------------------------- plateau_gate

def test_plateau_gate_flags_needle(monkeypatch):
    """Base config positive, but a neighbour goes negative -> NEEDLE, matching
    the exact failure mode found in box=75 (an isolated spike)."""
    def fake(strategy, inst, f, t, params, label):
        if label == "base":
            return _stats(30, 20.0, 2.0, label)
        if "box_min_up" in label:
            return _stats(30, -5.0, -1.0, label)  # neighbour fails
        return _stats(30, 8.0, 1.8, label)
    monkeypatch.setattr(research, "measure_signal_edge", fake)
    result = plateau_gate("renko_strategy", "NIFTY", {"box_min": 75.0, "sl_pct_premium": 0.3}, ("2025-01-01", "2025-12-31"))
    assert result.verdict == "NEEDLE"
    assert "box_min_up" in result.failing


def test_plateau_gate_passes_broad_region(monkeypatch):
    monkeypatch.setattr(research, "measure_signal_edge", lambda strategy, inst, f, t, params, label: _stats(30, 10.0, 2.0, label))
    result = plateau_gate("renko_strategy", "NIFTY", {"box_min": 65.0}, ("2025-01-01", "2025-12-31"))
    assert result.verdict == "PLATEAU"
    assert result.failing == []


def test_plateau_gate_only_perturbs_numeric_params(monkeypatch):
    calls = []
    def fake(strategy, inst, f, t, params, label):
        calls.append(label)
        return _stats(30, 5.0, 2.0, label)
    monkeypatch.setattr(research, "measure_signal_edge", fake)
    plateau_gate("renko_strategy", "NIFTY", {"box_min": 65.0, "name": "not_numeric", "enabled": True}, ("2025-01-01", "2025-12-31"))
    assert not any("name_" in c for c in calls)
    assert not any("enabled_" in c for c in calls)  # bool must not be treated as numeric


# ---------------------------------------------------------------- friction_gate

def _fake_trade(position_id, gross, costs, slip, direction="PE"):
    return BacktestTrade(
        id=position_id, symbol="NIFTY 24000 " + direction, instrument="NIFTY", direction=direction,
        opened_at="2025-01-01 09:30", closed_at="2025-01-01 10:00", qty=65,
        entry_price=100.0, exit_price=100.0, gross_pnl=gross, costs=costs, net_pnl=gross - costs,
        exit_reason="STOP", win=gross > 0, slippage_cost=slip, position_id=position_id,
    )


def _fake_result(trades):
    return BacktestResult(
        instrument="NIFTY", strategy_name="renko_strategy", days=1, start_date="", end_date="",
        initial_capital=120000.0, final_pnl=0.0, gross_pnl=0.0, total_costs=0.0, total_trades=0,
        wins=0, losses=0, win_pct=0.0, profit_factor=0.0, max_drawdown=0.0, trades=trades,
    )


def test_friction_gate_fails_when_edge_is_swamped_by_friction():
    """Reproduces the exact box=20 finding: ~86 gross vs ~470 friction per position."""
    trades = [_fake_trade("p1", 86.0, 158.0, 312.0)]
    result = friction_gate(_fake_result(trades))
    assert result.verdict == "FAIL"
    assert result.ratio < 1.0


def test_friction_gate_passes_when_edge_comfortably_clears_friction():
    trades = [_fake_trade(f"p{i}", 1500.0, 158.0, 312.0) for i in range(5)]
    result = friction_gate(_fake_result(trades))
    assert result.verdict == "PASS"
    assert result.ratio >= research.MIN_FRICTION_RATIO


def test_friction_gate_no_data():
    result = friction_gate(_fake_result([]))
    assert result.verdict == "NO_DATA"
    assert result.n == 0


def test_friction_gate_does_not_double_count_multi_leg_position():
    """Two legs of the SAME position (shared position_id) must combine, not
    be treated as two separate positions."""
    trades = [
        _fake_trade("p1", 1000.0, 80.0, 150.0, direction="PE"),
        _fake_trade("p1", 200.0, 78.0, 150.0, direction="CE"),
    ]
    result = friction_gate(_fake_result(trades))
    assert result.n == 1
    assert result.gross_per_position == pytest.approx(1200.0)


# ---------------------------------------------------------------- run_gauntlet (logic wiring only)

def test_run_gauntlet_rejects_when_any_gate_fails(monkeypatch):
    monkeypatch.setattr(research, "walk_forward_test", lambda *a, **k: research.WalkForwardResult(_stats(50, 5, 3), _stats(50, 5, 3), _stats(50, 5, 3), "PROMOTABLE", ["ok"]))
    monkeypatch.setattr(research, "plateau_gate", lambda *a, **k: research.PlateauResult(_stats(50, 5, 3), {}, "NEEDLE", ["box_min_up"]))
    monkeypatch.setattr(research, "friction_gate", lambda *a, **k: research.FrictionResult(50, 1500.0, 300.0, 5.0, "PASS"))

    class _FakeStrat:
        name = "renko_strategy"

    monkeypatch.setattr(research, "get_strategy", lambda *a, **k: _FakeStrat())

    class _FakeEngine:
        def run_strategy(self, **kwargs):
            return _fake_result([])
    monkeypatch.setattr(research, "BacktestEngine", lambda: _FakeEngine())

    verdict = run_gauntlet("renko_strategy", "NIFTY", {"box_min": 75.0})
    assert verdict.promotable is False
    assert any("needle" in r for r in verdict.reasons)


# ---------------------------------------------------------------- real smoke test

@pytest.mark.skipif(not iea_data.AVAILABLE, reason="IEA archive not present on this machine")
def test_measure_signal_edge_runs_against_real_archive():
    stats = research.measure_signal_edge("renko_strategy", "NIFTY", "2026-08-01", "2026-08-15", period_label="smoke")
    assert stats.period_label == "smoke"
    assert stats.n >= 0  # may legitimately be 0 signals in a 2-week window

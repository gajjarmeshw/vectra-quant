"""Route tests for the job-based backtest API (backtest/jobs.py + api/routes.py).

`/strategies/backtest` used to run the whole simulation synchronously inside
an `async def` route, blocking the entire asyncio event loop — freezing
/live and every other request for the duration. `POST /backtest/runs` fixes
that by handing the work to a dedicated worker thread and returning
immediately with a job id; these are the first route-level tests in this
project (previously only library-level engine calls were tested).

Uses a minimal FastAPI app (router only) rather than the full `create_app()`,
since the real app's startup touches the live broker/network — not something
a unit test should depend on.
"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vectra_quant.api.routes import router
from vectra_quant.backtest import iea_data
from vectra_quant.backtest.jobs import BacktestJobRunner

pytestmark = pytest.mark.skipif(not iea_data.AVAILABLE, reason="IEA archive not present on this machine")


class _FakeSecrets:
    api_shared_secret = ""  # empty -> require_secret treats this as local dev, no header needed


class _FakeSettings:
    secrets = _FakeSecrets()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    app.state.settings = _FakeSettings()
    app.state.backtest_jobs = BacktestJobRunner(broadcast=None)
    return TestClient(app)


def _wait_for_terminal(client, job_id: str, timeout_s: float = 30.0) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = client.get(f"/backtest/runs/{job_id}")
        assert r.status_code == 200
        data = r.json()
        if data["status"] in ("DONE", "FAILED", "CANCELLED", "ABORTED"):
            return data
        time.sleep(0.1)
    pytest.fail(f"job {job_id} did not reach a terminal state within {timeout_s}s")


def test_submit_returns_immediately_and_completes(client):
    t0 = time.time()
    r = client.post("/backtest/runs", json={
        "strategy": "renko_strategy", "instrument": "NIFTY",
        "from_date": "2026-08-01", "to_date": "2026-08-10", "wiggle_test": False,
    })
    submit_time = time.time() - t0
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # The whole point: submission must not block on the backtest itself.
    assert submit_time < 2.0

    data = _wait_for_terminal(client, job_id)
    assert data["status"] == "DONE"
    assert data["result"] is not None
    assert "total_trades" in data["result"]


def test_event_loop_not_blocked_during_run(client):
    """The literal bug report: other requests must stay responsive while a job runs.

    wiggle_test=True on Renko now runs baseline + 2 variants per numeric
    param (9 params -> 19 full sub-runs), so the window here is deliberately
    short — this test is about responsiveness, not about exercising every
    wiggle variant end to end.
    """
    r = client.post("/backtest/runs", json={
        "strategy": "renko_strategy", "instrument": "NIFTY",
        "from_date": "2026-08-01", "to_date": "2026-08-07", "wiggle_test": True,
    })
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    # Fire several unrelated requests while the job is (presumably) still running.
    for _ in range(5):
        t0 = time.time()
        resp = client.get(f"/backtest/runs")
        assert resp.status_code == 200
        assert (time.time() - t0) < 1.0  # must not queue behind the running job
        time.sleep(0.2)

    _wait_for_terminal(client, job_id, timeout_s=180.0)


def test_concurrent_submit_returns_409(client):
    # No wiggle here — this test only needs job1 to still be RUNNING when job2
    # is submitted a fraction of a second later; ~2 months is comfortably
    # long enough for that margin without paying for a full year's worth of
    # bars (cancel_check is polled per-session regardless of range, so a
    # smaller range doesn't weaken what this test is actually checking).
    r1 = client.post("/backtest/runs", json={
        "strategy": "renko_strategy", "instrument": "NIFTY",
        "from_date": "2025-01-01", "to_date": "2025-02-28", "wiggle_test": False,
    })
    assert r1.status_code == 202
    job1 = r1.json()["job_id"]

    r2 = client.post("/backtest/runs", json={
        "strategy": "renko_strategy", "instrument": "NIFTY",
        "from_date": "2026-08-01", "to_date": "2026-08-10", "wiggle_test": False,
    })
    assert r2.status_code == 409

    client.post(f"/backtest/runs/{job1}/cancel")
    _wait_for_terminal(client, job1, timeout_s=30.0)


def test_cancel_stops_a_running_job(client):
    # cancel_check is polled once per session, so cancellation speed doesn't
    # depend on the total date range -- a much smaller range (with wiggle
    # still on, to prove cancellation works across sub-runs too) exercises
    # the identical cancel path in a fraction of the wall-clock time a
    # multi-year + wiggle sweep previously took.
    r = client.post("/backtest/runs", json={
        "strategy": "renko_strategy", "instrument": "NIFTY",
        "from_date": "2026-06-01", "to_date": "2026-06-30", "wiggle_test": True,
    })
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    time.sleep(0.3)  # let it actually start
    cancel_resp = client.post(f"/backtest/runs/{job_id}/cancel")
    assert cancel_resp.status_code == 200

    data = _wait_for_terminal(client, job_id, timeout_s=30.0)
    assert data["status"] == "CANCELLED"


def test_unknown_strategy_rejected_before_queuing(client):
    r = client.post("/backtest/runs", json={"strategy": "not_a_real_strategy", "days": 5})
    assert r.status_code == 400


def test_unknown_job_id_returns_404(client):
    r = client.get("/backtest/runs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_history_lists_completed_runs(client):
    r = client.post("/backtest/runs", json={
        "strategy": "renko_strategy", "instrument": "NIFTY",
        "from_date": "2026-08-01", "to_date": "2026-08-05", "wiggle_test": False,
    })
    job_id = r.json()["job_id"]
    _wait_for_terminal(client, job_id)

    hist = client.get("/backtest/runs").json()["runs"]
    assert any(row["id"] == job_id for row in hist)


def test_restart_sweep_marks_stale_jobs_aborted():
    """Simulates a --reload restart: a row left QUEUED/RUNNING must not stay stuck forever."""
    from vectra_quant.db import BacktestRun, session

    with session() as s:
        s.add(BacktestRun(id="stale-job-1", status="RUNNING", strategy="renko_strategy", instrument="NIFTY"))

    runner = BacktestJobRunner(broadcast=None)
    runner.sweep_stale_on_startup()

    row = runner.get_run_row("stale-job-1")
    assert row["status"] == "ABORTED"

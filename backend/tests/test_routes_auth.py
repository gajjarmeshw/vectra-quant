"""Route-level tests for auth (`require_secret`) and `/health` reflecting
real readiness -- both added in the production-readiness pass. `api/routes.py`
had no direct test file before this.

Uses a minimal FastAPI app (router only), same pattern as test_backtest_api.py,
since the real app's startup touches the live broker/network.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vectra_quant.api.routes import router


class _FakeKillswitch:
    is_on = True


class _FakeBroker:
    name = "fake"


class _FakeSecrets:
    def __init__(self, api_shared_secret: str = ""):
        self.api_shared_secret = api_shared_secret
        self.vapid_public = "fake-vapid-public-key"


class _FakeFeed:
    _last_tick_time: dict = {}


class _FakeCandles:
    _bars_1m: dict = {}


class _FakeAlgoCfg:
    active_strategies = ["renko_strategy"]
    enabled = True
    auto_execute = False


class _FakeSettings:
    mode = "PAPER"
    algo = _FakeAlgoCfg()

    def __init__(self, api_shared_secret: str = ""):
        self.secrets = _FakeSecrets(api_shared_secret)


def _make_app(api_shared_secret: str = "", ready: bool = True, boot_error: str = ""):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = _FakeSettings(api_shared_secret)
    app.state.ready = ready
    app.state.boot_error = boot_error
    app.state.broker = _FakeBroker()
    app.state.killswitch = _FakeKillswitch()
    app.state.feed = _FakeFeed()
    app.state.candles = _FakeCandles()
    return app


# --------------------------------------------------------------- require_secret

def test_empty_secret_is_local_dev_bypass():
    client = TestClient(_make_app(api_shared_secret=""))
    r = client.get("/strategies")
    assert r.status_code == 200


def test_dev_insecure_secret_is_bypass():
    client = TestClient(_make_app(api_shared_secret="dev-insecure"))
    r = client.get("/strategies")
    assert r.status_code == 200


def test_real_secret_rejects_missing_header():
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.get("/strategies")
    assert r.status_code == 401


def test_real_secret_rejects_wrong_header():
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.get("/strategies", headers={"X-VectraQuant-Key": "wrong"})
    assert r.status_code == 401


def test_real_secret_accepts_correct_header():
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.get("/strategies", headers={"X-VectraQuant-Key": "a-real-secret-value"})
    assert r.status_code == 200


def test_data_status_requires_secret():
    """Previously unauthenticated -- also verifies the removed dhan_token_snippet field is gone."""
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.get("/data/status")
    assert r.status_code == 401


def test_broker_refresh_token_requires_secret():
    """Previously unauthenticated -- this endpoint can overwrite the live broker token."""
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.post("/broker/refresh-token", json={"access_token": "x"})
    assert r.status_code == 401


def test_webhooks_signal_requires_secret():
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.post("/webhooks/signal", json={"symbol": "NIFTY"})
    assert r.status_code == 401


def test_backtest_runs_list_requires_secret():
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.get("/backtest/runs")
    assert r.status_code == 401


def test_push_key_stays_public():
    """VAPID public key is meant to be public -- not a real secret."""
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.get("/push/key")
    assert r.status_code == 200


# --------------------------------------------------------------- /health

def test_health_ok_when_ready():
    client = TestClient(_make_app(ready=True))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["boot_error"] is None


def test_health_503_when_not_ready():
    client = TestClient(_make_app(ready=False, boot_error="broker auth failed"))
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["ok"] is False
    assert r.json()["boot_error"] == "broker auth failed"


def test_health_is_itself_unauthenticated():
    """Health checks must be reachable without the shared secret."""
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value", ready=True))
    r = client.get("/health")
    assert r.status_code == 200


# --------------------------------------------------------------- /orderflow/thunderbolt/status

def test_thunderbolt_status_requires_secret():
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.get("/orderflow/thunderbolt/status")
    assert r.status_code == 401


def test_thunderbolt_status_returns_nulls_when_files_missing(tmp_path, monkeypatch):
    """Before the standalone live script has run today (or on a machine
    that's never run it), every field must come back null, not 500 -- this
    endpoint reads a different process's output files, which may not exist."""
    monkeypatch.setenv("ORDERFLOW_RECORDS_ROOT", str(tmp_path / "nonexistent"))
    client = TestClient(_make_app(api_shared_secret=""))
    r = client.get("/orderflow/thunderbolt/status")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["recorder_health"] is None
    assert body["live_status"] is None
    assert body["record"] is None
    assert body["context"] is None


def test_thunderbolt_status_reads_real_files(tmp_path, monkeypatch):
    import json as _json

    from vectra_quant.core.session_clock import session_date

    root = tmp_path / "orderflow"
    monkeypatch.setenv("ORDERFLOW_RECORDS_ROOT", str(root))
    # The route resolves "today" via session_date() (IST-aware), not naive
    # date.today() (reads UTC on this host) -- those two disagree for the
    # first 5.5 hours of every IST day (00:00-05:29 IST is still "yesterday"
    # in UTC), which silently broke this exact test during that window.
    today = session_date()

    health_dir = root / "recorder_health"
    health_dir.mkdir(parents=True)
    (health_dir / "health.json").write_text(_json.dumps({"connected": True, "reconnect_count": 3}))

    tb_dir = root / "thunderbolt" / f"date={today}"
    tb_dir.mkdir(parents=True)
    (tb_dir / "live_status.json").write_text(_json.dumps({"trace": {"final": "SKIP"}}))
    (tb_dir / "record.json").write_text(_json.dumps({"skipped": False}))

    client = TestClient(_make_app(api_shared_secret=""))
    r = client.get("/orderflow/thunderbolt/status")
    assert r.status_code == 200
    body = r.json()
    assert body["recorder_health"]["reconnect_count"] == 3
    assert body["live_status"]["trace"]["final"] == "SKIP"
    assert body["record"]["skipped"] is False
    assert body["context"] is None  # not written in this test


# --------------------------------------------------------------- /orderflow/breadth/status

def test_breadth_status_requires_secret():
    client = TestClient(_make_app(api_shared_secret="a-real-secret-value"))
    r = client.get("/orderflow/breadth/status")
    assert r.status_code == 401


def test_breadth_status_returns_nulls_when_files_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDERFLOW_RECORDS_ROOT", str(tmp_path / "nonexistent"))
    client = TestClient(_make_app(api_shared_secret=""))
    r = client.get("/orderflow/breadth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["recorder_health"] == {"equities_conn0": None, "equities_conn1": None, "options_conn0": None}
    assert body["live_status"] is None
    assert body["record"] is None


def test_breadth_status_reads_real_files(tmp_path, monkeypatch):
    import json as _json

    from vectra_quant.core.session_clock import session_date

    root = tmp_path / "orderflow"
    monkeypatch.setenv("ORDERFLOW_RECORDS_ROOT", str(root))
    today = session_date()  # see test_thunderbolt_status_reads_real_files's comment on why not date.today()

    health_dir = root / "recorder_health"
    health_dir.mkdir(parents=True)
    (health_dir / "health_equities_conn0.json").write_text(_json.dumps({"connected": True, "n_instruments": 50}))
    (health_dir / "health_equities_conn1.json").write_text(_json.dumps({"connected": True, "n_instruments": 50}))
    # options_conn0 health file intentionally not written -- must come back null, not error

    b_dir = root / "breadth" / f"date={today}"
    b_dir.mkdir(parents=True)
    (b_dir / "live_status.json").write_text(_json.dumps({"mean_breadth": 0.03, "n_stocks_reporting": 87}))
    (b_dir / "record.json").write_text(_json.dumps({"skipped": False}))

    client = TestClient(_make_app(api_shared_secret=""))
    r = client.get("/orderflow/breadth/status")
    assert r.status_code == 200
    body = r.json()
    assert body["recorder_health"]["equities_conn0"]["n_instruments"] == 50
    assert body["recorder_health"]["equities_conn1"]["n_instruments"] == 50
    assert body["recorder_health"]["options_conn0"] is None
    assert body["live_status"]["n_stocks_reporting"] == 87
    assert body["record"]["skipped"] is False

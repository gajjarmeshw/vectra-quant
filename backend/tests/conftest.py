"""Shared fixtures. Every test gets an isolated SQLite file — no shared state."""
from __future__ import annotations

import pytest
from sentinel import db as db_mod


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point the DB at a fresh file per test and reset the cached engine."""
    path = tmp_path / "test.db"
    monkeypatch.setattr(db_mod, "DB_PATH", path)
    monkeypatch.setattr(db_mod, "_engine", None, raising=False)
    monkeypatch.setattr(db_mod, "_SessionLocal", None, raising=False)
    db_mod.init_db()
    yield path
    monkeypatch.setattr(db_mod, "_engine", None, raising=False)
    monkeypatch.setattr(db_mod, "_SessionLocal", None, raising=False)


@pytest.fixture
def cfg():
    """The user's live parameters, with expiry parity per D-009."""
    from sentinel.risk_engine import RiskConfig
    return RiskConfig(expiry_risk_scale=1.0)

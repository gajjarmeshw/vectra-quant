"""SQLite persistence. Build plan Phase 0 §2, design doc §5.1.

Single file, WAL mode. Tables: ticks_1m, chain_snapshots, events, suggestions,
trades, pnl_curve, violations, llm_calls, system_state.

WAL matters for a live trading loop: the FSM writer and the API readers must not
block each other, and a crash mid-write must not corrupt the day's journal.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from sentinel.config import ROOT

DB_PATH = Path(os.getenv("SENTINEL_DB", ROOT / "data" / "sentinel.db"))


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Tick1m(Base):
    __tablename__ = "ticks_1m"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(48), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (Index("ix_ticks_symbol_ts", "symbol", "ts", unique=True),)


class ChainSnapshot(Base):
    __tablename__ = "chain_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    instrument: Mapped[str] = mapped_column(String(16), index=True)
    expiry: Mapped[str] = mapped_column(String(16))
    strike: Mapped[float] = mapped_column(Float)
    side: Mapped[str] = mapped_column(String(2))  # CE | PE
    ltp: Mapped[float] = mapped_column(Float)
    oi: Mapped[float] = mapped_column(Float, default=0.0)
    oi_change_pct: Mapped[float] = mapped_column(Float, default=0.0)
    iv: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (Index("ix_chain_lookup", "instrument", "ts", "strike", "side"),)


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    instrument: Mapped[str] = mapped_column(String(16), default="")
    detail: Mapped[str] = mapped_column(Text, default="{}")
    snapshot: Mapped[str] = mapped_column(Text, default="{}")
    llm_dispatched: Mapped[bool] = mapped_column(Boolean, default=False)


class Suggestion(Base):
    __tablename__ = "suggestions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    event_kind: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(16))  # SUGGEST | NO_TRADE
    instrument: Mapped[str] = mapped_column(String(16), default="")
    direction: Mapped[str] = mapped_column(String(2), default="")
    strike_offset: Mapped[str] = mapped_column(String(8), default="")
    resolved_strike: Mapped[float] = mapped_column(Float, default=0.0)
    trading_symbol: Mapped[str] = mapped_column(String(64), default="")
    entry_low: Mapped[float] = mapped_column(Float, default=0.0)
    entry_high: Mapped[float] = mapped_column(Float, default=0.0)
    stop_loss_premium: Mapped[float] = mapped_column(Float, default=0.0)
    target_premium: Mapped[float] = mapped_column(Float, default=0.0)
    time_stop_minutes: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    thesis: Mapped[str] = mapped_column(Text, default="")
    invalidation: Mapped[str] = mapped_column(Text, default="")
    risk_reward: Mapped[float] = mapped_column(Float, default=0.0)
    # engine-computed, never from the LLM
    lots: Mapped[int] = mapped_column(Integer, default=0)
    rupee_risk: Mapped[float] = mapped_column(Float, default=0.0)
    position_cost: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", index=True)
    # QUEUED | TAKEN | REJECTED | EXPIRED | GATED | NO_TRADE
    gate_reason: Mapped[str] = mapped_column(String(128), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    outcome: Mapped[str] = mapped_column(String(16), default="")  # WIN | LOSS | FLAT


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # client uuid
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD IST
    origin: Mapped[str] = mapped_column(String(8), default="ai")  # ai | manual
    suggestion_id: Mapped[str | None] = mapped_column(ForeignKey("suggestions.id"), nullable=True)
    instrument: Mapped[str] = mapped_column(String(16), default="")
    trading_symbol: Mapped[str] = mapped_column(String(64), default="")
    exchange: Mapped[str] = mapped_column(String(8), default="")
    segment: Mapped[str] = mapped_column(String(8), default="FNO")
    direction: Mapped[str] = mapped_column(String(2), default="")
    lots: Mapped[int] = mapped_column(Integer, default=0)
    lot_size: Mapped[int] = mapped_column(Integer, default=0)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_price: Mapped[float] = mapped_column(Float, default=0.0)
    sl_price: Mapped[float] = mapped_column(Float, default=0.0)
    target_price: Mapped[float] = mapped_column(Float, default=0.0)
    entry_order_id: Mapped[str] = mapped_column(String(64), default="")
    sl_order_id: Mapped[str] = mapped_column(String(64), default="")
    broker_order_ids: Mapped[str] = mapped_column(Text, default="[]")
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    costs: Mapped[float] = mapped_column(Float, default=0.0)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)  # net, feeds the FSM
    exit_reason: Mapped[str] = mapped_column(String(32), default="")
    guardian_note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)


class PnlCurve(Base):
    __tablename__ = "pnl_curve"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    realized: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized: Mapped[float] = mapped_column(Float, default=0.0)
    day_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    peak: Mapped[float] = mapped_column(Float, default=0.0)
    floor: Mapped[float] = mapped_column(Float, default=0.0)
    state: Mapped[str] = mapped_column(String(12), default="NORMAL")


class Violation(Base):
    __tablename__ = "violations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(Text, default="")
    rupee_cost: Mapped[float] = mapped_column(Float, default=0.0)
    trade_id: Mapped[str | None] = mapped_column(ForeignKey("trades.id"), nullable=True)


class LlmCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    session_date: Mapped[str] = mapped_column(String(10), index=True)
    purpose: Mapped[str] = mapped_column(String(32), default="suggestion")
    provider: Mapped[str] = mapped_column(String(16), default="")  # bridge | groq
    model: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    event_kind: Mapped[str] = mapped_column(String(32), default="")


class SystemState(Base):
    """Crash-safe key/value. FSM state, floors, kill switch, drill results."""
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushSub(Base):
    __tablename__ = "push_subscriptions"
    endpoint: Mapped[str] = mapped_column(String(512), primary_key=True)
    p256dh: Mapped[str] = mapped_column(String(256))
    auth: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def engine():
    global _engine
    if _engine is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{DB_PATH}",
            future=True,
            # SQLite + a threaded server: allow cross-thread use, serialise via pool.
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(_engine, "connect")
        def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return _engine


def session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session() -> Iterator[Session]:
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    Base.metadata.create_all(engine())
    with engine().connect() as c:
        mode = c.execute(text("PRAGMA journal_mode")).scalar()
    if str(mode).lower() != "wal":  # pragma: no cover - defensive
        raise RuntimeError(f"WAL not enabled (got {mode!r})")


# ------------------------------------------------------------------ state helpers
def state_get(key: str, default: str = "") -> str:
    with session() as s:
        row = s.get(SystemState, key)
        return row.value if row else default


def state_set(key: str, value: str) -> None:
    with session() as s:
        row = s.get(SystemState, key)
        if row:
            row.value = value
            row.updated_at = utcnow()
        else:
            s.add(SystemState(key=key, value=value, updated_at=utcnow()))

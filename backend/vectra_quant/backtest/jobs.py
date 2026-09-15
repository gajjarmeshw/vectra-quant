"""Background job runner for backtests.

Backtests are CPU-bound and can take several seconds. Running one
synchronously inside an `async def` FastAPI route blocks the whole asyncio
event loop for its full duration — freezing the `/live` WebSocket, health
checks, everything. That was the actual cause behind "the app gets stuck"
when running a backtest.

This module submits a backtest to a dedicated single-worker thread pool,
tracks its progress in memory and in the `backtest_runs` table (so results
survive a page refresh and a restart doesn't leave a job stuck "running"
forever), and broadcasts progress over the existing `/live` WebSocket so the
PWA can show a live bar instead of a frozen button.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from vectra_quant.backtest.engine import BacktestCancelled
from vectra_quant.db import BacktestRun, session, utcnow
from vectra_quant.logging_setup import get

log = get("backtest.jobs")

MAX_HISTORY = 50
_EMIT_THROTTLE_S = 0.25


@dataclass
class JobHandle:
    id: str
    strategy: str
    instrument: str
    status: str = "QUEUED"  # QUEUED | RUNNING | DONE | FAILED | CANCELLED
    phase: str = "QUEUED"
    pct: float = 0.0
    sessions_done: int = 0
    sessions_total: int = 0
    session_date: str = ""
    started_at: float = field(default_factory=time.time)
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _last_emit_ts: float = field(default=0.0, repr=False)
    _last_emit_phase: str = field(default="", repr=False)

    def as_progress_payload(self) -> dict[str, Any]:
        return {
            "type": "backtest",
            "job_id": self.id,
            "status": self.status,
            "phase": self.phase,
            "pct": round(self.pct, 1),
            "sessions_done": self.sessions_done,
            "sessions_total": self.sessions_total,
            "session_date": self.session_date,
            "elapsed_s": round(time.time() - self.started_at, 1),
            "strategy": self.strategy,
            "instrument": self.instrument,
            "error": self.error,
        }


class BacktestJobRunner:
    """Owns the single-worker executor + in-memory job registry for one app instance."""

    def __init__(self, broadcast: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backtest-job")
        self._jobs: dict[str, JobHandle] = {}
        self._lock = threading.Lock()
        self._broadcast = broadcast
        self._active_id: str | None = None

    def sweep_stale_on_startup(self) -> None:
        """Mark QUEUED/RUNNING rows left over from a previous process (e.g. --reload) as ABORTED."""
        with session() as s:
            stale = s.query(BacktestRun).filter(BacktestRun.status.in_(("QUEUED", "RUNNING"))).all()
            for row in stale:
                row.status = "ABORTED"
                row.error = "Server restarted while this run was in progress."
                row.finished_at = utcnow()
            if stale:
                log.warning(f"Marked {len(stale)} stale backtest run(s) as ABORTED on startup")

    def submit(
        self,
        strategy: str,
        instrument: str,
        run_fn: Callable[[JobHandle], dict[str, Any]],
        meta: dict[str, Any],
    ) -> str:
        """Queue a backtest job. Raises RuntimeError if one is already running (single-worker pool)."""
        with self._lock:
            if self._active_id is not None:
                active = self._jobs.get(self._active_id)
                if active and active.status in ("QUEUED", "RUNNING"):
                    raise RuntimeError("A backtest is already running")

            job_id = str(uuid.uuid4())
            job = JobHandle(id=job_id, strategy=strategy, instrument=instrument)
            self._jobs[job_id] = job
            self._active_id = job_id

        with session() as s:
            s.add(BacktestRun(
                id=job_id,
                status="QUEUED",
                strategy=strategy,
                instrument=instrument,
                days=meta.get("days") or 0,
                from_date=meta.get("from_date") or "",
                to_date=meta.get("to_date") or "",
                wiggle_test=bool(meta.get("wiggle_test", True)),
                params_json=json.dumps(meta.get("params") or {}),
            ))

        self._trim_history()
        self._pool.submit(self._run, job, run_fn)
        self._emit(job, force=True)
        return job_id

    def _run(self, job: JobHandle, run_fn: Callable[[JobHandle], dict[str, Any]]) -> None:
        job.status = "RUNNING"
        job.phase = "STARTING"
        self._update_row(job, status="RUNNING")
        self._emit(job, force=True)
        t0 = time.time()
        try:
            result = run_fn(job)
            job.status = "DONE"
            job.result = result
            job.pct = 100.0
            job.phase = "DONE"
        except BacktestCancelled:
            job.status = "CANCELLED"
            job.phase = "CANCELLED"
        except Exception as exc:  # noqa: BLE001 - must never let a job vanish silently
            job.status = "FAILED"
            job.error = f"{exc}\n{traceback.format_exc()[-2000:]}"
            log.error(f"Backtest job {job.id} failed: {exc}", exc_info=True)
        finally:
            duration_ms = int((time.time() - t0) * 1000)
            self._update_row(
                job,
                status=job.status,
                result_json=json.dumps(job.result) if job.result else "",
                error=job.error or "",
                duration_ms=duration_ms,
                finished=True,
            )
            with self._lock:
                if self._active_id == job.id:
                    self._active_id = None
            self._emit(job, force=True)

    def _update_row(self, job: JobHandle, *, finished: bool = False, **fields: Any) -> None:
        with session() as s:
            row = s.get(BacktestRun, job.id)
            if not row:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            if finished:
                row.finished_at = utcnow()

    def _trim_history(self) -> None:
        with session() as s:
            rows = (
                s.query(BacktestRun)
                .order_by(BacktestRun.ts.desc())
                .offset(MAX_HISTORY)
                .all()
            )
            for r in rows:
                s.delete(r)

    def report_progress(self, job: JobHandle, phase: str, stage_idx: int, stage_count: int,
                         done: int, total: int, session_date: str) -> None:
        """Called from the worker thread by the engine's `progress` callback."""
        overall_pct = ((stage_idx + (done / max(total, 1))) / max(stage_count, 1)) * 100.0
        job.phase = phase
        job.pct = round(min(99.9, overall_pct), 1)  # 100 reserved for actual completion
        job.sessions_done = done
        job.sessions_total = total
        job.session_date = session_date
        self._emit(job)

    def _emit(self, job: JobHandle, force: bool = False) -> None:
        now = time.time()
        if not force and (now - job._last_emit_ts) < _EMIT_THROTTLE_S and job.phase == job._last_emit_phase:
            return
        job._last_emit_ts = now
        job._last_emit_phase = job.phase
        if self._broadcast:
            try:
                self._broadcast(job.as_progress_payload())
            except Exception:
                log.warning("Failed to broadcast backtest progress", exc_info=True)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def get(self, job_id: str) -> JobHandle | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("QUEUED", "RUNNING"):
            return False
        job.cancel_event.set()
        return True

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with session() as s:
            rows = s.query(BacktestRun).order_by(BacktestRun.ts.desc()).limit(limit).all()
            return [_row_to_dict(r) for r in rows]

    def get_run_row(self, job_id: str) -> dict[str, Any] | None:
        with session() as s:
            row = s.get(BacktestRun, job_id)
            return _row_to_dict(row) if row else None


def _row_to_dict(row: BacktestRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "ts": row.ts.isoformat() if row.ts else None,
        "status": row.status,
        "strategy": row.strategy,
        "instrument": row.instrument,
        "days": row.days,
        "from_date": row.from_date,
        "to_date": row.to_date,
        "wiggle_test": row.wiggle_test,
        "result": json.loads(row.result_json) if row.result_json else None,
        "error": row.error,
        "duration_ms": row.duration_ms,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }

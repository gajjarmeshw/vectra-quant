"""Live paper-trading loop for the cross-sectional breadth signal.

Polls two (or more) `MultiInstrumentDepthRecorder` top-of-book snapshots
covering the NIFTY100 universe, computes market-wide breadth every
`cfg.bucket_seconds`, detects threshold crossings, and paper-trades a
single-leg ATM option in the signal direction. Persists one JSON
decision+position record per day (same idempotency pattern as
`paper_trader.py`: a restart mid-day is a no-op if today already has a
call recorded) plus a live_status.json updated every poll cycle so the UI
can show *why* it hasn't fired, not just that it hasn't.

NOT independently tested against a live feed -- cannot be without real
Dhan credentials and market hours. Everything it calls into (breadth_signal,
breadth_strategy, multi_recorder) IS unit tested; this is the orchestration
glue, same boundary `paper_trader.py` already draws.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable

from vectra_quant.core.session_clock import now_ist
from vectra_quant.orderflow.breadth_signal import SignalDirection, compute_breadth, detect_breadth_crossing, per_stock_imbalance
from vectra_quant.orderflow.breadth_strategy import BreadthLeg, build_breadth_structure, exit_reason
from vectra_quant.orderflow.config import BreadthCfg

log = logging.getLogger("orderflow.breadth_paper_trader")


@dataclass
class BreadthPaperPosition:
    entry_ts: str
    direction: str
    leg: dict[str, Any]
    entry_premium: float
    exit_ts: str | None = None
    exit_premium: float | None = None
    exit_reason: str | None = None
    net_pnl: float | None = None
    status: str = "OPEN"


@dataclass
class BreadthDailyRecord:
    session_date: str
    skipped: bool = False
    skip_reason: str = ""
    decision: dict[str, Any] | None = None
    position: dict[str, Any] | None = None


class BreadthPaperTrader:
    def __init__(
        self,
        top_of_book_providers: list[Callable[[], dict[str, tuple[float, float]]]],
        quote_fn: Callable[[str], float],
        instrument_resolver_fn: Callable[[str, float], str],
        spot_fn: Callable[[], float],
        cfg: BreadthCfg,
        records_root: str,
        lot_size: int = 65,
    ):
        self._top_of_book_providers = top_of_book_providers
        self._quote_fn = quote_fn
        self._resolve_fn = instrument_resolver_fn
        self._spot_fn = spot_fn
        self.cfg = cfg
        self.records_root = Path(records_root)
        self.lot_size = lot_size
        self._stop = False
        self._breadth_series: list[tuple[datetime, float]] = []

    def _record_path(self, session_date: date) -> Path:
        d = self.records_root / "breadth" / f"date={session_date.isoformat()}"
        d.mkdir(parents=True, exist_ok=True)
        return d / "record.json"

    def _live_status_path(self, session_date: date) -> Path:
        d = self.records_root / "breadth" / f"date={session_date.isoformat()}"
        d.mkdir(parents=True, exist_ok=True)
        return d / "live_status.json"

    def _load_or_init_today(self, session_date: date) -> BreadthDailyRecord:
        path = self._record_path(session_date)
        if path.exists():
            return BreadthDailyRecord(**json.loads(path.read_text()))
        rec = BreadthDailyRecord(session_date=session_date.isoformat())
        self._save(rec)
        return rec

    def _save(self, rec: BreadthDailyRecord) -> None:
        self._record_path(date.fromisoformat(rec.session_date)).write_text(
            json.dumps(asdict(rec), indent=2, default=str)
        )

    def already_has_call_today(self, session_date: date) -> bool:
        rec = self._load_or_init_today(session_date)
        return rec.skipped or rec.position is not None

    def _current_breadth_reading(self) -> tuple[float, int]:
        merged: dict[str, tuple[float, float]] = {}
        for provider in self._top_of_book_providers:
            merged.update(provider())
        imbalances = {sym: per_stock_imbalance(bid, ask) for sym, (bid, ask) in merged.items()}
        reading = compute_breadth(imbalances, self.cfg.bullish_stock_threshold, self.cfg.bearish_stock_threshold)
        return reading.mean_imbalance, reading.n_stocks

    def _enter_position(self, direction: SignalDirection, now: datetime) -> BreadthPaperPosition:
        spot = self._spot_fn()
        legs = build_breadth_structure(direction, spot, self.cfg)
        leg: BreadthLeg = legs[0]
        symbol = self._resolve_fn(leg.right, leg.strike)
        premium = self._quote_fn(symbol)
        return BreadthPaperPosition(
            entry_ts=now.isoformat(), direction=direction.value,
            leg={"action": leg.action, "right": leg.right, "strike": leg.strike, "lots": leg.lots, "symbol": symbol},
            entry_premium=premium, status="OPEN",
        )

    def _write_live_status(self, session_date: date, now: datetime, breadth: float, n_stocks: int, day_skip_reason: str) -> None:
        payload = {
            "updated_at": now.isoformat(),
            "session_date": session_date.isoformat(),
            "day_skip_reason": day_skip_reason,
            "mean_breadth": breadth,
            "n_stocks_reporting": n_stocks,
            "min_stocks_reporting": self.cfg.min_stocks_reporting,
            "theta_cross": self.cfg.theta_cross,
            "breadth_series_tail": [{"ts": t.isoformat(), "value": v} for t, v in self._breadth_series[-30:]],
        }
        self._live_status_path(session_date).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    async def run_session(self, session_date: date, poll_seconds: float | None = None) -> BreadthDailyRecord:
        poll_seconds = poll_seconds if poll_seconds is not None else self.cfg.bucket_seconds
        rec = self._load_or_init_today(session_date)
        if self.already_has_call_today(session_date):
            log.info("Breadth: %s already has a call recorded -- no-op.", session_date)
            return rec

        position: BreadthPaperPosition | None = None

        while not self._stop:
            now = now_ist()
            if now.time() >= self.cfg.force_exit_time:
                break

            breadth, n_stocks = self._current_breadth_reading()
            in_window = self.cfg.signal_window_start <= now.time() <= self.cfg.signal_window_end
            enough_data = n_stocks >= self.cfg.min_stocks_reporting

            if in_window and enough_data:
                self._breadth_series.append((now, breadth))

            if position is None and in_window and enough_data:
                trigger = detect_breadth_crossing(self._breadth_series, theta_cross=self.cfg.theta_cross)
                if trigger is not None and trigger.direction != SignalDirection.SKIP:
                    try:
                        position = self._enter_position(trigger.direction, now)
                        rec.decision = {"trigger_ts": trigger.ts.isoformat(), "direction": trigger.direction.value, "value": trigger.value}
                        rec.position = asdict(position)
                        self._save(rec)
                        log.info("Breadth: entered %s paper position at %s", trigger.direction.value, now)
                    except Exception as exc:  # noqa: BLE001 -- see paper_trader.py's identical guard: a
                        # real signal firing but the entry leg's quote being unavailable must not crash
                        # the whole recorder process (hit live once already, for Thunderbolt).
                        log.exception("Breadth: could not price entry for %s signal -- stopping for today", trigger.direction.value)
                        rec.skipped = True
                        rec.skip_reason = f"COULD_NOT_PRICE_ENTRY: {exc}"
                        rec.decision = {"trigger_ts": trigger.ts.isoformat(), "direction": trigger.direction.value, "value": trigger.value}
                        self._save(rec)
                        self._write_live_status(session_date, now, breadth, n_stocks, "")
                        return rec

            elif position is not None and position.status == "OPEN":
                current_premium = self._quote_fn(position.leg["symbol"])
                minutes_open = (now - datetime.fromisoformat(position.entry_ts)).total_seconds() / 60.0
                reason = exit_reason(position.entry_premium, current_premium, minutes_open, self.cfg)
                if reason is not None:
                    self._close_position(position, current_premium, now, reason)
                    rec.position = asdict(position)
                    self._save(rec)
                    log.info("Breadth: closed position (%s), net_pnl=%s", reason, position.net_pnl)

            self._write_live_status(session_date, now, breadth, n_stocks, "")
            await asyncio.sleep(poll_seconds)

        if position is not None and position.status == "OPEN":
            current_premium = self._quote_fn(position.leg["symbol"])
            self._close_position(position, current_premium, now_ist(), "FORCE_EXIT")
            rec.position = asdict(position)
            self._save(rec)
        elif position is None and not rec.decision:
            rec.skipped = True
            rec.skip_reason = "NO_QUALIFYING_CROSSING_BY_WINDOW_CLOSE"
            self._save(rec)

        return rec

    def _close_position(self, position: BreadthPaperPosition, exit_premium: float, now: datetime, reason: str) -> None:
        # Always long a single option leg regardless of direction (CE on
        # bullish, PE on bearish) -- P&L is simply the premium change,
        # scaled by lot size and the number of lots taken.
        pnl = (exit_premium - position.entry_premium) * self.lot_size * position.leg["lots"]
        position.exit_ts = now.isoformat()
        position.exit_premium = exit_premium
        position.exit_reason = reason
        position.net_pnl = round(pnl, 2)
        position.status = "CLOSED"

    def stop(self) -> None:
        self._stop = True

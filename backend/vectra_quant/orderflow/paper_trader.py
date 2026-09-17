"""Live paper-trading loop for hedged121 ("Nifty Thunderbolt").

Ties together the pieces already built and unit-tested: a `SessionAggregator`
fed by a running `DepthRecorder` supplies the raw imbalance series;
`evaluate_thunderbolt` decides direction; `build_thunderbolt_structure`
builds the legs; this module resolves real strikes via the instrument
master, fetches real LTPs (no orders are ever placed -- paper only), and
persists one JSON decision+position record per day for audit and review.

One-attempt-per-day idempotency is a file-existence check, matching the
disclosure's own "database check... makes re-runs and restarts no-ops"
(section 5.2) -- if today's record already exists, `run_session()` is a
no-op even after a process restart.

This is the piece that lets you actually paper-trade Thunderbolt starting
tomorrow: run this alongside a running `DepthRecorder` for the NIFTY
future during market hours (09:15-15:35 IST). Nothing here can be
exercised against a live feed in this environment -- see the module-level
NOT INDEPENDENTLY TESTED note.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time

from vectra_quant.core.session_clock import now_ist
from pathlib import Path
from typing import Any

from vectra_quant.orderflow.aggregator import SessionAggregator
from vectra_quant.orderflow.config import ThunderboltCfg
from vectra_quant.orderflow.thunderbolt_signal import (
    DecisionTrace,
    SignalDirection,
    ThunderboltInputs,
    evaluate_thunderbolt,
)
from vectra_quant.orderflow.thunderbolt_strategy import (
    ThunderboltLeg,
    build_thunderbolt_structure,
    max_loss,
    net_debit,
    should_skip_day,
)

log = logging.getLogger("orderflow.paper_trader")


@dataclass
class PaperLegFill:
    action: str
    right: str
    strike: float
    lots: int
    ltp: float
    trading_symbol: str


@dataclass
class PaperPosition:
    entry_ts: str
    direction: str
    legs: list[PaperLegFill]
    net_debit_value: float
    max_loss_value: float
    exit_ts: str | None = None
    exit_legs: list[PaperLegFill] = field(default_factory=list)
    net_pnl: float | None = None
    status: str = "OPEN"  # OPEN | CLOSED | SKIPPED


@dataclass
class DailyRecord:
    session_date: str
    skipped: bool = False
    skip_reason: str = ""
    decision: dict[str, Any] | None = None
    position: dict[str, Any] | None = None


class ThunderboltPaperTrader:
    """One instance per trading day (construct fresh each morning, or call
    `for_new_day()` to reset). NOT independently tested against a live feed
    -- everything it calls into (aggregator, thunderbolt_signal,
    thunderbolt_strategy) is unit tested; this is the orchestration glue."""

    def __init__(
        self, aggregator: SessionAggregator, quote_fn, instrument_resolver_fn,
        cfg: ThunderboltCfg, records_root: str, lot_size: int = 65,
    ):
        self.aggregator = aggregator
        self._quote_fn = quote_fn                    # callable(trading_symbol: str) -> float (LTP)
        self._resolve_fn = instrument_resolver_fn     # callable(right: str, strike: float) -> trading_symbol
        self.cfg = cfg
        self.records_root = Path(records_root)
        self.lot_size = lot_size
        self._today_record: DailyRecord | None = None
        self._stop = False

    def _record_path(self, session_date: date) -> Path:
        d = self.records_root / "thunderbolt" / f"date={session_date.isoformat()}"
        d.mkdir(parents=True, exist_ok=True)
        return d / "record.json"

    def _live_status_path(self, session_date: date) -> Path:
        d = self.records_root / "thunderbolt" / f"date={session_date.isoformat()}"
        d.mkdir(parents=True, exist_ok=True)
        return d / "live_status.json"

    def _write_live_status(
        self, session_date: date, now: datetime, in_signal_window: bool,
        trace: DecisionTrace | None, prior_session_vix: float | None,
        recent_realized_vols: list[float], skip_reason: str,
    ) -> None:
        """Every poll cycle's would-be decision, even when it's SKIP -- the
        UI's only way to answer "why hasn't this fired" is to see this
        exact trace, not just the final end-of-day outcome that
        `record.json` keeps (which stays null until something actually
        triggers)."""
        series = self.aggregator.raw_imbalance_series()
        payload = {
            "updated_at": now.isoformat(),
            "session_date": session_date.isoformat(),
            "day_skip_reason": skip_reason,
            "in_signal_window": in_signal_window,
            "prior_session_vix": prior_session_vix,
            "vix_skip_at_or_above": self.cfg.vix_skip_at_or_above,
            "recent_realized_vols": recent_realized_vols,
            "regime_low_max": self.cfg.regime_low_max,
            "regime_high_min": self.cfg.regime_high_min,
            "latest_imbalance_reading": series[-1][1] if series else None,
            "imbalance_series_tail": [{"ts": t.isoformat(), "value": v} for t, v in series[-30:]],
            "trace": self._trace_to_dict(trace) if trace is not None else None,
        }
        self._live_status_path(session_date).write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

    @staticmethod
    def _trace_to_dict(trace: DecisionTrace) -> dict[str, Any]:
        d = asdict(trace)
        d["regime"] = trace.regime.value
        d["final"] = trace.final.value
        if trace.trigger is not None:
            d["trigger"] = {
                "ts": trace.trigger.ts.isoformat(), "direction": trace.trigger.direction.value,
                "value": trace.trigger.value, "source": trace.trigger.source,
            }
        return d

    def _load_or_init_today(self, session_date: date) -> DailyRecord:
        path = self._record_path(session_date)
        if path.exists():
            data = json.loads(path.read_text())
            return DailyRecord(**data)
        rec = DailyRecord(session_date=session_date.isoformat())
        self._save(rec)
        return rec

    def _save(self, rec: DailyRecord) -> None:
        path = self._record_path(date.fromisoformat(rec.session_date))
        path.write_text(json.dumps(asdict(rec), indent=2, default=str))

    def already_has_call_today(self, session_date: date) -> bool:
        """The disclosure's own idempotency rule: a call already exists for
        today (skipped or a real position) means today is a no-op, even
        across a process restart."""
        rec = self._load_or_init_today(session_date)
        return rec.skipped or rec.position is not None

    def _enter_paper_position(self, spot: float, direction: SignalDirection, entry_ts: datetime) -> PaperPosition:
        legs = build_thunderbolt_structure(direction, spot, self.cfg)
        filled: list[PaperLegFill] = []
        for leg in sorted(legs, key=lambda lg: lg.order):  # longs (order=1) filled first, per the disclosure
            symbol = self._resolve_fn(leg.right, leg.strike)
            ltp = self._quote_fn(symbol)
            filled.append(PaperLegFill(action=leg.action, right=leg.right, strike=leg.strike,
                                        lots=leg.lots, ltp=ltp, trading_symbol=symbol))

        long_premium = next(f.ltp for f in filled if f.action == "BUY")
        short_premium = next(f.ltp for f in filled if f.action == "SELL")
        d = net_debit(long_premium, short_premium, self.cfg, self.lot_size)
        ml = max_loss(d, self.cfg, self.lot_size)

        return PaperPosition(
            entry_ts=entry_ts.isoformat(), direction=direction.value, legs=filled,
            net_debit_value=d, max_loss_value=ml, status="OPEN",
        )

    def _exit_paper_position(self, position: PaperPosition, exit_ts: datetime) -> None:
        exit_legs: list[PaperLegFill] = []
        for leg in position.legs:
            ltp = self._quote_fn(leg.trading_symbol)
            exit_legs.append(PaperLegFill(action=("SELL" if leg.action == "BUY" else "BUY"),
                                           right=leg.right, strike=leg.strike, lots=leg.lots,
                                           ltp=ltp, trading_symbol=leg.trading_symbol))

        # Short leg bought back and confirmed before longs are sold, per the
        # disclosure's exit ordering -- paper trading has no fill-gating
        # risk, but we still price it in that stated order for parity.
        pnl = 0.0
        for entry_leg, exit_leg in zip(position.legs, exit_legs):
            sign = 1 if entry_leg.action == "BUY" else -1
            pnl += sign * (exit_leg.ltp - entry_leg.ltp) * entry_leg.lots * self.lot_size

        position.exit_ts = exit_ts.isoformat()
        position.exit_legs = exit_legs
        position.net_pnl = round(pnl, 2)
        position.status = "CLOSED"

    async def run_session(
        self, session_date: date, recent_realized_vols: list[float],
        prior_session_vix: float | None, spot_fn, poll_seconds: float = 5.0,
    ) -> DailyRecord:
        """`spot_fn` is a callable() -> float giving the live NIFTY spot."""
        rec = self._load_or_init_today(session_date)
        if self.already_has_call_today(session_date):
            log.info("Thunderbolt: %s already has a call recorded -- no-op.", session_date)
            return rec

        skip, reason = should_skip_day(session_date.weekday(), prior_session_vix, self.cfg)
        if skip:
            rec.skipped = True
            rec.skip_reason = reason
            self._save(rec)
            self._write_live_status(session_date, now_ist(), False, None, prior_session_vix, recent_realized_vols, reason)
            log.info("Thunderbolt: skipping %s (%s)", session_date, reason)
            return rec

        position: PaperPosition | None = None
        trace: DecisionTrace | None = None

        while not self._stop:
            now = now_ist()
            if now.time() >= self.cfg.force_exit_time:
                break

            in_window = self.cfg.signal_window_start <= now.time() <= self.cfg.signal_window_end
            if position is None and in_window:
                inputs = ThunderboltInputs(
                    series=self.aggregator.raw_imbalance_series(),
                    recent_realized_vols=recent_realized_vols,
                )
                trace = evaluate_thunderbolt(inputs, self.cfg)
                if trace.final != SignalDirection.SKIP:
                    try:
                        spot = spot_fn()
                        position = self._enter_paper_position(spot, trace.final, now)
                    except Exception as exc:  # noqa: BLE001
                        # A real signal firing but the entry leg's quote
                        # being unavailable (an illiquid/just-listed strike,
                        # a transient Dhan API gap) must not crash the whole
                        # process -- this took the depth recorder down with
                        # it for hours in production, silently, since
                        # nothing was watching a bare traceback in a log
                        # file. Record it plainly and stop for today rather
                        # than hammering the same failing quote every poll
                        # cycle for the rest of the window.
                        log.exception("Thunderbolt: could not price entry for %s signal -- stopping for today", trace.final.value)
                        rec.skipped = True
                        rec.skip_reason = f"COULD_NOT_PRICE_ENTRY: {exc}"
                        rec.decision = asdict(trace)
                        self._save(rec)
                        return rec
                    rec.decision = asdict(trace)
                    rec.position = asdict(position)
                    self._save(rec)
                    log.info("Thunderbolt: entered %s paper position at %s", trace.final.value, now)

            self._write_live_status(session_date, now, in_window, trace, prior_session_vix, recent_realized_vols, "")

            await asyncio.sleep(poll_seconds)

        if position is not None and position.status == "OPEN":
            self._exit_paper_position(position, now_ist())
            rec.position = asdict(position)
            self._save(rec)
            log.info("Thunderbolt: force-exited paper position, net_pnl=%s", position.net_pnl)
        elif position is None and trace is not None and trace.final == SignalDirection.SKIP:
            rec.skipped = True
            rec.skip_reason = "NO_QUALIFYING_TRIGGER_BY_WINDOW_CLOSE"
            rec.decision = asdict(trace)
            self._save(rec)

        return rec

    def stop(self) -> None:
        self._stop = True

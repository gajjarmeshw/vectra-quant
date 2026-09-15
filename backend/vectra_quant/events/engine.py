"""Event engine — code decides WHEN to wake the LLM. Design §5.2.

The LLM is never polled and never watches ticks. Deterministic detectors fire, each
packaging a snapshot. All thresholds and debounce windows come from params.yaml.

Debounce is per (kind, key) so a level breaking twice in 15 minutes fires once, but
two different levels breaking do not suppress each other.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from vectra_quant.core.session_clock import now_ist, session_date, to_ist
from vectra_quant.db import EventRow, session, utcnow
from vectra_quant.logging_setup import get

log = get("events.engine")


class EventKind(str, Enum):
    PRE_MARKET = "PRE_MARKET"
    OPENING_RANGE_SET = "OPENING_RANGE_SET"
    LEVEL_BREAK = "LEVEL_BREAK"
    VIX_SPIKE = "VIX_SPIKE"
    OI_SHIFT = "OI_SHIFT"
    MOMENTUM_BURST = "MOMENTUM_BURST"
    POSITION_EVENT = "POSITION_EVENT"
    MIDDAY_CHECK = "MIDDAY_CHECK"
    DAY_END = "DAY_END"
    OPTION_WALL_TEST = "OPTION_WALL_TEST"
    OPTION_WALL_BREAK = "OPTION_WALL_BREAK"
    REGIME_SHIFT = "REGIME_SHIFT"
    PCR_EXTREME = "PCR_EXTREME"


@dataclass
class Event:
    kind: EventKind
    instrument: str = ""
    key: str = ""                       # debounce discriminator
    detail: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(UTC))
    row_id: int | None = None

    @property
    def wants_suggestion(self) -> bool:
        """Which events should reach the LLM at all."""
        return self.kind not in (EventKind.DAY_END,)


DEFAULT_DEBOUNCE_MIN = {
    EventKind.LEVEL_BREAK: 15,
    EventKind.VIX_SPIKE: 30,
    EventKind.OI_SHIFT: 30,
    EventKind.MOMENTUM_BURST: 20,
    EventKind.POSITION_EVENT: 5,
    EventKind.OPTION_WALL_TEST: 20,
    EventKind.OPTION_WALL_BREAK: 20,
    EventKind.REGIME_SHIFT: 30,
    EventKind.PCR_EXTREME: 45,
}


class EventEngine:
    def __init__(
        self,
        *,
        thresholds: dict[str, float] | None = None,
        debounce_min: dict[str, int] | None = None,
        times: dict[str, Any] | None = None,
    ):
        t = thresholds or {}
        self.level_break_pct = float(t.get("level_break_pct", 0.05))
        self.vix_spike_pct = float(t.get("vix_spike_pct", 5.0))
        self.oi_shift_pct = float(t.get("oi_shift_pct", 20.0))
        self.momentum_atr_mult = float(t.get("momentum_atr_mult", 2.0))

        self.debounce_min: dict[EventKind, int] = dict(DEFAULT_DEBOUNCE_MIN)
        for raw_key, minutes in (debounce_min or {}).items():
            name = str(raw_key).replace("_min", "").upper()
            try:
                self.debounce_min[EventKind(name)] = int(minutes)
            except ValueError:
                log.warning("unknown debounce key", extra={"key": raw_key})

        self.times = times or {}
        self._last_fired: dict[tuple[EventKind, str], datetime] = {}
        self._once_today: set[tuple[str, str]] = set()
        self._vix_open: float | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ debounce
    def _debounced(self, kind: EventKind, key: str) -> bool:
        window = self.debounce_min.get(kind, 0)
        if window <= 0:
            return False
        with self._lock:
            last = self._last_fired.get((kind, key))
        if last is None:
            return False
        return datetime.now(UTC) - last < timedelta(minutes=window)

    def _mark(self, kind: EventKind, key: str) -> None:
        with self._lock:
            self._last_fired[(kind, key)] = datetime.now(UTC)

    def _fire_once_today(self, tag: str) -> bool:
        """True the first time only, per IST day."""
        marker = (session_date(), tag)
        with self._lock:
            if marker in self._once_today:
                return False
            self._once_today.add(marker)
        return True

    def reset_day(self) -> None:
        with self._lock:
            self._last_fired.clear()
            self._once_today = {m for m in self._once_today if m[0] == session_date()}
            self._vix_open = None

    # ------------------------------------------------------------------ emit
    def _emit(self, ev: Event, snapshot: dict | None = None) -> Event:
        with session() as s:
            row = EventRow(
                ts=utcnow(), kind=ev.kind.value, instrument=ev.instrument,
                detail=json.dumps(ev.detail, default=str),
                snapshot=json.dumps(snapshot or {}, default=str),
            )
            s.add(row)
            s.flush()
            ev.row_id = row.id
        self._mark(ev.kind, ev.key or ev.instrument)
        log.info("event fired", extra={
            "kind": ev.kind.value, "instrument": ev.instrument, "detail": ev.detail,
        })
        return ev

    # ------------------------------------------------------------------ time detectors
    def check_time_events(self, when: datetime | None = None) -> list[Event]:
        d = to_ist(when or now_ist())
        out: list[Event] = []
        schedule = [
            ("pre_market", EventKind.PRE_MARKET),
            ("opening_range_set", EventKind.OPENING_RANGE_SET),
            ("midday", EventKind.MIDDAY_CHECK),
            ("day_end", EventKind.DAY_END),
        ]
        for cfg_key, kind in schedule:
            at = self.times.get(cfg_key)
            if at is None:
                continue
            if d.time() >= at and self._fire_once_today(kind.value):
                out.append(self._emit(Event(kind=kind, key=kind.value,
                                           detail={"at": at.strftime("%H:%M")})))
        return out

    # ------------------------------------------------------------------ price detectors
    def check_level_break(
        self, instrument: str, last_close: float, levels: dict[str, float],
        prev_close: float | None = None,
    ) -> list[Event]:
        """A CROSS, confirmed by a 1-min close, past the level by the threshold %.

        Distance alone is proximity, not a break: spot resting 0.06% under PDL all
        morning satisfied it on every tick and re-fired every debounce window,
        burning the daily LLM budget on a level that was never crossed. A cross needs
        the previous close on the other side.
        """
        out: list[Event] = []
        if last_close <= 0:
            return out
        for label, level in levels.items():
            if not level or level <= 0:
                continue
            move_pct = abs(last_close - level) / level * 100.0
            if move_pct < self.level_break_pct:
                continue
            if prev_close is not None and prev_close > 0:
                crossed_up = prev_close <= level < last_close
                crossed_down = prev_close >= level > last_close
                if not (crossed_up or crossed_down):
                    continue
            key = f"{instrument}:{label}"
            if self._debounced(EventKind.LEVEL_BREAK, key):
                continue
            out.append(self._emit(Event(
                kind=EventKind.LEVEL_BREAK, instrument=instrument, key=key,
                detail={"level": label, "level_value": round(level, 2),
                        "close": round(last_close, 2), "move_pct": round(move_pct, 3),
                        "direction": "above" if last_close > level else "below"},
            )))
        return out

    def check_vix_spike(self, vix: float) -> list[Event]:
        if vix <= 0:
            return []
        with self._lock:
            if self._vix_open is None:
                self._vix_open = vix
                return []
            base = self._vix_open
        change = (vix - base) / base * 100.0
        if abs(change) < self.vix_spike_pct:
            return []
        if self._debounced(EventKind.VIX_SPIKE, "vix"):
            return []
        return [self._emit(Event(
            kind=EventKind.VIX_SPIKE, instrument="INDIAVIX", key="vix",
            detail={"vix": round(vix, 2), "open": round(base, 2),
                    "change_pct": round(change, 2)},
        ))]

    def check_oi_shift(self, instrument: str, shifted_rows: list) -> list[Event]:
        out: list[Event] = []
        for r in shifted_rows:
            if abs(getattr(r, "oi_change_pct", 0.0)) < self.oi_shift_pct:
                continue
            key = f"{instrument}:{getattr(r, 'strike', 0)}:{getattr(r, 'side', '')}"
            if self._debounced(EventKind.OI_SHIFT, key):
                continue
            out.append(self._emit(Event(
                kind=EventKind.OI_SHIFT, instrument=instrument, key=key,
                detail={"strike": getattr(r, "strike", 0), "side": getattr(r, "side", ""),
                        "oi": int(getattr(r, "oi", 0)),
                        "oi_change_pct": getattr(r, "oi_change_pct", 0.0)},
            )))
        return out

    def check_momentum_burst(
        self, instrument: str, last_5m_range: float, atr: float | None,
    ) -> list[Event]:
        if not atr or atr <= 0 or last_5m_range <= 0:
            return []
        if last_5m_range < self.momentum_atr_mult * atr:
            return []
        if self._debounced(EventKind.MOMENTUM_BURST, instrument):
            return []
        return [self._emit(Event(
            kind=EventKind.MOMENTUM_BURST, instrument=instrument, key=instrument,
            detail={"range": round(last_5m_range, 2), "atr": round(atr, 2),
                    "mult": round(last_5m_range / atr, 2)},
        ))]

    def check_position_event(
        self, symbol: str, *, pnl_pct_of_target: float, near_sl: bool,
    ) -> list[Event]:
        reason = None
        if near_sl:
            reason = "approaching stop"
        elif pnl_pct_of_target >= 50.0:
            reason = "past 50% of target"
        if reason is None:
            return []
        if self._debounced(EventKind.POSITION_EVENT, symbol):
            return []
        return [self._emit(Event(
            kind=EventKind.POSITION_EVENT, instrument=symbol, key=symbol,
            detail={"reason": reason,
                    "pnl_pct_of_target": round(pnl_pct_of_target, 1),
                    "near_sl": near_sl},
        ))]

    # ------------------------------------------------------------------ institutional detectors
    def check_option_wall_events(
        self, instrument: str, spot: float, walls: Any, prev_spot: float | None = None,
    ) -> list[Event]:
        out: list[Event] = []
        if spot <= 0 or not walls or not getattr(walls, "call_wall", 0):
            return out

        call_w = getattr(walls, "call_wall", 0.0)
        put_w = getattr(walls, "put_wall", 0.0)

        # 1. Option Wall Tests (inside range fade opportunity)
        if hasattr(walls, "is_testing_call_wall") and walls.is_testing_call_wall(0.2):
            key = f"{instrument}:call_wall_test:{call_w}"
            if not self._debounced(EventKind.OPTION_WALL_TEST, key):
                out.append(self._emit(Event(
                    kind=EventKind.OPTION_WALL_TEST, instrument=instrument, key=key,
                    detail={"wall": "CALL", "strike": call_w, "spot": round(spot, 2),
                            "dist_pct": getattr(walls, "dist_call_wall_pct", 0.0),
                            "bias": "resistance_fade"},
                )))

        if hasattr(walls, "is_testing_put_wall") and walls.is_testing_put_wall(0.2):
            key = f"{instrument}:put_wall_test:{put_w}"
            if not self._debounced(EventKind.OPTION_WALL_TEST, key):
                out.append(self._emit(Event(
                    kind=EventKind.OPTION_WALL_TEST, instrument=instrument, key=key,
                    detail={"wall": "PUT", "strike": put_w, "spot": round(spot, 2),
                            "dist_pct": getattr(walls, "dist_put_wall_pct", 0.0),
                            "bias": "support_fade"},
                )))

        # 2. Option Wall Break (trapped sellers hedging in panic = momentum fuel)
        if prev_spot is not None and prev_spot > 0:
            if prev_spot <= call_w < spot:
                key = f"{instrument}:call_wall_break:{call_w}"
                if not self._debounced(EventKind.OPTION_WALL_BREAK, key):
                    out.append(self._emit(Event(
                        kind=EventKind.OPTION_WALL_BREAK, instrument=instrument, key=key,
                        detail={"wall": "CALL", "strike": call_w, "spot": round(spot, 2),
                                "prev_spot": round(prev_spot, 2),
                                "thesis": "trapped call writers forced to cover"},
                    )))
            elif prev_spot >= put_w > spot:
                key = f"{instrument}:put_wall_break:{put_w}"
                if not self._debounced(EventKind.OPTION_WALL_BREAK, key):
                    out.append(self._emit(Event(
                        kind=EventKind.OPTION_WALL_BREAK, instrument=instrument, key=key,
                        detail={"wall": "PUT", "strike": put_w, "spot": round(spot, 2),
                                "prev_spot": round(prev_spot, 2),
                                "thesis": "trapped put writers forced to cover"},
                    )))

        return out

    def check_regime_shift(
        self, instrument: str, current_regime: str, prev_regime: str | None = None,
    ) -> list[Event]:
        if not current_regime or current_regime == prev_regime:
            return []
        if current_regime in ("LONG_BUILDUP", "SHORT_BUILDUP"):
            key = f"{instrument}:regime:{current_regime}"
            if self._debounced(EventKind.REGIME_SHIFT, key):
                return []
            return [self._emit(Event(
                kind=EventKind.REGIME_SHIFT, instrument=instrument, key=key,
                detail={"regime": current_regime, "prev_regime": prev_regime or "UNKNOWN"},
            ))]
        return []

    def check_pcr_extreme(self, instrument: str, pcr: float) -> list[Event]:
        if pcr <= 0:
            return []
        if pcr < 0.7:
            key = f"{instrument}:pcr_oversold"
            if self._debounced(EventKind.PCR_EXTREME, key):
                return []
            return [self._emit(Event(
                kind=EventKind.PCR_EXTREME, instrument=instrument, key=key,
                detail={"pcr": round(pcr, 2), "sentiment": "FEAR_OVERSOLD", "bias": "contrarian_bounce"},
            ))]
        elif pcr > 1.3:
            key = f"{instrument}:pcr_overbought"
            if self._debounced(EventKind.PCR_EXTREME, key):
                return []
            return [self._emit(Event(
                kind=EventKind.PCR_EXTREME, instrument=instrument, key=key,
                detail={"pcr": round(pcr, 2), "sentiment": "COMPLACENT_OVERBOUGHT", "bias": "exhaustion_fade"},
            ))]
        return []

    # ------------------------------------------------------------------ bookkeeping
    def mark_dispatched(self, ev: Event) -> None:
        if ev.row_id is None:
            return
        with session() as s:
            row = s.get(EventRow, ev.row_id)
            if row:
                row.llm_dispatched = True

    def attach_snapshot(self, ev: Event, snapshot: dict) -> None:
        if ev.row_id is None:
            return
        with session() as s:
            row = s.get(EventRow, ev.row_id)
            if row:
                row.snapshot = json.dumps(snapshot, default=str)

"""IST session helpers. One place that knows what "today" and "expiry day" mean.

Everything downstream takes dates from here rather than calling datetime.now()
directly, so tests can pin a moment and the live loop cannot disagree with itself
about which session it is in.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
CONFIG_LOCK_START = time(9, 15)
CONFIG_LOCK_END = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def session_date(dt: datetime | None = None) -> str:
    """YYYY-MM-DD in IST. The journal's day key."""
    return (to_ist(dt) if dt else now_ist()).strftime("%Y-%m-%d")


def is_weekend(dt: datetime | None = None) -> bool:
    return (to_ist(dt) if dt else now_ist()).weekday() >= 5


def is_market_hours(dt: datetime | None = None) -> bool:
    d = to_ist(dt) if dt else now_ist()
    return not is_weekend(d) and MARKET_OPEN <= d.time() <= MARKET_CLOSE


def config_edit_allowed(dt: datetime | None = None) -> bool:
    """Build plan Phase 4: PUT /config rejected 09:15-15:30 IST."""
    d = to_ist(dt) if dt else now_ist()
    if is_weekend(d):
        return True
    return not (CONFIG_LOCK_START <= d.time() <= CONFIG_LOCK_END)


def is_expiry_day(expiries: list[str], dt: datetime | None = None) -> bool:
    """True when today matches one of the instrument master's expiry dates.

    Per D-009 this only moves the entry cutoff earlier; risk is unchanged.
    """
    return session_date(dt) in set(expiries)


def next_session_date(dt: datetime | None = None) -> str:
    d = (to_ist(dt) if dt else now_ist()).date()
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.strftime("%Y-%m-%d")


def week_start(dt: datetime | None = None) -> str:
    """Monday of the current IST week — the weekly governor's bucket key."""
    d = (to_ist(dt) if dt else now_ist()).date()
    return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def parse_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()

"""Price feed service: REST price polling, tick caching, staleness monitoring."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from sentinel.data.candles import CandleBuilder
from sentinel.logging_setup import get

log = get("data.feed")


@dataclass
class FeedHealth:
    worst_age_s: float
    degraded: bool
    critical: bool
    subscribed: int = 0


class FeedService:
    def __init__(
        self,
        broker: Any,
        candles: CandleBuilder,
        degraded_after_s: float = 10.0,
        critical_after_s: float = 60.0,
        on_degraded: Callable[[FeedHealth], None] | None = None,
        on_critical: Callable[[FeedHealth], None] | None = None,
        on_recovered: Callable[[FeedHealth], None] | None = None,
    ):
        self.broker = broker
        self.candles = candles
        self.degraded_after_s = degraded_after_s
        self.critical_after_s = critical_after_s
        self.on_degraded = on_degraded
        self.on_critical = on_critical
        self.on_recovered = on_recovered

        self._prices: dict[str, float] = {}
        self._last_tick_time: dict[str, float] = {}
        self._subscribed: set[str] = set()

        self._is_degraded = False
        self._is_critical = False
        self.reconnects: int = 0

    def subscribe(self, symbols: list[str]) -> None:
        self._subscribed.update(symbols)

    def stop(self) -> None:
        self._subscribed.clear()

    def on_tick(self, symbol: str, price: float) -> None:
        if price <= 0:
            return
        now = time.time()
        self._prices[symbol] = price
        self._last_tick_time[symbol] = now
        self.candles.on_tick(symbol, price)

    def poll_rest(self, symbols: list[str] | None = None, segment: str = "FNO") -> dict[str, float]:
        target_symbols = list(symbols) if symbols else list(self._subscribed)
        if not target_symbols:
            return {}

        fetched: dict[str, float] = {}
        try:
            # Batch fetch prices from broker adapter
            ltp_map = getattr(self.broker, "get_ltp_batch", None)
            if callable(ltp_map):
                fetched = ltp_map(target_symbols)
            else:
                for sym in target_symbols:
                    p = self.broker.get_ltp(sym)
                    if p > 0:
                        fetched[sym] = p

            for sym, price in fetched.items():
                self.on_tick(sym, price)
        except Exception as e:
            log.warning("REST price poll failed for %s: %s", target_symbols, e)

        return fetched

    def refresh_via_rest(self, symbols: list[str]) -> dict[str, float]:
        return self.poll_rest(symbols)

    def prices(self) -> dict[str, float]:
        return dict(self._prices)

    def price(self, symbol: str) -> float | None:
        return self._prices.get(symbol)

    @property
    def suggestions_allowed(self) -> bool:
        return not self._is_degraded

    def health(self, has_open_position: bool = False) -> FeedHealth:
        now = time.time()
        if not self._last_tick_time:
            worst_age = 0.0
        else:
            worst_age = max(now - t for t in self._last_tick_time.values())

        degraded = worst_age > self.degraded_after_s
        critical = has_open_position and (worst_age > self.critical_after_s)

        h = FeedHealth(
            worst_age_s=worst_age,
            degraded=degraded,
            critical=critical,
            subscribed=len(self._subscribed),
        )

        # Trigger callbacks on state transitions
        if degraded and not self._is_degraded:
            self._is_degraded = True
            if self.on_degraded:
                self.on_degraded(h)

        elif not degraded and self._is_degraded:
            self._is_degraded = False
            if self.on_recovered:
                self.on_recovered(h)

        if critical and not self._is_critical:
            self._is_critical = True
            if self.on_critical:
                self.on_critical(h)
        elif not critical:
            self._is_critical = False

        return h

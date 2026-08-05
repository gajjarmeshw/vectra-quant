"""Instrument service: master refresh, strike resolution, expiry management."""
from __future__ import annotations

from typing import Any

from sentinel.brokers.base import Instrument, InstrumentMaster
from sentinel.core.session_clock import session_date
from sentinel.logging_setup import get

log = get("data.instruments")


class InstrumentService:
    def __init__(self, broker: Any, chain_depth: int = 5):
        self.broker = broker
        self.chain_depth = chain_depth
        self._master: InstrumentMaster | None = None

    @property
    def master(self) -> InstrumentMaster:
        if self._master is None:
            self.refresh(force=True)
        assert self._master is not None
        return self._master

    def refresh(self, force: bool = False) -> InstrumentMaster:
        master = self.broker.get_instruments(force=force)
        self._master = master
        log.info("Instrument master refreshed: %d total instruments", len(master))
        return master

    def get(self, symbol: str) -> Instrument | None:
        if self._master is None:
            self.refresh()
        assert self._master is not None
        return self._master.get(symbol)

    def current_expiry(self, name: str) -> str:
        today = session_date()
        exp = self.master.nearest_expiry(name, today)
        return exp or today

    def expiring_today(self, names: list[str] | str) -> bool | list[str]:
        today = session_date()
        if isinstance(names, str):
            exp = self.current_expiry(names)
            return exp == today
        
        # If passed a list of names, check if any of them expire today
        expiring = [n for n in names if self.current_expiry(n) == today]
        return expiring if isinstance(names, list) else len(expiring) > 0

    def resolve_strike(
        self, name: str, expiry: str, strike: float, side: str
    ) -> Instrument | None:
        return self.master.find_option(name, expiry, strike, side)

"""Resolve the current-month NIFTY future contract for depth recording.

VERIFIED against a real live download of the Dhan instrument master
(`DhanAdapter.get_instruments()`, fixed 2026-09-16 -- see that function's
docstring for the confirmed column mapping): `Instrument.instrument_type
== "FUT"` and `Instrument.name == "NIFTY"` correctly isolate rows like
"NIFTY-Sep2026-FUT" (lot_size 65) from lookalikes such as "NIFTYFPI-*-FUT"
or "BANKNIFTY-*-FUT". `Instrument.expiry` is a "YYYY-MM-DD" string.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from vectra_quant.brokers.base import Instrument


class NoFutureContractFound(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedContract:
    instrument: Instrument
    expiry: date
    is_rollover_needed_by: date  # last day it's safe to keep using this contract


def _parse_expiry(expiry_str: str) -> date:
    return datetime.strptime(expiry_str, "%Y-%m-%d").date()


def resolve_current_nifty_future(
    instruments: list[Instrument], today: date, roll_days_before_expiry: int = 1,
) -> ResolvedContract:
    """Nearest-expiry NIFTY future, rolling early by `roll_days_before_expiry`
    days so the recorder never rides a contract into its last, thinnest day.
    """
    futures = [
        i for i in instruments
        if i.name.upper() == "NIFTY" and i.instrument_type.upper() == "FUT" and i.expiry
    ]
    if not futures:
        raise NoFutureContractFound("no NIFTY FUT instruments in the supplied instrument list")

    dated = sorted(((_parse_expiry(i.expiry), i) for i in futures), key=lambda x: x[0])

    for expiry, inst in dated:
        roll_by = expiry - timedelta(days=roll_days_before_expiry)
        if today <= roll_by:
            return ResolvedContract(instrument=inst, expiry=expiry, is_rollover_needed_by=roll_by)

    # every listed contract is already inside its roll window (e.g. today is
    # expiry day itself with no next contract listed yet) -- fall back to the
    # furthest-dated one available rather than raising, and let the caller's
    # rollover check trigger a re-resolve on the next poll.
    expiry, inst = dated[-1]
    return ResolvedContract(instrument=inst, expiry=expiry, is_rollover_needed_by=expiry)


def needs_rollover(current: ResolvedContract, today: date) -> bool:
    return today > current.is_rollover_needed_by

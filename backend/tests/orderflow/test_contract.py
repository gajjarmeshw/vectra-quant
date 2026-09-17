from __future__ import annotations

from datetime import date

import pytest

from vectra_quant.brokers.base import Instrument
from vectra_quant.orderflow.contract import (
    NoFutureContractFound,
    needs_rollover,
    resolve_current_nifty_future,
)


def make_future(expiry: str, symbol: str) -> Instrument:
    return Instrument(
        trading_symbol=symbol, exchange="NSE", segment="NSE_FNO", lot_size=65,
        instrument_type="FUT", name="NIFTY", expiry=expiry,
    )


def test_resolves_nearest_future_when_today_well_before_expiry():
    instruments = [make_future("2026-09-30", "NIFTY26SEPFUT"), make_future("2026-10-28", "NIFTY26OCTFUT")]
    resolved = resolve_current_nifty_future(instruments, today=date(2026, 9, 1))
    assert resolved.instrument.trading_symbol == "NIFTY26SEPFUT"


def test_rolls_to_next_contract_after_roll_by_date():
    instruments = [make_future("2026-09-30", "NIFTY26SEPFUT"), make_future("2026-10-28", "NIFTY26OCTFUT")]
    # roll_by = expiry - 1 day = Sep 29 (last day the Sep contract is used);
    # the day after that, resolution should have already moved to Oct.
    resolved = resolve_current_nifty_future(instruments, today=date(2026, 9, 30), roll_days_before_expiry=1)
    assert resolved.instrument.trading_symbol == "NIFTY26OCTFUT"


def test_falls_back_to_last_contract_when_all_are_in_roll_window():
    instruments = [make_future("2026-09-30", "NIFTY26SEPFUT")]
    resolved = resolve_current_nifty_future(instruments, today=date(2026, 9, 30), roll_days_before_expiry=1)
    assert resolved.instrument.trading_symbol == "NIFTY26SEPFUT"


def test_raises_when_no_futures_present():
    with pytest.raises(NoFutureContractFound):
        resolve_current_nifty_future([], today=date(2026, 9, 1))


def test_ignores_non_nifty_and_non_future_instruments():
    instruments = [
        make_future("2026-09-30", "NIFTY26SEPFUT"),
        Instrument(trading_symbol="BANKNIFTY26SEPFUT", exchange="NSE", segment="NSE_FNO", lot_size=30,
                   instrument_type="FUT", name="BANKNIFTY", expiry="2026-09-30"),
        Instrument(trading_symbol="NIFTY26SEP24500CE", exchange="NSE", segment="NSE_FNO", lot_size=65,
                   instrument_type="CE", name="NIFTY", expiry="2026-09-30", strike=24500),
    ]
    resolved = resolve_current_nifty_future(instruments, today=date(2026, 9, 1))
    assert resolved.instrument.trading_symbol == "NIFTY26SEPFUT"


def test_needs_rollover_true_after_roll_by_date():
    instruments = [make_future("2026-09-30", "NIFTY26SEPFUT")]
    resolved = resolve_current_nifty_future(instruments, today=date(2026, 9, 1), roll_days_before_expiry=1)
    assert needs_rollover(resolved, date(2026, 9, 30)) is True
    assert needs_rollover(resolved, date(2026, 9, 28)) is False

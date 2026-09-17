from __future__ import annotations

from datetime import date, datetime

import pytest

from vectra_quant.brokers.base import Instrument, InstrumentMaster
from vectra_quant.orderflow.contract import (
    NoFutureContractFound,
    needs_rollover,
    resolve_current_nifty_future,
    resolve_nifty_option_window,
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


def make_option(strike: float, side: str, expiry: str = "2026-09-22") -> Instrument:
    return Instrument(
        trading_symbol=f"NIFTY-{expiry}-{int(strike)}-{side}", exchange="NSE", segment="NSE_FNO",
        lot_size=65, instrument_type=side, name="NIFTY", expiry=expiry, strike=strike,
    )


def make_option_master(strikes: list[float], expiry: str = "2026-09-22") -> InstrumentMaster:
    instruments = [make_option(k, side, expiry) for k in strikes for side in ("CE", "PE")]
    return InstrumentMaster(instruments, fetched_at=datetime(2026, 9, 17))


def test_resolve_nifty_option_window_returns_ce_and_pe_around_spot():
    strikes = [22800 + i * 50 for i in range(22)]  # 22800..23850
    master = make_option_master(strikes)
    out = resolve_nifty_option_window(master, spot=23280, on_or_after="2026-09-17", n_strikes_each_side=2, strike_step=50.0)
    symbols = sorted(i.trading_symbol for i in out)
    # ATM rounds 23280 -> 23300; window is 23200,23250,23300,23350,23400 (5 strikes x CE/PE = 10)
    assert len(symbols) == 10
    assert any("23300" in s and s.endswith("CE") for s in symbols)
    assert any("23300" in s and s.endswith("PE") for s in symbols)


def test_resolve_nifty_option_window_skips_unlisted_strikes():
    strikes = [23200, 23300, 23400]  # 23250/23350 not listed
    master = make_option_master(strikes)
    out = resolve_nifty_option_window(master, spot=23280, on_or_after="2026-09-17", n_strikes_each_side=1, strike_step=50.0)
    symbols = sorted(i.trading_symbol for i in out)
    # wanted window (ATM 23300 +/- 1 step) = {23250, 23300, 23350}; only 23300 is listed
    assert len(symbols) == 2
    assert all("23300" in s for s in symbols)


def test_resolve_nifty_option_window_empty_when_no_expiry():
    master = InstrumentMaster([], fetched_at=None)
    out = resolve_nifty_option_window(master, spot=23280, on_or_after="2026-09-17")
    assert out == []

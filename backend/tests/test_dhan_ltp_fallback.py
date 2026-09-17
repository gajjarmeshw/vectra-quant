"""`get_ltp_batch`'s index-LTP path and its fallback chain.

Regression coverage for a real bug hit live: Dhan's `ticker_data` can return
{"status": "failure", "data": ""} for IDX_I tickers -- `data` is a string,
not a dict, in that shape. The old code called `.get()` on it unconditionally
and crashed with "'str' object has no attribute 'get'", silently swallowed by
the outer try/except, then fell through to a 1-minute-candle fallback that
also proved unreliable for index symbols (intermittently 0 bars), leaving
`get_ltp("INDIA VIX")` returning 0.0 -- a real, misleading value, not a
missing one, which broke the new order-flow UI's VIX display.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from vectra_quant.brokers.base import Candle


@pytest.fixture
def adapter():
    with patch("vectra_quant.brokers.dhan.HAS_DHANHQ", True), \
         patch("vectra_quant.brokers.dhan.DhanHQ") as mock_dhanhq, \
         patch("vectra_quant.brokers.dhan.DhanContext", None):
        from vectra_quant.brokers.dhan import DhanAdapter
        a = DhanAdapter("client123", "token123")
        a.dhan = MagicMock()
        yield a


def test_ticker_data_failure_string_does_not_crash_and_falls_back(adapter):
    adapter.dhan.ticker_data.return_value = {"status": "failure", "data": ""}
    adapter.get_candles = MagicMock(return_value=[
        Candle(ts=__import__("datetime").datetime(2026, 9, 17, 9, 20), open=13.0, high=13.5, low=12.8, close=13.14),
    ])

    out = adapter.get_ltp_batch(["INDIA VIX"])

    assert out["INDIA VIX"] == 13.14


def test_ticker_data_success_path_used_directly(adapter):
    # Real observed shape: the SDK wraps the raw HTTP body under "data" again,
    # with per-security entries two levels deeper under the segment key.
    adapter.dhan.ticker_data.return_value = {
        "status": "success", "remarks": "",
        "data": {"data": {"IDX_I": {"21": {"last_price": 13.14}}}, "status": "success"},
    }
    adapter.get_candles = MagicMock(side_effect=AssertionError("should not fall back when ticker_data succeeds"))

    out = adapter.get_ltp_batch(["INDIA VIX"])

    assert out["INDIA VIX"] == 13.14


def test_falls_back_to_daily_candles_when_1m_candles_are_empty(adapter):
    adapter.dhan.ticker_data.return_value = {"status": "failure", "data": ""}

    def fake_get_candles(sym, tf, span):
        if tf == "1m":
            return []  # the intermittent 0-bars case observed live
        return [Candle(ts=__import__("datetime").datetime(2026, 9, 17), open=13.0, high=13.5, low=12.8, close=13.14)]

    adapter.get_candles = MagicMock(side_effect=fake_get_candles)

    out = adapter.get_ltp_batch(["INDIA VIX"])

    assert out["INDIA VIX"] == 13.14


def test_returns_empty_when_every_source_fails(adapter):
    adapter.dhan.ticker_data.return_value = {"status": "failure", "data": ""}
    adapter.get_candles = MagicMock(return_value=[])

    out = adapter.get_ltp_batch(["INDIA VIX"])

    assert "INDIA VIX" not in out


# --------------------------------------------------------------- get_quote (same bug, different method)
# Hit live: a real Thunderbolt signal fired, then crashed the whole recorder
# process trying to price the entry leg because get_quote's index AND
# option paths had the identical double-nesting bug as ticker_data above.
# The process had no top-level recovery, so it stayed dead for ~8.5 hours.

def test_get_quote_index_success_path(adapter):
    adapter.dhan.ohlc_data.return_value = {
        "status": "success", "remarks": "",
        "data": {"data": {"IDX_I": {"13": {"last_price": 23270.6, "ohlc": {}}}}, "status": "success"},
    }
    adapter.get_candles = MagicMock(side_effect=AssertionError("should not fall back when ohlc_data succeeds"))

    out = adapter.get_quote(["NIFTY"])

    assert out["NIFTY"].last_price == 23270.6


def test_get_quote_option_success_path(adapter):
    fake_inst = MagicMock(exchange_token="55555")
    adapter._instruments_master = MagicMock()
    adapter._instruments_master.get.return_value = fake_inst
    adapter.dhan.ohlc_data.return_value = {
        "status": "success", "remarks": "",
        "data": {"data": {"NSE_FNO": {"55555": {"last_price": 120.5, "oi": 1000, "volume": 500}}}, "status": "success"},
    }
    adapter.get_candles = MagicMock(side_effect=AssertionError("should not fall back when ohlc_data succeeds"))

    out = adapter.get_quote(["NIFTY-Sep2026-23400-CE"])

    assert out["NIFTY-Sep2026-23400-CE"].last_price == 120.5


def test_get_quote_falls_back_to_candles_when_option_quote_missing(adapter):
    """Before this fix, a missing option quote propagated all the way up to
    an unhandled RuntimeError in the live paper trader and killed the whole
    process. This confirms the candle fallback actually kicks in now."""
    fake_inst = MagicMock(exchange_token="55555")
    adapter._instruments_master = MagicMock()
    adapter._instruments_master.get.return_value = fake_inst
    adapter.dhan.ohlc_data.return_value = {"status": "failure", "data": ""}
    adapter.get_candles = MagicMock(return_value=[
        Candle(ts=__import__("datetime").datetime(2026, 9, 17), open=118.0, high=122.0, low=117.0, close=120.5),
    ])

    out = adapter.get_quote(["NIFTY-Sep2026-23400-CE"])

    assert out["NIFTY-Sep2026-23400-CE"].last_price == 120.5

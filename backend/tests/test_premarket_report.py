"""The pre-market report must never present placeholder levels as a market read.

Every number in the report -- the option walls, the summary text, the whole
prompt handed to the LLM -- is derived from the NIFTY/BANKNIFTY/VIX spot. When
the feed is down those spots fall back to hardcoded constants, which is fine
for keeping the screen renderable and not fine if the UI shows the result as
though someone quoted it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from vectra_quant.reports import premarket


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Keep the report builder off the network in tests."""
    monkeypatch.setattr(premarket, "fetch_financial_news", lambda: ["headline"])
    monkeypatch.setattr(
        premarket,
        "fetch_global_cues",
        lambda nifty_spot=0.0: {
            "dow_change_pct": 0.5,
            "nasdaq_change_pct": 0.4,
            "brent_crude": 80.0,
            "usdinr": 83.0,
            "dxy": 104.0,
            "gift_nifty_px": nifty_spot,
            "fii_dii_net_cr": (0.0, 0.0),
        },
    )


def _state(prices: dict[str, float | None]):
    return SimpleNamespace(
        feed=SimpleNamespace(price=lambda sym: prices.get(sym)),
        settings=SimpleNamespace(secrets=SimpleNamespace(groq_api_key="")),
    )


def test_live_feed_produces_no_placeholder_flag():
    report = premarket.build_premarket_report(
        _state({"NIFTY": 25123.0, "BANKNIFTY": 55000.0, "INDIAVIX": 12.4})
    )
    assert report["nifty_ltp"] == 25123.0
    assert report["vix"] == 12.4
    assert not any("placeholder" in f for f in report["data_flags"])


def test_dead_feed_is_flagged_and_names_every_missing_symbol():
    report = premarket.build_premarket_report(
        _state({"NIFTY": None, "BANKNIFTY": None, "INDIAVIX": None})
    )
    flag = next(f for f in report["data_flags"] if "placeholder" in f)
    for symbol in ("NIFTY", "BANKNIFTY", "INDIAVIX"):
        assert symbol in flag
    # The report still renders -- it just says the levels are invented.
    assert report["nifty_ltp"] == 24750.0


def test_partially_dead_feed_flags_only_the_missing_symbol():
    report = premarket.build_premarket_report(
        _state({"NIFTY": 25123.0, "BANKNIFTY": None, "INDIAVIX": 12.4})
    )
    flag = next(f for f in report["data_flags"] if "placeholder" in f)
    assert "BANKNIFTY" in flag
    assert "NIFTY," not in flag  # the live one is not named
    assert "INDIAVIX" not in flag


def test_placeholder_flag_is_listed_first():
    """It is the caveat that invalidates every other line; order matters."""
    report = premarket.build_premarket_report(
        _state({"NIFTY": None, "BANKNIFTY": None, "INDIAVIX": None})
    )
    assert "placeholder" in report["data_flags"][0]


def test_report_says_when_it_is_the_rule_based_fallback():
    """A templated report and a model-written one used to look identical."""
    report = premarket.build_premarket_report(
        _state({"NIFTY": 25123.0, "BANKNIFTY": 55000.0, "INDIAVIX": 12.4})
    )
    assert report["engine"] == "template"
    assert any("fallback" in f for f in report["data_flags"])
    assert report["generated_at"]


def test_second_call_is_served_from_cache():
    """One Home render must not mean a fresh paid LLM call."""
    calls = {"n": 0}
    real = premarket.build_premarket_report

    def counting(st):
        calls["n"] += 1
        return real(st)

    st = _state({"NIFTY": 25123.0, "BANKNIFTY": 55000.0, "INDIAVIX": 12.4})
    import unittest.mock as mock

    with mock.patch.object(premarket, "build_premarket_report", counting):
        first = premarket.get_premarket_report(st)
        second = premarket.get_premarket_report(st)
        assert calls["n"] == 1
        assert first["cached"] is False
        assert second["cached"] is True

        # The refresh button must still be able to force a rebuild.
        third = premarket.get_premarket_report(st, force=True)
        assert calls["n"] == 2
        assert third["cached"] is False

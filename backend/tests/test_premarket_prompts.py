"""The pre-market prompt must not give the model room to invent numbers.

`reports.premarket` imported `vectra_quant.llm.prompts` from the day it was
written, but the module did not exist, so every call raised ImportError into a
broad `except Exception` and silently produced the rule-based template. These
tests exist so that cannot recur unnoticed.
"""
from __future__ import annotations

import pytest

from vectra_quant.llm.prompts import PREMARKET_SYSTEM_PROMPT, build_premarket_prompt


def test_the_module_premarket_imports_is_actually_importable():
    """The regression that started all of this."""
    from vectra_quant.reports import premarket  # noqa: F401
    from vectra_quant.llm.prompts import build_premarket_prompt as fn

    assert callable(fn)


def test_system_prompt_forbids_invented_numbers_and_personal_advice():
    p = PREMARKET_SYSTEM_PROMPT
    assert "ONLY the figures supplied" in p
    assert "personalised financial advice" in p
    assert "Do not recommend specific trades" in p


def _full(**over):
    kwargs = dict(
        gift_nifty_px=25200.0,
        dow_change_pct=0.42,
        nasdaq_change_pct=-0.18,
        dxy=104.2,
        brent_crude=79.5,
        usdinr=83.1,
        fii_dii_net_cr=(1250.0, 840.0),
        news_headlines=["RBI holds rates", "IT majors guide lower"],
    )
    kwargs.update(over)
    return build_premarket_prompt(
        "2026-09-18", 25123.0, 55010.0, 12.4, 25050.0, 25200.0, 54700.0, 55300.0, **kwargs
    )


def test_every_supplied_input_appears():
    p = _full()
    for fragment in ("25,123", "55,010", "12.40", "25,050", "+0.42%", "-0.18%", "83.10"):
        assert fragment in p, fragment
    assert "RBI holds rates" in p
    assert "not available" not in p


def test_missing_inputs_are_named_not_omitted():
    """An absent line invites the model to fill the gap; an explicit
    'not available' does not."""
    p = _full(dow_change_pct=None, brent_crude=None, fii_dii_net_cr=None, news_headlines=[])
    assert p.count("not available") >= 4
    assert "Dow" in p and "Brent crude" in p and "FII / DII net flow" in p


def test_placeholder_spots_are_marked_and_escalated():
    p = _full()
    assert "PLACEHOLDER" not in p

    p = build_premarket_prompt(
        "2026-09-18", 24750.0, 52100.0, 14.5, 24650.0, 24850.0, 51800.0, 52400.0,
        stale_inputs=["NIFTY", "INDIAVIX"],
    )
    assert "PLACEHOLDER, no live quote for NIFTY" in p
    assert "PLACEHOLDER, no live quote for INDIAVIX" in p
    # BANKNIFTY was live, so it must not be marked.
    assert "no live quote for BANKNIFTY" not in p
    # And the model is told what to do about it.
    assert "hardcoded" in p and "meaningless" in p


def test_headlines_are_capped():
    p = _full(news_headlines=[f"headline {i}" for i in range(20)])
    assert "headline 7" in p
    assert "headline 8" not in p


@pytest.mark.parametrize("bad", ["", None, "n/a"])
def test_unparseable_numbers_degrade_to_not_available(bad):
    p = _full(dxy=bad)
    assert "Dollar index (DXY)  not available" in p

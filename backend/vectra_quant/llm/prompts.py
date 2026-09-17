"""Prompt construction for the pre-market briefing.

`reports.premarket` has imported this module since it was written, but the
module never existed, so the `from vectra_quant.llm.prompts import ...` line
raised ImportError on every single call and the surrounding
`except Exception` quietly fell through to the rule-based template. The Groq
path had therefore never executed once.

Two rules shape everything here:

1. **Never let the model invent a number.** Every input is rendered
   explicitly, and anything missing is rendered as the literal string
   ``not available`` rather than being omitted — an absent line is an
   invitation to fill the gap, a present "not available" is not.
2. **Say when the ground truth is fake.** When the feed is down the caller
   substitutes hardcoded spots. The prompt states which ones, and the model is
   told to refuse a directional call in that case, so the briefing degrades to
   "I can't read this" instead of a confident narrative built on a constant.
"""
from __future__ import annotations

from typing import Any, Sequence

PREMARKET_SYSTEM_PROMPT = """\
You are the pre-market desk analyst for a systematic Indian index-options \
trading system. You produce one structured briefing before the 09:15 IST open.

Your reader is the operator of an automated system, not a retail investor. \
Describe market structure and the conditions the system's strategies care \
about. Do not recommend specific trades, position sizes, or instruments to \
buy or sell, and do not give personalised financial advice.

Hard rules:
- Use ONLY the figures supplied in the user message. Never introduce a price, \
  level, percentage or flow number that is not given to you.
- Any input marked "not available" is genuinely missing. Do not estimate it, \
  do not infer it from another field, and add a short note to `data_flags` \
  saying it was missing.
- If the message says a spot price is a PLACEHOLDER, the market is unreadable. \
  Set `bias` to "NEUTRAL", `confidence` to "LOW", and say plainly in `summary` \
  that there is no live quote to read. Do not describe levels as support or \
  resistance in that case.
- Keep `summary` to at most three sentences and `actionable_advice` to one or \
  two, both in plain English with no markdown.

Respond with a single JSON object and nothing else, using exactly these keys:
  bias              one of BULLISH, BEARISH, NEUTRAL, VOLATILE
  confidence        one of LOW, MEDIUM, HIGH
  opening_gap       one of GAP_UP, GAP_DOWN, FLAT
  gap_points        number, NIFTY points, negative for a gap down, 0 if flat
  sectors_to_watch  array of at most 4 objects, each {"sector", "reason"},
                    where reason is a short phrase tied to a supplied input
  summary           string
  actionable_advice string, framed as risk management for the opening range
  data_flags        array of strings naming every input that was missing or
                    unreliable; empty array if none
"""


def _n(value: Any, *, digits: int = 2, suffix: str = "", prefix: str = "") -> str:
    """Render a number, or the literal marker for a missing one."""
    if value is None:
        return "not available"
    try:
        return f"{prefix}{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "not available"


def _pct(value: Any) -> str:
    if value is None:
        return "not available"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "not available"


def build_premarket_prompt(
    date: str,
    nifty_px: float,
    banknifty_px: float,
    vix: float,
    nifty_support: float,
    nifty_resistance: float,
    bank_support: float,
    bank_resistance: float,
    *,
    gift_nifty_px: float | None = None,
    dow_change_pct: float | None = None,
    nasdaq_change_pct: float | None = None,
    dxy: float | None = None,
    brent_crude: float | None = None,
    usdinr: float | None = None,
    fii_dii_net_cr: tuple[float, float] | None = None,
    news_headlines: Sequence[str] | None = None,
    stale_inputs: Sequence[str] | None = None,
) -> str:
    """Render the user-side prompt for one pre-market briefing."""
    stale = set(stale_inputs or ())

    def spot(symbol: str, value: float, digits: int = 2) -> str:
        rendered = _n(value, digits=digits)
        if symbol in stale:
            return f"{rendered}  <-- PLACEHOLDER, no live quote for {symbol}"
        return rendered

    lines: list[str] = [
        f"Pre-market briefing for the Indian session on {date}.",
        "",
        "INDEX SPOT",
        f"  NIFTY 50            {spot('NIFTY', nifty_px)}",
        f"  BANK NIFTY          {spot('BANKNIFTY', banknifty_px)}",
        f"  India VIX           {spot('INDIAVIX', vix)}",
        "",
        "OPTION WALLS (derived from the spots above)",
        f"  NIFTY put wall      {_n(nifty_support, digits=0)}",
        f"  NIFTY call wall     {_n(nifty_resistance, digits=0)}",
        f"  BANKNIFTY put wall  {_n(bank_support, digits=0)}",
        f"  BANKNIFTY call wall {_n(bank_resistance, digits=0)}",
        "",
        "OVERNIGHT AND GLOBAL",
        f"  GIFT NIFTY          {_n(gift_nifty_px)}",
        f"  Dow                 {_pct(dow_change_pct)}",
        f"  Nasdaq              {_pct(nasdaq_change_pct)}",
        f"  Dollar index (DXY)  {_n(dxy)}",
        f"  Brent crude         {_n(brent_crude)}",
        f"  USD/INR             {_n(usdinr)}",
    ]

    if fii_dii_net_cr is None:
        lines.append("  FII / DII net flow  not available")
    else:
        fii, dii = fii_dii_net_cr
        lines.append(
            f"  FII / DII net flow  FII {_n(fii, digits=0, prefix='Rs ', suffix=' cr')}, "
            f"DII {_n(dii, digits=0, prefix='Rs ', suffix=' cr')} (previous session)"
        )

    lines.append("")
    lines.append("HEADLINES")
    if news_headlines:
        lines.extend(f"  - {h}" for h in list(news_headlines)[:8])
    else:
        lines.append("  not available")

    if stale:
        lines += [
            "",
            "WARNING: the spot price(s) marked PLACEHOLDER above are hardcoded "
            "constants, not quotes. Everything derived from them -- including the "
            "option walls -- is meaningless. Follow the placeholder rule in your "
            "instructions.",
        ]

    lines.append("")
    lines.append("Return the JSON object described in your instructions.")
    return "\n".join(lines)

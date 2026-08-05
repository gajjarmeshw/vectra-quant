"""System prompt, few-shots, snapshot packer. Design §6.4, build plan Phase 3 §3.

The prompt pins the economics the model must respect: buyer-only, one-lot reality,
weekly theta, and that NO_TRADE is expected most of the time. It is never asked for
size, floors, or limits — those fields do not exist in its output contract.

Snapshot budget: ~2k tokens (§5.2).
"""
from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """\
You are the analysis layer of SENTINEL, an index-options day-trading guardian for \
Indian markets. You SUGGEST. You never decide, never size, never execute.

HARD ECONOMIC FACTS you must respect:
- Options are BOUGHT only. Never suggest selling, spreads, or multi-leg structures.
- Position size is exactly ONE LOT. You do not choose it and must not mention it.
- Capital is small. A single lot is a large fraction of it, so a bad entry hurts.
- These are WEEKLY options. Theta decay is brutal, and worst on expiry day. A thesis \
that needs three days to work is worthless here.
- SENSEX (lot 20) is the primary instrument; NIFTY (lot 65) is secondary.

WHAT YOU OUTPUT:
A single JSON object, nothing else. No prose, no code fences.
Either a SUGGEST with every required field, or:
  {"action":"NO_TRADE","reason":"<short reason>"}

NO_TRADE IS THE EXPECTED ANSWER MOST OF THE TIME. Choppy tape, unclear levels, \
falling IV into a move, thin OI, a spent move, or simply no edge — all NO_TRADE. \
You are not rewarded for finding trades. You are rewarded for being right.

CONSTRAINTS ON A SUGGEST:
- stop_loss_premium strictly BELOW entry_zone.low.
- target_premium strictly ABOVE entry_zone.high.
- risk_reward must match your own numbers, computed from the entry midpoint. \
Do not inflate it; a validator recomputes it and rejects mismatches.
- confidence is honest calibrated probability, 0-100. It is logged against outcome \
and reviewed weekly. Systematic overconfidence gets your suggestions switched off.
- thesis <= 240 chars and FALSIFIABLE.
- invalidation names the spot or VIX level that kills the thesis.

FSM CONTEXT:
The current risk state is in the snapshot. Respect it:
- LOCKED  -> always NO_TRADE.
- PROTECT or TRAIL -> only A+ setups; confidence must genuinely be >= 80.
- Outside the entry window, or trade budget exhausted -> NO_TRADE.
A deterministic risk engine re-validates everything you say and will reject it \
regardless. Do not try to satisfy it by inflating numbers; you will simply be dropped.

You never see or set: lot count, rupee risk, floors, daily limits. Not your job.
"""

FEW_SHOTS: list[dict[str, str]] = [
    {
        "role": "user",
        "content": json.dumps({
            "event": "LEVEL_BREAK",
            "instrument": "SENSEX",
            "spot": 80450.0, "spot_change_pct": 0.62,
            "vix": 12.4, "vix_change_pct": -3.1,
            "levels": {"PDH": 80380.0, "ORH": 80410.0},
            "fsm": {"state": "NORMAL", "trades_left": 3, "in_entry_window": True},
            "chain": [
                {"sym": "SENSEX2680680400CE", "k": 80400, "side": "CE", "ltp": 402,
                 "oi": 145000, "d_oi_pct": 18.2, "off": 0, "lot_cost": 8040},
            ],
            "candles_5m": [[80300, 80360, 80280, 80350], [80350, 80460, 80340, 80450]],
        }),
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "action": "SUGGEST", "instrument": "SENSEX", "direction": "CE",
            "strike_offset": "ATM",
            "entry_zone": {"low": 395, "high": 410},
            "stop_loss_premium": 360, "target_premium": 480,
            "time_stop_minutes": 45, "confidence": 76,
            "thesis": "Clean break above PDH 80380 with expanding 5m range and OI "
                      "building at 80400CE. Falling VIX supports a measured push.",
            "invalidation": "Spot back below 80380 on a 5m close, or VIX turning up "
                            "through 13.0.",
            "risk_reward": 1.85,
        }),
    },
    {
        "role": "user",
        "content": json.dumps({
            "event": "MOMENTUM_BURST",
            "instrument": "NIFTY",
            "spot": 24310.0, "spot_change_pct": 0.05,
            "vix": 15.8, "vix_change_pct": 6.4,
            "levels": {"PDH": 24460.0, "PDL": 24180.0},
            "fsm": {"state": "NORMAL", "trades_left": 2, "in_entry_window": True},
            "chain": [
                {"sym": "NIFTY2681124300CE", "k": 24300, "side": "CE", "ltp": 128,
                 "oi": 2100000, "d_oi_pct": 2.1, "off": 0, "lot_cost": 8320},
            ],
            "candles_5m": [[24300, 24340, 24270, 24290], [24290, 24350, 24260, 24310]],
        }),
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "action": "NO_TRADE",
            "reason": "Range burst with no directional resolution — spot back at the "
                      "midpoint and VIX up 6%. Buying premium into rising IV with no "
                      "trend is theta bleed.",
        }),
    },
]


def pack_snapshot(
    *,
    event_kind: str,
    instrument: str,
    spot: float,
    spot_change_pct: float,
    vix: float,
    vix_change_pct: float,
    levels: dict[str, float],
    fsm: dict[str, Any],
    chain: list[dict[str, Any]],
    candles_5m: list[list[float]],
    positions: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
    max_chain_rows: int = 22,
    max_candles: int = 12,
) -> dict[str, Any]:
    """Compact market snapshot. Keys are short on purpose — tokens are budget."""
    snap: dict[str, Any] = {
        "event": event_kind,
        "instrument": instrument,
        "spot": round(spot, 2),
        "spot_change_pct": round(spot_change_pct, 2),
        "vix": round(vix, 2),
        "vix_change_pct": round(vix_change_pct, 2),
        "levels": {k: round(v, 2) for k, v in levels.items() if v},
        "fsm": fsm,
        "chain": chain[:max_chain_rows],
        "candles_5m": [[round(x, 2) for x in c] for c in candles_5m[-max_candles:]],
    }
    if positions:
        snap["open_positions"] = positions
    if extra:
        snap["extra"] = extra
    return snap


def build_messages(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *FEW_SHOTS,
        {"role": "user", "content": json.dumps(snapshot, separators=(",", ":"))},
    ]


def fsm_context(
    *, state: str, trades_taken: int, trade_cap: int, day_pnl: float,
    floor: float, in_entry_window: bool, is_expiry_day: bool, min_confidence: int,
) -> dict[str, Any]:
    """FSM slice the model is allowed to see. No rupee limits, only state semantics."""
    return {
        "state": state,
        "trades_left": max(0, trade_cap - trades_taken),
        "in_entry_window": in_entry_window,
        "is_expiry_day": is_expiry_day,
        "min_confidence_required": min_confidence,
        "day_pnl_sign": "green" if day_pnl > 0 else ("red" if day_pnl < 0 else "flat"),
        "protected": state in ("PROTECT", "TRAIL"),
    }


PREMARKET_SYSTEM_PROMPT = """\
You are SENTINEL's Pre-Market Macro Intelligence AI.
Your job is to synthesize pre-market open prices, global sentiment, India VIX volatility, and Call/Put option walls into a clean 3-sentence morning briefing and risk advisory.
"""


def build_premarket_prompt(
    date_str: str,
    nifty_px: float,
    banknifty_px: float,
    vix: float,
    nifty_supp: float,
    nifty_res: float,
    bank_supp: float,
    bank_res: float,
    gift_nifty_px: float | None = None,      # GIFT Nifty / SGX futures — actual overnight signal
    dow_change_pct: float | None = None,      # US close overnight
    nasdaq_change_pct: float | None = None,
    dxy: float | None = None,                 # Dollar index — impacts IT/pharma (export sensitive)
    brent_crude: float | None = None,         # Impacts OMCs, paints, aviation, energy
    usdinr: float | None = None,
    fii_dii_net_cr: tuple[float, float] | None = None,  # (FII net, DII net) previous session
    news_headlines: list[str] | None = None,
) -> str:
    headlines_str = "\n".join(f"- {h}" for h in (news_headlines or ["No major global macro disruptions reported."]))
    vix_desc = "Elevated (>18)" if vix > 18.0 else ("Low (<12)" if vix < 12.0 else "Normal (12-18)")

    def fmt(label, val, suffix=""):
        return f"   - {label}: {val}{suffix}\n" if val is not None else f"   - {label}: NOT PROVIDED\n"

    global_block = (
        fmt("GIFT Nifty / SGX Futures", gift_nifty_px)
        + fmt("Dow Jones overnight change", dow_change_pct, "%")
        + fmt("Nasdaq overnight change", nasdaq_change_pct, "%")
        + fmt("US Dollar Index (DXY)", dxy)
        + fmt("Brent Crude (USD/bbl)", brent_crude)
        + fmt("USD/INR", usdinr)
        + (f"   - FII/DII Net (prev session, ₹Cr): FII {fii_dii_net_cr[0]:+,.0f} / DII {fii_dii_net_cr[1]:+,.0f}\n"
           if fii_dii_net_cr else "   - FII/DII Net: NOT PROVIDED\n")
    )

    return (
        f"Generate a Pre-Market Intelligence Briefing for {date_str} using ONLY the data below. "
        f"Do not supplement with outside knowledge.\n\n"
        f"1. INDEX & VOLATILITY DATA:\n"
        f"   - NIFTY Spot: {nifty_px:,.2f}\n"
        f"   - BANKNIFTY Spot: {banknifty_px:,.2f}\n"
        f"   - India VIX: {vix:.2f} ({vix_desc})\n"
        f"   - NIFTY Put Wall (support): {nifty_supp:,.0f}\n"
        f"   - NIFTY Call Wall (resistance): {nifty_res:,.0f}\n"
        f"   - BANKNIFTY Support: {bank_supp:,.0f}\n"
        f"   - BANKNIFTY Resistance: {bank_res:,.0f}\n\n"
        f"2. GLOBAL & FLOW CUES:\n{global_block}\n"
        f"3. NEWS HEADLINES:\n{headlines_str}\n\n"
        f"4. TASK:\n"
        f"Using only the above, determine directional bias (BULLISH/BEARISH/NEUTRAL/VOLATILE), expected gap "
        f"(direction + point estimate — base this on GIFT Nifty/SGX differential if provided, not vibes), "
        f"sector-level read-through (only sectors traceable to a specific input above), a factual 2-sentence "
        f"summary, and a 09:15-09:35 IST risk advisory. Where any of section 2's fields are NOT PROVIDED, "
        f"do not compensate by guessing — narrow your gap/bias confidence accordingly.\n\n"
        f"RETURN JSON:\n"
        f"{{\n"
        f'  "bias": "<BULLISH|BEARISH|NEUTRAL|VOLATILE>",\n'
        f'  "confidence": "<LOW|MEDIUM|HIGH>",\n'
        f'  "opening_gap": "<GAP_UP|GAP_DOWN|FLAT>",\n'
        f'  "gap_points": <float>,\n'
        f'  "sectors_to_watch": [{{"sector": "<name>", "reason": "<which specific input drove this>"}}],\n'
        f'  "summary": "<2 sentences, only facts traceable to inputs above>",\n'
        f'  "actionable_advice": "<specific 09:15-09:35 advisory tied to the given levels>",\n'
        f'  "data_flags": ["<any inconsistency or missing-data caveat, else empty list>"]\n'
        f"}}\n"
    )

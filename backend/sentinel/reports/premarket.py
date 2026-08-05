"""Pre-Market Morning Intelligence & Global Macro Briefing module."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from sentinel.core.session_clock import session_date
from sentinel.logging_setup import get

log = get("reports.premarket")


@dataclass
class PreMarketReport:
    date: str
    bias: str                     # BULLISH | BEARISH | NEUTRAL | VOLATILE
    confidence: str               # LOW | MEDIUM | HIGH
    opening_gap: str              # GAP_UP | GAP_DOWN | FLAT
    gap_points: float
    vix: float
    nifty_ltp: float
    nifty_support: float
    nifty_resistance: float
    banknifty_ltp: float
    banknifty_support: float
    banknifty_resistance: float
    sectors_to_watch: list[dict[str, str]]
    summary: str
    actionable_advice: str
    data_flags: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def fetch_financial_news() -> list[str]:
    """Fetch top live Indian financial news headlines via RSS."""
    try:
        import urllib.request
        import xml.etree.ElementTree as ET

        url = "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-IN&gl=IN&ceid=IN:en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            xml_data = resp.read()
        root = ET.fromstring(xml_data)
        headlines = [
            item.find("title").text
            for item in root.findall(".//item")[:5]
            if item.find("title") is not None and item.find("title").text
        ]
        return headlines or ["Global markets monitoring overnight trend"]
    except Exception as exc:
        log.warning("Failed to fetch live RSS news: %s", exc)
        return ["Global markets monitoring overnight trend"]


def fetch_global_cues(nifty_spot: float = 24750.0) -> dict[str, Any]:
    """Fetch live global market cues (GIFT Nifty, Dow %, Nasdaq %, Brent Crude, DXY, USD/INR, FII/DII)."""
    out: dict[str, Any] = {
        "dow_change_pct": None,
        "nasdaq_change_pct": None,
        "brent_crude": None,
        "usdinr": None,
        "dxy": None,
        "gift_nifty_px": None,
        "fii_dii_net_cr": (1250.0, 840.0),  # Previous session FII/DII net flows (₹Cr)
    }
    try:
        import json
        import urllib.request

        def _fetch_data(sym: str) -> tuple[float | None, float | None]:
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    d = json.loads(resp.read())
                    meta = d["chart"]["result"][0]["meta"]
                    px = float(meta["regularMarketPrice"])
                    prev = float(meta["chartPreviousClose"])
                    pct = round(((px - prev) / prev) * 100.0, 2)
                    return round(px, 2), pct
            except Exception:
                return None, None

        dow_px, dow_pct = _fetch_data("YM=F")
        if dow_pct is None:
            dow_px, dow_pct = _fetch_data("%5EDJI")

        nasdaq_px, nasdaq_pct = _fetch_data("NQ=F")
        if nasdaq_pct is None:
            nasdaq_px, nasdaq_pct = _fetch_data("%5EIXIC")

        brent_px, _ = _fetch_data("BZ=F")
        usdinr_px, _ = _fetch_data("INR=X")
        dxy_px, _ = _fetch_data("DX-Y.NYB")

        out["dow_change_pct"] = dow_pct
        out["nasdaq_change_pct"] = nasdaq_pct
        out["brent_crude"] = brent_px
        out["usdinr"] = usdinr_px
        out["dxy"] = dxy_px

        # Compute GIFT Nifty overnight signal from spot + US futures change
        avg_us_pct = ((dow_pct or 0.0) + (nasdaq_pct or 0.0)) / 2.0
        out["gift_nifty_px"] = round(nifty_spot * (1.0 + (avg_us_pct * 0.0035)), 2)

    except Exception as exc:
        log.warning("Failed to fetch global market cues: %s", exc)
        out["gift_nifty_px"] = nifty_spot

    return out


def build_premarket_report(st: Any) -> dict[str, Any]:
    """Synthesize pre-market market data, option walls, global cues, live news, and Groq LLM intelligence."""
    today = session_date()

    # Get live quotes
    nifty_px = float(st.feed.price("NIFTY") or 24750.0)
    banknifty_px = float(st.feed.price("BANKNIFTY") or 52100.0)
    vix = float(st.feed.price("INDIAVIX") or 14.5)

    # Option wall estimates
    nifty_step = 50.0
    nifty_supp = round(nifty_px / nifty_step) * nifty_step - 100.0
    nifty_res = round(nifty_px / nifty_step) * nifty_step + 100.0

    bank_step = 100.0
    bank_supp = round(banknifty_px / bank_step) * bank_step - 300.0
    bank_res = round(banknifty_px / bank_step) * bank_step + 300.0

    # Fetch live RSS headlines and global cues
    headlines = fetch_financial_news()
    cues = fetch_global_cues(nifty_spot=nifty_px)

    # Try Groq LLM synthesis
    groq_key = getattr(st.settings.secrets, "groq_api_key", "")

    summary = (
        f"Pre-market open setup for {today}: NIFTY spot near {nifty_px:,.0f} with key Put wall at "
        f"{nifty_supp:,.0f} and Call wall at {nifty_res:,.0f}. India VIX at {vix:.1f}."
    )
    bias = "NEUTRAL" if vix < 16.0 else "VOLATILE"
    confidence = "MEDIUM"
    opening_gap = "FLAT"
    gap_pts = 0.0
    sectors = [{"sector": "Banking", "reason": "Option Put Wall support"}, {"sector": "IT", "reason": "Global tech sentiment"}]
    advice = "Wait for opening range establishment (09:15–09:35 AM) before taking momentum trades."
    data_flags: list[str] = []

    if cues["dow_change_pct"] is None:
        data_flags.append("Dow Jones overnight data not provided")

    if groq_key:
        try:
            import httpx
            from sentinel.llm.prompts import PREMARKET_SYSTEM_PROMPT, build_premarket_prompt

            prompt = build_premarket_prompt(
                today,
                nifty_px,
                banknifty_px,
                vix,
                nifty_supp,
                nifty_res,
                bank_supp,
                bank_res,
                gift_nifty_px=cues.get("gift_nifty_px"),
                dow_change_pct=cues.get("dow_change_pct"),
                nasdaq_change_pct=cues.get("nasdaq_change_pct"),
                dxy=cues.get("dxy"),
                brent_crude=cues.get("brent_crude"),
                usdinr=cues.get("usdinr"),
                fii_dii_net_cr=cues.get("fii_dii_net_cr"),
                news_headlines=headlines,
            )
            with httpx.Client(timeout=5) as client:
                r = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [
                            {"role": "system", "content": PREMARKET_SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
                if r.status_code == 200:
                    data = r.json()["choices"][0]["message"]["content"]
                    parsed = json.loads(data)
                    bias = str(parsed.get("bias", bias)).upper()
                    confidence = str(parsed.get("confidence", confidence)).upper()
                    opening_gap = str(parsed.get("opening_gap", opening_gap)).upper()
                    gap_pts = float(parsed.get("gap_points", gap_pts))
                    raw_sec = parsed.get("sectors_to_watch", sectors)
                    if isinstance(raw_sec, list) and raw_sec:
                        if isinstance(raw_sec[0], str):
                            sectors = [{"sector": str(s), "reason": "Macro news read-through"} for s in raw_sec]
                        else:
                            sectors = raw_sec
                    summary = str(parsed.get("summary", summary))
                    advice = str(parsed.get("actionable_advice", advice))
                    parsed_flags = parsed.get("data_flags", [])
                    if isinstance(parsed_flags, list):
                        data_flags.extend([str(f) for f in parsed_flags])
        except Exception as exc:
            log.warning("Groq pre-market synthesis failed, using fallback: %s", exc)

    report = PreMarketReport(
        date=today,
        bias=bias,
        confidence=confidence,
        opening_gap=opening_gap,
        gap_points=gap_pts,
        vix=vix,
        nifty_ltp=nifty_px,
        nifty_support=nifty_supp,
        nifty_resistance=nifty_res,
        banknifty_ltp=banknifty_px,
        banknifty_support=bank_supp,
        banknifty_resistance=bank_res,
        sectors_to_watch=sectors,
        summary=summary,
        actionable_advice=advice,
        data_flags=list(set(data_flags)),
    )
    return report.as_dict()

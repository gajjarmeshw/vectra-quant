"""Day-end report + journal stats. Design §3.5 / §3.6, build plan Phase 5.

The suggested-vs-taken diff is the point: it tells you whether your rejects were
good rejects, which is the only honest way to judge the AI layer.
"""
from __future__ import annotations

from typing import Any

from vectra_quant.core.session_clock import session_date
from vectra_quant.db import LlmCall, PnlCurve, Suggestion, Trade, Violation, session
from vectra_quant.logging_setup import get

log = get("reports.dayend")


def build_dayend_report(st: Any, date: str | None = None) -> dict[str, Any]:
    day = date or session_date()
    with session() as s:
        trades = list(s.query(Trade).filter(Trade.session_date == day)
                      .order_by(Trade.opened_at.asc()).all())
        curve = list(s.query(PnlCurve).filter(PnlCurve.session_date == day)
                     .order_by(PnlCurve.ts.asc()).all())
        violations = list(s.query(Violation).filter(Violation.session_date == day).all())
        llm_rows = list(s.query(LlmCall).filter(LlmCall.session_date == day).all())
        suggestions = list(s.query(Suggestion)
                           .filter(Suggestion.trading_symbol != "")
                           .order_by(Suggestion.ts.asc()).all())
        suggestions = [q for q in suggestions
                       if q.ts and q.ts.strftime("%Y-%m-%d") == day]

    closed = [t for t in trades if t.status == "CLOSED"]
    realized = round(sum(t.pnl for t in closed), 2)
    costs = round(sum(t.costs for t in closed), 2)
    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl < 0]

    state_path: list[str] = []
    for c in curve:
        if not state_path or state_path[-1] != c.state:
            state_path.append(c.state)

    ignored = [q for q in suggestions if q.status in ("REJECTED", "EXPIRED")]
    good_rejects = sum(1 for q in ignored if q.outcome == "LOSS")

    discipline = _discipline_score(len(trades), len(violations))

    return {
        "date": day,
        "verdict": {
            "realized": realized,
            "costs": costs,
            "gross": round(realized + costs, 2),
            "state_path": state_path or ["NORMAL"],
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        },
        "replay": [{
            "symbol": t.trading_symbol, "origin": t.origin,
            "entry": round(t.entry_price, 2), "exit": round(t.exit_price, 2),
            "lots": t.lots, "gross": round(t.gross_pnl, 2),
            "costs": round(t.costs, 2), "net": round(t.pnl, 2),
            "reason": t.exit_reason, "guardian": t.guardian_note,
            "llm_said": _llm_said(suggestions, t.suggestion_id),
        } for t in closed],
        "suggested_vs_taken": {
            "suggested": len([q for q in suggestions if q.action == "SUGGEST"]),
            "taken": len([q for q in suggestions if q.status == "TAKEN"]),
            "ignored": len(ignored),
            "good_rejects": good_rejects,
            "note": (f"{len(ignored)} suggestion(s) ignored; {good_rejects} would have lost"
                     if ignored else "no suggestions ignored"),
        },
        "discipline": {
            "score": discipline,
            "violations": len(violations),
            "detail": [{"kind": v.kind, "detail": v.detail,
                        "cost": round(v.rupee_cost, 2)} for v in violations],
        },
        "calibration": calibration_table(),
        "llm": {
            "calls": len(llm_rows),
            "failures": sum(1 for r in llm_rows if not r.ok),
            "by_provider": _by(llm_rows, "provider"),
            "avg_latency_ms": (round(sum(r.latency_ms for r in llm_rows) / len(llm_rows))
                               if llm_rows else 0),
        },
        "tomorrow": _tomorrow_note(st),
    }


def _llm_said(suggestions: list, sid: str | None) -> dict[str, Any]:
    if not sid:
        return {"origin": "manual — no LLM thesis"}
    for q in suggestions:
        if q.id == sid:
            return {"confidence": q.confidence, "thesis": q.thesis,
                    "invalidation": q.invalidation, "rr": q.risk_reward}
    return {}


def _discipline_score(trades: int, violations: int) -> float:
    if trades == 0:
        return 100.0 if violations == 0 else 0.0
    return round(max(0.0, (1 - violations / max(trades, 1)) * 100), 1)


def _by(rows: list, field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = getattr(r, field, "") or "unknown"
        out[k] = out.get(k, 0) + 1
    return out


def _tomorrow_note(st: Any) -> dict[str, Any]:
    try:
        names = [st.settings.instruments.primary, st.settings.instruments.secondary]
        return {"expiry_today": st.instruments.expiring_today(names),
                "next_expiry": {n: st.instruments.current_expiry(n) for n in names}}
    except Exception:
        return {}


def calibration_table() -> dict[str, Any]:
    """Confidence bucket x hit rate. The kill-criterion lives here (§6.3)."""
    buckets = {"70-79": [0, 0], "80-89": [0, 0], "90+": [0, 0]}
    with session() as s:
        rows = list(s.query(Suggestion)
                    .filter(Suggestion.action == "SUGGEST",
                            Suggestion.outcome != "").all())
    for q in rows:
        c = q.confidence
        key = "70-79" if 70 <= c < 80 else ("80-89" if 80 <= c < 90 else ("90+" if c >= 90 else None))
        if key is None:
            continue
        buckets[key][1] += 1
        if q.outcome == "WIN":
            buckets[key][0] += 1

    table = {}
    for k, (wins, n) in buckets.items():
        table[k] = {"wins": wins, "n": n,
                    "hit_pct": round(wins / n * 100, 1) if n else None}

    total_70plus = sum(v["n"] for v in table.values())
    wins_70plus = sum(v["wins"] for v in table.values())
    hit_70plus = (wins_70plus / total_70plus * 100) if total_70plus else None
    # §6.3: below 45% over >=60 samples, the AI is not adding edge.
    kill = bool(total_70plus >= 60 and hit_70plus is not None and hit_70plus < 45)

    return {"buckets": table, "samples": total_70plus,
            "hit_pct_70plus": round(hit_70plus, 1) if hit_70plus is not None else None,
            "kill_criterion_tripped": kill,
            "recommendation": ("demote AI to commentary-only — 70+ bucket is under 45% "
                               "over 60+ samples" if kill else "keep AI active")}


def build_journal(*, limit: int = 200) -> dict[str, Any]:
    """Expectancy, PF, win%, equity curve, violation ledger (§3.6)."""
    with session() as s:
        closed = list(s.query(Trade).filter(Trade.status == "CLOSED")
                      .order_by(Trade.closed_at.asc()).limit(limit).all())
        violations = list(s.query(Violation).order_by(Violation.ts.desc()).limit(100).all())

    pnls = [t.pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    equity, running = [], 0.0
    for t in closed:
        running += t.pnl
        equity.append({"ts": t.closed_at.isoformat() if t.closed_at else None,
                       "equity": round(running, 2)})

    return {
        "stats": {
            "trades": len(closed),
            "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
            "win_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
            "total_costs": round(sum(t.costs for t in closed), 2),
            "net": round(sum(pnls), 2),
        },
        "equity_curve": equity,
        "calibration": calibration_table(),
        "by_origin": {
            "ai": _origin_stats([t for t in closed if t.origin == "ai"]),
            "manual": _origin_stats([t for t in closed if t.origin == "manual"]),
        },
        "violations": [{
            "date": v.session_date, "kind": v.kind, "detail": v.detail,
            "cost": round(v.rupee_cost, 2),
        } for v in violations],
        "trades": [{
            "id": t.id, "date": t.session_date, "symbol": t.trading_symbol,
            "origin": t.origin, "lots": t.lots, "entry": round(t.entry_price, 2),
            "exit": round(t.exit_price, 2), "net": round(t.pnl, 2),
            "reason": t.exit_reason,
        } for t in reversed(closed)],
    }


def _origin_stats(trades: list) -> dict[str, Any]:
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    return {
        "trades": len(trades),
        "net": round(sum(pnls), 2) if pnls else 0.0,
        "win_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "expectancy": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
    }

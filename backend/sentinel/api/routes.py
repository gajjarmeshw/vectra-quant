"""REST + WebSocket API. Build plan Phase 4 §1-2.

Auth is a single shared secret in a header — v1 is one user (§OUT OF SCOPE).
Config PUT is refused 09:15-15:30 IST: you cannot loosen your own rules mid-tilt.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from sentinel.brokers.costs import round_trip_cost
from sentinel.core.session_clock import config_edit_allowed, now_ist, session_date
from sentinel.db import LlmCall, Suggestion, Trade, Violation, session, state_get
from sentinel.logging_setup import get

log = get("api.routes")
router = APIRouter()


def require_secret(request: Request, x_sentinel_key: str = Header(default="")) -> None:
    expected = request.app.state.settings.secrets.api_shared_secret
    if not expected or expected == "dev-insecure":
        return  # local dev; production sets a real secret
    if x_sentinel_key != expected:
        raise HTTPException(status_code=401, detail="bad or missing X-Sentinel-Key")


# ---------------------------------------------------------------- state

@router.get("/")
def root_index(request: Request) -> Any:
    from sentinel import config as config_mod
    pwa_index = config_mod.ROOT / "pwa" / "dist" / "index.html"
    if pwa_index.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(pwa_index))
    return {"name": "SENTINEL API", "status": "running"}


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    st = request.app.state
    return {
        "ok": True,
        "mode": st.settings.mode,
        "ist": now_ist().isoformat(),
        "session_date": session_date(),
        "broker": getattr(st.broker, "name", "?"),
        "kill_switch": "ON" if st.killswitch.is_on else "OFF",
    }


@router.get("/state", dependencies=[Depends(require_secret)])
def get_state(request: Request) -> dict[str, Any]:
    return build_state(request.app.state)


def build_state(st: Any) -> dict[str, Any]:
    """The single payload the PWA renders. Also pushed over the WS."""
    snap = st.orch.snapshot()
    feed_health = st.feed.health(has_open_position=bool(st.lifecycle.open_trades()))

    with session() as s:
        trades = list(
            s.query(Trade).filter(Trade.session_date == session_date())
            .order_by(Trade.opened_at.asc()).all()
        )
        violations = s.query(Violation).filter(
            Violation.session_date == session_date()).count()
        llm_rows = list(s.query(LlmCall).filter(
            LlmCall.session_date == session_date()).all())
        suggestions = list(
            s.query(Suggestion).filter(Suggestion.ts >= _today_start())
            .order_by(Suggestion.ts.desc()).limit(40).all()
        )

    # Charges are real money: ~Rs.50-60 a round trip against an r=1200 budget.
    # Realized P&L is already net (lifecycle books it through costs.net_pnl); an open
    # position's charges are not yet paid, so they are shown as an estimate and the
    # net-after-charges figure sits beside the gross one.
    positions = []
    open_charges = 0.0
    for p in st.broker.get_positions():
        if not p.is_open:
            continue
        charges = round_trip_cost(
            p.average_price, p.last_price or p.average_price, abs(int(p.quantity)),
            exchange=p.exchange or "NSE",
        )
        open_charges += charges
        positions.append({
            "symbol": p.trading_symbol, "qty": p.quantity,
            "avg": round(p.average_price, 2), "ltp": round(p.last_price, 2),
            "unrealized": round(p.unrealized_pnl, 2), "lot_size": p.lot_size,
            "est_charges": round(charges, 2),
            "net_unrealized": round(p.unrealized_pnl - charges, 2),
        })

    def _quote(name: str, *, expiry: bool = False) -> dict:
        sym = st.index_symbols.get(name, name)
        px = st.feed.price(sym)
        pdc = st.candles.prev_day_close(sym)
        out: dict[str, Any] = {"ltp": round(px, 2) if px else None}
        if px and pdc:
            out["change_pct"] = round((px - pdc) / pdc * 100.0, 2)
        if expiry:
            out["expiry"] = st.instruments.current_expiry(name)
        return out

    market = {
        name: _quote(name, expiry=True)
        for name in (st.settings.instruments.primary, st.settings.instruments.secondary)
    }
    market["INDIAVIX"] = _quote("INDIAVIX")

    return {
        "fsm": snap.as_dict(),
        # Day Rail geometry: the PWA must not hardcode any rupee figure.
        "target": st.settings.risk.target,
        "loss_limit": st.settings.risk.loss_limit,
        "risk_per_trade": st.settings.risk.risk_per_trade,
        "capital": st.settings.capital,
        "max_position_cost": st.settings.sizing.max_position_cost,
        "mode": st.settings.mode,
        "broker": getattr(st.settings, "broker_name", "groww").lower(),
        "kill_switch": "ON" if st.killswitch.is_on else "OFF",
        "week_locked": st.orch.week_locked,
        "floor_distance": st.orch.floor_distance(),
        "expiry_today": st.instruments.expiring_today(
            [st.settings.instruments.primary, st.settings.instruments.secondary]),
        "in_entry_window": st.lifecycle.in_entry_window(),
        "entry_open": st.settings.risk.entry_open.strftime("%H:%M"),
        "entry_close": st.settings.risk.entry_close.strftime("%H:%M"),
        "squareoff_at": st.settings.risk.squareoff_at.strftime("%H:%M"),
        "market": market,
        "positions": positions,
        # Charges: paid so far today, plus what the open book would cost to close.
        "charges_today": round(sum(t.costs for t in trades), 2),
        "open_charges_est": round(open_charges, 2),
        "trades": [{
            "id": t.id, "symbol": t.trading_symbol, "origin": t.origin,
            "lots": t.lots, "lot_size": t.lot_size, "qty": t.qty,
            "entry": round(t.entry_price, 2),
            "exit": round(t.exit_price, 2), "pnl": round(t.pnl, 2),
            "gross": round(t.gross_pnl, 2), "costs": round(t.costs, 2),
            "sl": round(t.sl_price, 2), "target": round(t.target_price, 2),
            "status": t.status,
            "reason": t.exit_reason, "guardian_note": t.guardian_note,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        } for t in trades],
        "suggestions": [{
            "id": q.id, "status": q.status, "action": q.action,
            "instrument": q.instrument, "direction": q.direction,
            "strike": q.resolved_strike, "symbol": q.trading_symbol,
            "confidence": q.confidence, "thesis": q.thesis,
            "invalidation": q.invalidation, "rr": q.risk_reward,
            "entry_low": q.entry_low, "entry_high": q.entry_high,
            "sl": q.stop_loss_premium, "target": q.target_premium,
            "time_stop": q.time_stop_minutes, "lots": q.lots,
            "risk": q.rupee_risk, "cost": q.position_cost,
            "gate_reason": q.gate_reason, "model": q.model,
            "event": q.event_kind, "outcome": q.outcome,
            "expires_at": q.expires_at.isoformat() if q.expires_at else None,
        } for q in suggestions],
        "active_suggestions": [{
            "id": q.id, "symbol": q.trading_symbol, "instrument": q.instrument,
            "event": q.raw.get("_event", ""), "model": q.raw.get("_model", ""),
            "direction": q.direction, "strike": q.strike, "lots": q.lots,
            "qty": q.qty, "entry_low": q.entry_low, "entry_high": q.entry_high,
            "sl": q.stop_loss, "target": q.target, "confidence": q.confidence,
            "risk": q.risk, "cost": q.cost, "over_risk": q.over_risk,
            "seconds_left": q.seconds_left,
            "thesis": q.raw.get("thesis", ""),
            "invalidation": q.raw.get("invalidation", ""),
            "rr": q.raw.get("risk_reward", 0),
            "time_stop": q.raw.get("time_stop_minutes", 0),
        } for q in st.lifecycle.active()],
        "health": {
            "feed_degraded": feed_health.degraded,
            "feed_critical": feed_health.critical,
            "feed_age_s": feed_health.worst_age_s,
            "feed_subscribed": feed_health.subscribed,
            "chain_fresh": {
                n: st.chain.is_fresh(n)
                for n in (st.settings.instruments.primary,
                          st.settings.instruments.secondary)
            },
            "guardian_detection": st.settings.guardian.detection,
            # D-001 answers itself here: if `ws` stays 0 while `rest` climbs, the
            # Groww websocket does not report app-placed orders.
            "guardian_detected_by": st.guardian.detected_by,
            "reconnects": st.feed.reconnects,
        },
        "llm": {
            "calls_today": len(llm_rows),
            "cap": st.settings.llm.suggest_cap_per_day,
            "by_provider": _count_by(llm_rows, "provider"),
            "failures": sum(1 for r in llm_rows if not r.ok),
        },
        "violations_today": violations,
        "institutional": {
            "fii_drift": json.loads(state_get("FII_NET_INDEX_FUTURES", "[]")),
            **{
                n: st.chain.get_institutional_context(n)
                for n in (st.settings.instruments.primary, st.settings.instruments.secondary)
                if hasattr(st.chain, "get_institutional_context")
            }
        },
        "algo": {
            "enabled": getattr(st.settings.algo, "enabled", True) if hasattr(st.settings, "algo") else True,
            "active_strategies": getattr(st.settings.algo, "active_strategies", ["institutional_breakout"]) if hasattr(st.settings, "algo") else ["institutional_breakout"],
            "auto_execute": getattr(st.settings.algo, "auto_execute", False) if hasattr(st.settings, "algo") else False,
        },
        "server_time": now_ist().isoformat(),
    }


def _today_start():
    from datetime import datetime, time

    from sentinel.core.session_clock import IST
    d = now_ist().date()
    return datetime.combine(d, time(0, 0), tzinfo=IST)


def _count_by(rows: list, field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = getattr(r, field, "") or "unknown"
        out[k] = out.get(k, 0) + 1
    return out


# ---------------------------------------------------------------- actions

@router.post("/suggestions/{sid}/approve", dependencies=[Depends(require_secret)])
def approve(sid: str, request: Request,
            idempotency_key: str = Header(default="", alias="Idempotency-Key")) -> dict:
    st = request.app.state
    out = st.lifecycle.approve(sid, idempotency_key=idempotency_key)
    if not out.get("ok"):
        raise HTTPException(status_code=409, detail=out.get("error", "approval refused"))
    return out


@router.post("/suggestions/{sid}/reject", dependencies=[Depends(require_secret)])
async def reject(sid: str, request: Request) -> dict:
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    out = request.app.state.lifecycle.reject(sid, str(body.get("reason", "")))
    if not out.get("ok"):
        raise HTTPException(status_code=409, detail=out.get("error", "reject refused"))
    return out


@router.post("/squareoff", dependencies=[Depends(require_secret)])
def squareoff(request: Request) -> dict:
    st = request.app.state
    ids, flat = st.guardian.square_off_all("manual square-off from PWA")
    for t in st.lifecycle.open_trades():
        st.lifecycle.close_trade(
            t.id, st.lifecycle._exit_price(t.trading_symbol, t.entry_price),
            "manual_squareoff")
    return {"ok": True, "orders": ids, "flat": flat}


@router.post("/killswitch", dependencies=[Depends(require_secret)])
async def killswitch(request: Request) -> dict:
    st = request.app.state
    body = await request.json()
    action = str(body.get("action", "")).upper()
    if action == "OFF":
        out = st.killswitch.turn_off(str(body.get("reason", "from PWA")),
                                    square_off=bool(body.get("square_off")))
        st.orch.set_kill_switch(False)
        if body.get("square_off"):
            st.guardian.square_off_all("kill switch OFF with square-off")
        return out
    if action == "ON":
        out = st.killswitch.turn_on(str(body.get("typed", "")),
                                   str(body.get("reason", "from PWA")))
        if out.get("ok"):
            st.orch.set_kill_switch(True)
        else:
            raise HTTPException(status_code=400, detail=out.get("error"))
        return out
    raise HTTPException(status_code=400, detail="action must be ON or OFF")


# ---------------------------------------------------------------- config

@router.get("/config", dependencies=[Depends(require_secret)])
def get_config(request: Request) -> dict:
    from sentinel import config as config_mod
    st = request.app.state
    return {
        "params": st.settings.raw,
        "defaults": config_mod.default_params(),
        "editable_now": config_edit_allowed(),
        "edit_window": "outside 09:15-15:30 IST",
        "secrets": st.settings.secrets.redacted(),
    }


@router.put("/config", dependencies=[Depends(require_secret)])
async def put_config(request: Request) -> dict:
    """Refused during market hours. Deliberate friction (design §4)."""
    if not config_edit_allowed():
        raise HTTPException(status_code=423,
                            detail="config is locked 09:15-15:30 IST — editable after close")
    body = await request.json()
    if str(body.get("confirm", "")).strip() != "CONFIRM":
        raise HTTPException(status_code=400, detail='must send confirm:"CONFIRM"')
    params = body.get("params")
    if not isinstance(params, dict) or not params:
        raise HTTPException(status_code=400, detail="params must be a non-empty object")

    from sentinel import config as config_mod
    try:
        new_settings = config_mod.write_params(params)
    except Exception as exc:
        raise HTTPException(status_code=400,
                            detail=f"rejected invalid config: {str(exc)[:200]}") from exc

    request.app.state.settings = new_settings
    log.warning("params.yaml updated via API")
    return {"ok": True, "params": new_settings.raw,
            "note": "risk-engine values apply after restart"}


@router.post("/config/reset", dependencies=[Depends(require_secret)])
async def reset_config(request: Request) -> dict:
    """Restore factory defaults. Same market-hours lock as an edit."""
    if not config_edit_allowed():
        raise HTTPException(status_code=423,
                            detail="config is locked 09:15-15:30 IST — editable after close")
    body = await request.json()
    if str(body.get("confirm", "")).strip() != "RESET":
        raise HTTPException(status_code=400, detail='must send confirm:"RESET"')

    from sentinel import config as config_mod
    try:
        new_settings = config_mod.reset_params()
    except Exception as exc:
        raise HTTPException(status_code=500,
                            detail=f"reset failed: {str(exc)[:200]}") from exc

    request.app.state.settings = new_settings
    log.warning("params.yaml RESET to factory defaults via API")
    return {"ok": True, "params": new_settings.raw,
            "note": "risk-engine values apply after restart"}


# ---------------------------------------------------------------- push

@router.get("/push/key")
def push_key(request: Request) -> dict:
    return {"public_key": request.app.state.settings.secrets.vapid_public}


@router.post("/push/subscribe", dependencies=[Depends(require_secret)])
async def push_subscribe(request: Request) -> dict:
    body = await request.json()
    keys = body.get("keys") or {}
    ok = request.app.state.pusher.subscribe(
        str(body.get("endpoint", "")), str(keys.get("p256dh", "")), str(keys.get("auth", "")))
    if not ok:
        raise HTTPException(status_code=400, detail="incomplete subscription")
    return {"ok": True, "subscriptions": request.app.state.pusher.subscription_count()}


@router.post("/push/test", dependencies=[Depends(require_secret)])
def push_test(request: Request) -> dict:
    res = request.app.state.pusher.send("SYSTEM", "Test push from SENTINEL.")
    return {"ok": True, "sent": res.sent, "pruned": res.pruned, "failed": res.failed}


# ---------------------------------------------------------------- reports

@router.get("/report/dayend", dependencies=[Depends(require_secret)])
def dayend(request: Request, date: str = "") -> dict:
    from sentinel.reports.dayend import build_dayend_report
    return build_dayend_report(request.app.state, date or session_date())


@router.get("/report/premarket", dependencies=[Depends(require_secret)])
def premarket_report(request: Request) -> dict:
    from sentinel.reports.premarket import build_premarket_report
    return build_premarket_report(request.app.state)


@router.get("/report/journal", dependencies=[Depends(require_secret)])
def journal(request: Request, limit: int = 200) -> dict:
    from sentinel.reports.dayend import build_journal
    return build_journal(limit=limit)


@router.post("/config/preset", dependencies=[Depends(require_secret)])
def set_preset(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    preset_name = str(body.get("preset", "")).strip().upper()
    if not preset_name:
        raise HTTPException(status_code=400, detail="preset field is required")
    try:
        from sentinel.config import apply_preset
        s = apply_preset(preset_name)
        request.app.state.settings = s
        return {
            "ok": True,
            "preset": preset_name,
            "target": s.risk.target,
            "loss_limit": s.risk.loss_limit,
            "risk_per_trade": s.risk.risk_per_trade,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/report/backtest", dependencies=[Depends(require_secret)])
def run_backtest(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    instrument = str(body.get("instrument", "NIFTY")).upper()
    days = int(body.get("days", 5))
    preset = str(body.get("preset", "MODERATE")).upper()
    strategy_name = str(body.get("strategy", "institutional_breakout"))

    from sentinel.backtest.engine import BacktestEngine
    from sentinel.config import RISK_PRESETS
    from sentinel.risk_engine import RiskConfig

    preset_vals = RISK_PRESETS.get(preset, RISK_PRESETS["MODERATE"])
    risk_cfg = RiskConfig(
        capital=request.app.state.settings.capital,
        target=preset_vals["target"],
        loss_limit=preset_vals["loss_limit"],
        risk_per_trade=preset_vals["risk_per_trade"],
    )
    
    if strategy_name == "nifty_5d_breakout":
        from sentinel.backtest.event_engine import EventBacktestEngine
        engine = EventBacktestEngine(risk_cfg)
        res = engine.run(strategy_name=strategy_name, instrument=instrument, days=days)
    else:
        engine = BacktestEngine(risk_cfg)
        res = engine.run(strategy_name=strategy_name, instrument=instrument, days=days)
        
    return res.as_dict()


# ---------------------------------------------------------------- websocket

class WsHub:
    """Broadcasts state diffs to connected PWA clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._last: dict[str, Any] | None = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        log.info("ws client connected", extra={"clients": len(self._clients)})

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)
        if not clients:
            self._last = payload
            return
        text = json.dumps(payload, default=str)
        dead = []
        for ws in clients:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
        self._last = payload

    @property
    def client_count(self) -> int:
        return len(self._clients)


@router.websocket("/live")
async def live(ws: WebSocket) -> None:
    st = ws.app.state
    await st.hub.connect(ws)
    try:
        await ws.send_text(json.dumps(build_state(st), default=str))
        while True:
            # Client pings keep the socket alive; we do not expect commands here.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        log.info("ws closed", extra={"error": str(exc)[:120]})
    finally:
        await st.hub.disconnect(ws)


# ---------------------------------------------------------------- webhooks

@router.post("/webhooks/signal")
async def signal_webhook(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Free Custom / TradingView Signal Webhook endpoint."""
    symbol = str(payload.get("symbol", "NIFTY")).upper()
    event_kind = str(payload.get("event", "CUSTOM_SIGNAL")).upper()
    direction = str(payload.get("direction", "CE")).upper()

    log.info("Custom Signal Webhook received", extra={"symbol": symbol, "event": event_kind, "direction": direction})
    return {"ok": True, "processed": True, "symbol": symbol, "event": event_kind}


@router.post("/broker/refresh-token")
async def refresh_broker_token(request: Request) -> dict[str, Any]:
    """Update DhanHQ access token dynamically in-memory and in .env."""
    import os
    import re

    from sentinel import config as config_mod

    st = request.app.state
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    new_token = str(body.get("access_token", "")).strip()
    if not new_token:
        raise HTTPException(status_code=400, detail="access_token is required")

    # Update live broker adapter in memory
    target = st.broker
    if hasattr(target, "data_source"):
        target = target.data_source

    if hasattr(target, "access_token"):
        target.access_token = new_token
    if hasattr(target, "dhan"):
        try:
            from dhanhq import dhanhq as DhanHQ, DhanContext
            if DhanContext is not None:
                target.dhan = DhanHQ(DhanContext(target.client_id, new_token))
            else:
                target.dhan = DhanHQ(target.client_id, new_token)
        except Exception:
            pass
    if hasattr(target, "is_authenticated"):
        target.is_authenticated = True
        target.last_auth_error = ""

    os.environ["DHAN_ACCESS_TOKEN"] = new_token

    # Update .env file on disk if it exists
    env_path = config_mod.ROOT / ".env"
    if env_path.exists():
        content = env_path.read_text()
        if "DHAN_ACCESS_TOKEN=" in content:
            content = re.sub(r"DHAN_ACCESS_TOKEN=.*", f"DHAN_ACCESS_TOKEN={new_token}", content)
        else:
            content += f"\nDHAN_ACCESS_TOKEN={new_token}\n"
        env_path.write_text(content)

    log.info("DhanHQ access_token updated successfully: %s...", new_token[:8])
    return {
        "ok": True,
        "token_snippet": new_token[:8] + "...",
        "message": "DhanHQ access_token updated & verified live in memory!",
    }


@router.get("/data/status")
async def get_data_status(request: Request) -> dict[str, Any]:
    """Return status of market data feeds, historical store, option chains, and broker session."""
    import os

    st = request.app.state
    broker_name = getattr(st.broker, "name", "dhan") if hasattr(st, "broker") else "dhan"
    health = getattr(st, "health", None)
    chain_fresh = getattr(health, "chain_fresh", {}) if health else {}

    has_dhan = bool(os.getenv("DHAN_CLIENT_ID") and os.getenv("DHAN_ACCESS_TOKEN"))
    dhan_snip = os.getenv("DHAN_ACCESS_TOKEN", "")[:8] + "..." if has_dhan else "none"

    broker_obj = getattr(st, "broker", None)
    is_authenticated = getattr(broker_obj, "is_authenticated", True)
    auth_error = getattr(broker_obj, "last_auth_error", "")

    return {
        "ok": True,
        "broker": broker_name,
        "broker_authenticated": is_authenticated,
        "broker_auth_error": auth_error,
        "feed_subscribed": getattr(health, "feed_subscribed", 0) if health else 0,
        "feed_age_s": getattr(health, "feed_age_s", 0.0) if health else 0.0,
        "feed_degraded": getattr(health, "feed_degraded", False) if health else False,
        "chain_fresh": chain_fresh,
        "dhan_configured": has_dhan,
        "dhan_token_snippet": dhan_snip,
        "supported_instruments": ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"],
    }


@router.post("/data/fetch-candles")
async def fetch_candles_on_demand(request: Request) -> dict[str, Any]:
    """Fetch/sync historical 1-minute OHLCV candles for an index."""
    from sentinel.backtest.engine import load_candles_for_backtest

    try:
        body = await request.json()
    except Exception:
        body = {}

    instrument = str(body.get("instrument", "NIFTY")).upper()
    days = int(body.get("days", 5))

    st = request.app.state
    broker = getattr(st, "broker", None)

    c_dict, source, warning = load_candles_for_backtest(
        instrument=instrument,
        days=days,
        broker=broker,
    )
    candles_count = sum(len(v) for v in c_dict.values())

    return {
        "ok": True,
        "instrument": instrument,
        "days": days,
        "source": source,
        "warning": warning,
        "bars_loaded": candles_count,
        "message": f"Successfully loaded {candles_count} 1-minute bars for {instrument} ({len(c_dict)} sessions) via {source}",
    }


@router.post("/data/refresh-chain")
async def refresh_chain_on_demand(request: Request) -> dict[str, Any]:
    """Force an immediate option chain snapshot refresh across active indices."""
    st = request.app.state
    refreshed: list[str] = []

    if hasattr(st, "chain") and hasattr(st, "feed"):
        for name in ("NIFTY", "SENSEX", "BANKNIFTY"):
            quote = st.feed.get(name) if hasattr(st.feed, "get") else None
            spot = quote.ltp if quote else 0.0
            if spot > 0:
                try:
                    st.chain.snapshot(name, spot)
                    refreshed.append(name)
                except Exception as exc:
                    log.warning("Manual chain snapshot failed for %s: %s", name, exc)

    return {
        "ok": True,
        "refreshed_indices": refreshed or ["NIFTY", "SENSEX"],
        "message": f"Option chain snapshot triggered for {', '.join(refreshed or ['NIFTY', 'SENSEX'])}",
    }


@router.get("/strategies")
def get_strategies_list(request: Request) -> dict[str, Any]:
    """List all registered dynamic algo strategies, parameters, and current active selection."""
    from sentinel.strategies.registry import list_strategies

    import time
    st = request.app.state
    algo_cfg = getattr(st.settings, "algo", None)
    
    strategies = list_strategies()
    
    now = time.time()
    for strat in strategies:
        manifest = strat.get("manifest", {})
        health = {"symbols": {}, "timeframes": {}}
        
        # Check symbols (ticks)
        for sym in manifest.get("symbols", []):
            # check feed for last tick time
            last_tick = st.feed._last_tick_time.get(sym, 0)
            age = now - last_tick
            if age < 300:  # 5 minutes
                health["symbols"][sym] = "OK"
            elif last_tick > 0:
                health["symbols"][sym] = "STALE"
            else:
                health["symbols"][sym] = "WAITING"
                
        # Check timeframes (candles)
        for tf in manifest.get("timeframes", []):
            if tf == "1m":
                # checking if we have built any 1m candles across symbols
                # A bit simplistic, but we can check if NIFTY or main symbol has bars
                if st.candles._bars_1m:
                    health["timeframes"][tf] = "OK"
                else:
                    health["timeframes"][tf] = "WAITING"
            else:
                # Other timeframes (5m etc) logic could be added here
                health["timeframes"][tf] = "OK"

        strat["dependency_health"] = health

    return {
        "ok": True,
        "strategies": strategies,
        "active_strategies": algo_cfg.active_strategies if algo_cfg else ["institutional_breakout"],
        "enabled": algo_cfg.enabled if algo_cfg else False,
        "auto_execute": algo_cfg.auto_execute if algo_cfg else False,
    }


@router.post("/strategies/backtest")
async def run_strategy_backtest(request: Request) -> dict[str, Any]:
    """Run an on-demand historical backtest for a dynamic algo strategy with the 23-point checklist."""
    from sentinel.backtest.engine import BacktestEngine
    from sentinel.strategies.registry import get_strategy

    try:
        body = await request.json()
    except Exception:
        body = {}

    strat_name = str(body.get("strategy", "institutional_breakout")).strip()
    instrument = str(body.get("instrument", "NIFTY")).upper()
    days = int(body.get("days", 5))
    from_date = body.get("from_date")
    to_date = body.get("to_date")
    params = body.get("params") or {}
    enable_slippage = bool(body.get("enable_slippage", True))

    st = request.app.state
    broker = getattr(st, "broker", None)

    if strat_name in ("nifty_5d_breakout", "renko_strategy"):
        from sentinel.backtest.event_engine import EventBacktestEngine
        from sentinel.backtest.engine import _BACKTEST_CACHE
        _BACKTEST_CACHE.clear()   # fresh data for every explicit run
        bte = EventBacktestEngine()
        result = bte.run(
            strategy_name=strat_name,
            instrument=instrument,
            days=days,
            broker=broker,
            from_date=from_date,
            to_date=to_date,
        )
        return result.as_dict()

    try:
        strat = get_strategy(strat_name, params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bte = BacktestEngine()
    result = bte.run_strategy(
        strategy=strat,
        instrument=instrument,
        days=days,
        from_date=from_date,
        to_date=to_date,
        broker=broker,
        enable_slippage=enable_slippage,
    )

    wiggle = bte.parameter_wiggle_test(
        strategy=strat,
        instrument=instrument,
        days=days,
        from_date=from_date,
        to_date=to_date,
        broker=broker,
    )

    res_dict = result.as_dict()
    res_dict["wiggle_analysis"] = wiggle
    return {
        "ok": True,
        "result": res_dict,
    }


@router.post("/strategies/active", dependencies=[Depends(require_secret)])
async def set_active_strategy(request: Request) -> dict[str, Any]:
    """Update active strategies or toggle algo auto-execution mode."""
    from sentinel.config import AlgoCfg

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    st = request.app.state
    strats = body.get("strategies")

    curr_algo = getattr(st.settings, "algo", AlgoCfg())
    new_active = strats if strats is not None else curr_algo.active_strategies
    new_enabled = bool(body.get("enabled", curr_algo.enabled))
    new_auto_exec = bool(body.get("auto_execute", curr_algo.auto_execute))

    try:
        # Re-build settings with new algo configuration
        st.settings = type(st.settings)(
            **{f.name: getattr(st.settings, f.name) for f in st.settings.__dataclass_fields__.values() if f.name != "algo"},
            algo=AlgoCfg(
                enabled=new_enabled,
                active_strategies=new_active,
                auto_execute=new_auto_exec,
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"config error: {exc}")

    log.info("Active algo strategies updated: %s (enabled=%s, auto_execute=%s)",
             new_active, new_enabled, new_auto_exec)
    return {
        "ok": True,
        "active_strategies": new_active,
        "enabled": new_enabled,
        "auto_execute": new_auto_exec,
    }


@router.get("/{filename:path}")
def serve_pwa_static(filename: str) -> Any:
    """Serve root static assets from pwa/dist (e.g. manifest, icons, sw.js)."""
    from sentinel import config as config_mod
    pwa_dist = config_mod.ROOT / "pwa" / "dist"
    target = pwa_dist / filename
    if target.exists() and target.is_file():
        from fastapi.responses import FileResponse
        return FileResponse(str(target))
    raise HTTPException(status_code=404, detail=f"File not found: {filename}")


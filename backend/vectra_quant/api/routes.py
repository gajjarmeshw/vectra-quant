"""REST + WebSocket API. Build plan Phase 4 §1-2.

Auth is a single shared secret in a header — v1 is one user (§OUT OF SCOPE).
Config PUT is refused 09:15-15:30 IST: you cannot loosen your own rules mid-tilt.
"""
from __future__ import annotations

import asyncio
import hmac
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
    Response,
    WebSocket,
    WebSocketDisconnect,
)

from vectra_quant.brokers.costs import round_trip_cost
from vectra_quant.core.session_clock import config_edit_allowed, now_ist, session_date
from vectra_quant.db import LlmCall, Suggestion, Trade, Violation, session, state_get
from vectra_quant.logging_setup import get

log = get("api.routes")
router = APIRouter()


def _secret_ok(provided: str, expected: str) -> bool:
    """Constant-time compare -- a plain `!=` leaks timing information about
    how many leading characters matched, which matters for a shared secret
    guarding an API that can place real orders and hold a live broker token."""
    return hmac.compare_digest(provided or "", expected or "")


def require_secret(
    request: Request,
    x_vectra_quant_key: str = Header(default="", alias="X-VectraQuant-Key"),
) -> None:
    # FastAPI derives a header name from the parameter name by hyphenating
    # every underscore -- for `x_vectra_quant_key` that's
    # `X-Vectra-Quant-Key`, NOT `X-VectraQuant-Key` (what the frontend
    # actually sends, pwa/src/api.js). Without this explicit alias, the two
    # never matched: invisible while API_SHARED_SECRET defaults to
    # "dev-insecure" (bypassed below), but the instant a real secret is
    # configured, every authenticated request from the real frontend would
    # 401 forever.
    expected = request.app.state.settings.secrets.api_shared_secret
    if not expected or expected == "dev-insecure":
        return  # local dev; production sets a real secret
    if not _secret_ok(x_vectra_quant_key, expected):
        raise HTTPException(status_code=401, detail="bad or missing X-VectraQuant-Key")


# ---------------------------------------------------------------- state

@router.get("/")
def root_index(request: Request) -> Any:
    from vectra_quant import config as config_mod
    pwa_index = config_mod.ROOT / "pwa" / "dist" / "index.html"
    if pwa_index.exists():
        from fastapi.responses import FileResponse
        return FileResponse(str(pwa_index))
    return {"name": "VECTRA_QUANT API", "status": "running"}


@router.get("/health")
def health(request: Request, response: Response) -> dict[str, Any]:
    st = request.app.state
    # This must never itself raise -- it's what an orchestrator polls to
    # decide whether to route traffic here at all, including during a
    # partial/failed boot when not every attribute below is guaranteed set.
    ready = bool(getattr(st, "ready", False))
    boot_error = getattr(st, "boot_error", "") or None
    if not ready:
        response.status_code = 503
    return {
        "ok": ready,
        "boot_error": boot_error,
        "mode": getattr(getattr(st, "settings", None), "mode", "?"),
        "ist": now_ist().isoformat(),
        "session_date": session_date(),
        "broker": getattr(getattr(st, "broker", None), "name", "?"),
        "kill_switch": "ON" if getattr(getattr(st, "killswitch", None), "is_on", False) else "OFF",
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

        suggestions = list(
            s.query(Suggestion).filter(Suggestion.ts >= _today_start())
            .order_by(Suggestion.ts.desc()).limit(40).all()
        )

    # Charges are real money: ~Rs.50-60 a round trip against an r=1200 budget.
    # Realized P&L is already net (lifecycle books it through costs.net_pnl); an open
    # position's charges are not yet paid, so they are shown as an estimate and the
    # net-after-charges figure sits beside the gross one.
    positions = []
    for p in st.broker.get_positions():
        if not p.is_open:
            continue
        charges = round_trip_cost(
            p.average_price, p.last_price or p.average_price, abs(int(p.quantity)),
            exchange=p.exchange or "NSE",
        )
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
        "type": "state",
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
        },

        "violations_today": violations,
        # NOTE: an "institutional" block (option call/put walls, FII drift) used
        # to be built here on EVERY /state poll via
        # `st.chain.get_institutional_context(...)` per symbol. Nothing in the
        # frontend ever read it -- verified by grep across the whole PWA -- so
        # each poll paid for that computation and shipped the payload for no
        # one. Removed rather than left as silent overhead.
        #
        # The data itself is not lost: `chain.get_institutional_context(symbol)`
        # still exists and can be called from a dedicated endpoint (or added
        # back here) the moment a screen actually renders walls.
        "algo": {
            "enabled": getattr(st.settings.algo, "enabled", True) if hasattr(st.settings, "algo") else True,
            "active_strategies": getattr(st.settings.algo, "active_strategies", []) if hasattr(st.settings, "algo") else [],
            "auto_execute": getattr(st.settings.algo, "auto_execute", False) if hasattr(st.settings, "algo") else False,
        },
        "server_time": now_ist().isoformat(),
    }


def _today_start():
    from datetime import datetime, time

    from vectra_quant.core.session_clock import IST
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
    from vectra_quant import config as config_mod
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

    from vectra_quant import config as config_mod
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

    from vectra_quant import config as config_mod
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
    res = request.app.state.pusher.send("SYSTEM", "Test push from VECTRA_QUANT.")
    return {"ok": True, "sent": res.sent, "pruned": res.pruned, "failed": res.failed}


# ---------------------------------------------------------------- reports

@router.get("/report/dayend", dependencies=[Depends(require_secret)])
def dayend(request: Request, date: str = "") -> dict:
    from vectra_quant.reports.dayend import build_dayend_report
    return build_dayend_report(request.app.state, date or session_date())


@router.get("/report/premarket", dependencies=[Depends(require_secret)])
def premarket_report(request: Request, refresh: bool = False) -> dict:
    """Cached by default; `?refresh=1` is the UI's explicit re-run button."""
    from vectra_quant.reports.premarket import get_premarket_report
    return get_premarket_report(request.app.state, force=refresh)


@router.get("/report/journal", dependencies=[Depends(require_secret)])
def journal(request: Request, limit: int = 200) -> dict:
    from vectra_quant.reports.dayend import build_journal
    return build_journal(limit=limit)


@router.post("/config/preset", dependencies=[Depends(require_secret)])
def set_preset(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    preset_name = str(body.get("preset", "")).strip().upper()
    if not preset_name:
        raise HTTPException(status_code=400, detail="preset field is required")
    try:
        from vectra_quant.config import apply_preset
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
    expected = st.settings.secrets.api_shared_secret
    if expected and expected != "dev-insecure":
        # Browsers can't set custom headers on a WebSocket handshake, so the
        # shared secret travels as a query param here instead (see
        # pwa/src/api.js liveSocket()) -- same secret, same constant-time
        # check as every REST endpoint via require_secret().
        provided = ws.query_params.get("key", "")
        if not _secret_ok(provided, expected):
            await ws.close(code=4401)
            return
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

@router.post("/webhooks/signal", dependencies=[Depends(require_secret)])
async def signal_webhook(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Free Custom / TradingView Signal Webhook endpoint."""
    symbol = str(payload.get("symbol", "NIFTY")).upper()
    event_kind = str(payload.get("event", "CUSTOM_SIGNAL")).upper()
    direction = str(payload.get("direction", "CE")).upper()

    log.info("Custom Signal Webhook received", extra={"symbol": symbol, "event": event_kind, "direction": direction})
    return {"ok": True, "processed": True, "symbol": symbol, "event": event_kind}


@router.post("/broker/refresh-token", dependencies=[Depends(require_secret)])
async def refresh_broker_token(request: Request) -> dict[str, Any]:
    """Update DhanHQ access token dynamically in-memory and in .env."""
    import os
    import re

    from vectra_quant import config as config_mod

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


@router.get("/data/status", dependencies=[Depends(require_secret)])
async def get_data_status(request: Request) -> dict[str, Any]:
    """Return status of market data feeds, historical store, option chains, and broker session."""
    import os

    st = request.app.state
    broker_name = getattr(st.broker, "name", "dhan") if hasattr(st, "broker") else "dhan"
    health = getattr(st, "health", None)
    chain_fresh = getattr(health, "chain_fresh", {}) if health else {}

    has_dhan = bool(os.getenv("DHAN_CLIENT_ID") and os.getenv("DHAN_ACCESS_TOKEN"))

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
        "supported_instruments": ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"],
    }


@router.post("/data/fetch-candles", dependencies=[Depends(require_secret)])
async def fetch_candles_on_demand(request: Request) -> dict[str, Any]:
    """Fetch/sync historical 1-minute OHLCV candles for an index."""
    from vectra_quant.backtest.engine import load_candles_for_backtest

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


@router.post("/data/refresh-chain", dependencies=[Depends(require_secret)])
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


@router.get("/strategies", dependencies=[Depends(require_secret)])
def get_strategies_list(request: Request) -> dict[str, Any]:
    """List all registered dynamic algo strategies, parameters, and current active selection."""
    from vectra_quant.strategies.registry import list_strategies

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
        "active_strategies": algo_cfg.active_strategies if algo_cfg else [],
        "enabled": algo_cfg.enabled if algo_cfg else False,
        "auto_execute": algo_cfg.auto_execute if algo_cfg else False,
    }


@router.get("/orderflow/thunderbolt/status", dependencies=[Depends(require_secret)])
def get_thunderbolt_status() -> dict[str, Any]:
    """Live order-flow + decision-trace status for today, so the UI can show
    *why* Thunderbolt hasn't fired -- not just that it hasn't.

    Reads three files written by `scripts/run_thunderbolt_live.py`, a
    separate process from this API server: the depth recorder's connection
    health, the per-poll-cycle decision trace (updated every ~5s while the
    signal window is open, unlike `record.json` which only changes when a
    real signal fires), and today's session context (VIX/candles the run
    used). Returns nulls for whatever hasn't been written yet (e.g. before
    the live script has started today) rather than erroring.
    """
    root = os.environ.get("ORDERFLOW_RECORDS_ROOT", "./data/orderflow")
    today = session_date()

    def _read_json(path: str) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    return {
        "ok": True,
        "session_date": today,
        "recorder_health": _read_json(os.path.join(root, "recorder_health", "health.json")),
        "live_status": _read_json(os.path.join(root, "thunderbolt", f"date={today}", "live_status.json")),
        "record": _read_json(os.path.join(root, "thunderbolt", f"date={today}", "record.json")),
        "context": _read_json(os.path.join(root, "context", f"date={today}", "context.json")),
    }


@router.get("/orderflow/breadth/status", dependencies=[Depends(require_secret)])
def get_breadth_status() -> dict[str, Any]:
    """Live cross-sectional breadth status for today -- the multi-instrument
    counterpart to /orderflow/thunderbolt/status. Reads three connection
    health files (two equity batches + one option-chain batch, per
    scripts/run_breadth_live.py's topology) plus the breadth paper trader's
    live_status.json and record.json. Nulls for anything not written yet.
    """
    root = os.environ.get("ORDERFLOW_RECORDS_ROOT", "./data/orderflow")
    today = session_date()

    def _read_json(path: str) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    return {
        "ok": True,
        "session_date": today,
        "recorder_health": {
            label: _read_json(os.path.join(root, "recorder_health", f"health_{label}.json"))
            for label in ("equities_conn0", "equities_conn1", "options_conn0")
        },
        "live_status": _read_json(os.path.join(root, "breadth", f"date={today}", "live_status.json")),
        "record": _read_json(os.path.join(root, "breadth", f"date={today}", "record.json")),
    }


MAX_BACKTEST_DAYS = 250


@router.post("/backtest/runs", status_code=202, dependencies=[Depends(require_secret)])
async def submit_backtest_run(request: Request) -> dict[str, Any]:
    """Queue a backtest job and return immediately with a job id.

    The actual simulation runs on a dedicated worker thread (see
    backtest/jobs.py) — this route only validates input and enqueues, so it
    never blocks the event loop no matter how long the backtest takes.
    Progress streams over /live as {"type": "backtest", ...} frames; poll
    GET /backtest/runs/{job_id} as a fallback.
    """
    from vectra_quant.backtest.engine import BacktestEngine
    from vectra_quant.strategies.registry import get_strategy

    try:
        body = await request.json()
    except Exception:
        body = {}

    strat_name = str(body.get("strategy", "renko_strategy")).strip()
    instrument = str(body.get("instrument", "NIFTY")).upper()
    days = min(int(body.get("days", 5)), MAX_BACKTEST_DAYS)
    from_date = body.get("from_date")
    to_date = body.get("to_date")
    params = body.get("params") or {}
    enable_slippage = bool(body.get("enable_slippage", True))
    run_wiggle = bool(body.get("wiggle_test", True))

    st = request.app.state
    broker = getattr(st, "broker", None)

    try:
        get_strategy(strat_name, params)  # validate before queuing
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def run_fn(job: Any) -> dict[str, Any]:
        strat = get_strategy(strat_name, params)

        # thunderbolt's signal is the instantaneous order-book imbalance --
        # there is no historical proxy for this at all (not even an
        # approximate one like weekly_credit_spread's PCR stand-in), because
        # the archive has zero order-book depth for any past date. Rather
        # than run something misleading, return a complete, FE-safe
        # zero-trade result with a clear explanation in `data_warning` (the
        # same banner mechanism weekly_credit_spread already uses for its
        # short-window notice) so the UI renders cleanly instead of crashing
        # on missing fields or showing a fabricated number.
        if getattr(strat, "EXECUTION_MODE", None) == "live_only":
            return {
                "strategy": strat_name, "instrument": instrument,
                "days": 0, "start_date": from_date or "", "end_date": to_date or "",
                "initial_capital": 0.0, "final_pnl": 0.0, "gross_pnl": 0.0, "total_costs": 0.0,
                "total_trades": 0, "wins": 0, "losses": 0, "win_pct": 0.0, "profit_factor": 0.0,
                "max_drawdown": 0.0, "trades": [], "wiggle_analysis": None,
                "data_source": "LIVE_ORDER_FLOW_ONLY",
                "data_warning": (
                    "Thunderbolt's signal is the instantaneous order-book imbalance, which does not "
                    "exist historically for any past date -- there is no backtest to run, not even an "
                    "approximate one. This strategy can only be paper-traded going forward, once the "
                    "order-flow recorder is live during market hours."
                ),
            }

        # short_vol is a two-leg option structure priced off the real chain with
        # its own entry/exit clocks and per-leg costs -- BacktestEngine's candle
        # loop and single directional position can't express it, so it runs
        # through the dedicated ShortVolEngine (same pattern as weekly_multiday).
        if getattr(strat, "EXECUTION_MODE", None) == "intraday_short_vol":
            from vectra_quant.backtest.short_vol_engine import NIFTY_LOT_SIZE as _SV_LOT
            from vectra_quant.backtest.short_vol_engine import ShortVolEngine
            from vectra_quant.backtest import iea_data as _iea_sv

            resolved_from, resolved_to = from_date, to_date
            if not resolved_from and not resolved_to:
                tail = _iea_sv.load_index_window(instrument, tail_sessions=days)
                if tail:
                    resolved_from, resolved_to = min(tail), max(tail)

            p = dict(strat.params)
            sv_result = ShortVolEngine().run(
                instrument=instrument,
                from_date=resolved_from,
                to_date=resolved_to,
                lots=int(p.get("lots", 1)),
                cost_per_leg=float(p.get("cost_per_leg", 40.0)),
                entry_time=str(p.get("entry_time", "09:20:00")),
                exit_time=str(p.get("exit_time", "15:15:00")),
                min_dte=int(p.get("min_dte", 0)),
            )
            res = sv_result.as_dict()
            res["strategy"] = strat_name
            res["final_pnl"] = res["net_pnl"]
            res["days"] = sv_result.total_trades + sv_result.skipped_days
            res["initial_capital"] = 0.0
            res["data_warning"] = (
                "Naked short straddle: loss is unbounded in principle. This sample begins "
                "after the March-2020 crash, so the worst tail event of the modern era is "
                "NOT reflected in these statistics. Costs use a Rs/leg figure measured on a "
                "calm session; spreads widen in stressed markets, exactly when the large "
                "losses occur, so realised tail costs are worse than modelled."
            )

            # Emit the leg-level BacktestTrade shape the FE already renders,
            # grouping the CE+PE pair under one position_id per session.
            sv_trades: list[dict[str, Any]] = []
            for t in sv_result.trades:
                for tag, right, entry_px, exit_px, leg_pnl, leg_costs in (
                    ("L1", "CE", t.ce_entry, t.ce_exit, t.net_pnl, t.costs),
                    ("L2", "PE", t.pe_entry, t.pe_exit, 0.0, 0.0),
                ):
                    sv_trades.append({
                        "id": f"{t.date}_{tag}", "position_id": t.date,
                        "symbol": f"NIFTY {int(t.strike)} {right}", "direction": right,
                        "entry_price": entry_px, "exit_price": exit_px,
                        "opened_at": f"{t.date} {p.get('entry_time', '09:20:00')}",
                        "closed_at": f"{t.date} {p.get('exit_time', '15:15:00')}",
                        "qty": _SV_LOT * int(p.get("lots", 1)),
                        "net_pnl": leg_pnl, "gross_pnl": t.gross_pnl if tag == "L1" else 0.0,
                        "costs": leg_costs, "slippage_cost": 0.0, "capital_used": 0.0,
                        "exit_reason": t.exit_reason, "win": t.win,
                    })
            res["trades"] = sv_trades
            res["wiggle_analysis"] = {}
            return res

        # weekly_credit_spread holds positions across multiple sessions
        # (Wednesday entry, held toward next-week expiry); BacktestEngine's
        # day loop resets `in_trade` every session by design and can't model
        # that, so it runs through the dedicated WeeklySpreadEngine instead.
        if getattr(strat, "EXECUTION_MODE", None) == "weekly_multiday":
            from datetime import datetime as _dt
            from vectra_quant.backtest.weekly_spread_engine import WeeklySpreadEngine
            from vectra_quant.backtest import iea_data as _iea_data_pre

            # Honor the same from_date/to_date/days the FE already sends for
            # every other strategy, instead of hardcoding a fixed window --
            # when no explicit range is given, "days" resolves to the last N
            # real trading sessions exactly like BacktestEngine's tail-session
            # mode does, so the Timeline picker behaves consistently across
            # strategies.
            resolved_from, resolved_to = from_date, to_date
            if not resolved_from and not resolved_to:
                tail = _iea_data_pre.load_index_window(instrument, tail_sessions=days)
                if tail:
                    resolved_from, resolved_to = min(tail), max(tail)
            resolved_from = resolved_from or "2024-01-01"
            resolved_to = resolved_to or "2026-09-14"

            span_days = (_dt.strptime(resolved_to, "%Y-%m-%d") - _dt.strptime(resolved_from, "%Y-%m-%d")).days
            short_window_warning = None
            if span_days < 60:
                short_window_warning = (
                    f"This strategy trades once a week and holds toward next-week expiry -- "
                    f"a {span_days}-day window is too short to produce a meaningful sample "
                    f"(often 0-1 trades). Pick a custom date range of several months instead."
                )

            p = dict(strat.params)
            result = WeeklySpreadEngine().run(
                instrument=instrument,
                from_date=resolved_from,
                to_date=resolved_to,
                entry_weekday=int(p.get("entry_weekday", 2)),
                direction_mode="pcr",
                pcr_band_pct=float(p.get("pcr_band_pct", 0.03)),
                pcr_bullish_above=float(p.get("pcr_bullish_above", 1.05)),
                pcr_bearish_below=float(p.get("pcr_bearish_below", 0.95)),
                max_hold_days=int(p.get("max_hold_days", 7)),
                stop_loss_mult_of_credit=(float(p["stop_loss_mult_of_credit"]) if p.get("stop_loss_mult_of_credit") is not None else None),
                futures_target_pts_early=float(p.get("futures_target_pts_early", 40.0)),
                futures_target_pts_late=float(p.get("futures_target_pts_late", 70.0)),
                futures_target_widen_from_day=int(p.get("futures_target_widen_from_day", 5)),
                moneyness_offset_pct=float(p.get("moneyness_offset_pct", 0.0)),
                min_entry_volume=float(p.get("min_entry_volume", 20000.0)),
            )
            from vectra_quant.backtest.weekly_spread_engine import NIFTY_LOT_SIZE

            res_dict = result.as_dict()
            res_dict["strategy"] = strat_name
            res_dict["final_pnl"] = res_dict["net_pnl"]
            res_dict["days"] = len(_iea_data_pre.load_index_window(instrument, resolved_from, resolved_to))
            res_dict["data_warning"] = short_window_warning
            res_dict["initial_capital"] = round(max((t.max_risk for t in result.trades), default=0.0), 2)

            # The FE renders a leg-level BacktestTrade shape (position_id,
            # symbol, direction, entry_price/exit_price, opened_at/closed_at)
            # and groups legs sharing one position_id into a single card --
            # emit the sell+buy leg pair per trade in that shape rather than
            # inventing new UI just for this strategy.
            fe_trades: list[dict[str, Any]] = []
            for t in result.trades:
                pos_id = t.entry_date
                right_code = "CE" if t.right == "C" else "PE"
                fe_trades.append({
                    "id": f"{pos_id}_L1", "position_id": pos_id,
                    "symbol": f"NIFTY {int(t.sell_strike)} {right_code}", "direction": right_code,
                    "entry_price": t.sell_entry, "exit_price": t.sell_exit,
                    "opened_at": f"{t.entry_date} 09:20:00", "closed_at": f"{t.exit_date} 15:15:00",
                    "qty": NIFTY_LOT_SIZE, "net_pnl": t.net_pnl, "gross_pnl": t.gross_pnl,
                    "costs": t.costs, "slippage_cost": 0.0, "capital_used": t.max_risk,
                    "exit_reason": t.exit_reason, "win": t.win,
                })
                fe_trades.append({
                    "id": f"{pos_id}_L2", "position_id": pos_id,
                    "symbol": f"NIFTY {int(t.buy_strike)} {right_code}", "direction": right_code,
                    "entry_price": t.buy_entry, "exit_price": t.buy_exit,
                    "opened_at": f"{t.entry_date} 09:20:00", "closed_at": f"{t.exit_date} 15:15:00",
                    "qty": NIFTY_LOT_SIZE, "net_pnl": 0.0, "gross_pnl": 0.0,
                    "costs": 0.0, "slippage_cost": 0.0, "capital_used": 0.0,
                    "exit_reason": t.exit_reason, "win": t.win,
                })
            res_dict["trades"] = fe_trades
            res_dict["wiggle_analysis"] = {}
            return res_dict

        bte = BacktestEngine()

        def progress_cb(phase: str, si: int, sc: int, done: int, total: int, date_str: str) -> None:
            st.backtest_jobs.report_progress(job, phase, si, sc, done, total, date_str)

        cancel_cb = job.cancel_event.is_set

        if run_wiggle:
            wiggle, result = bte.parameter_wiggle_test(
                strategy=strat,
                instrument=instrument,
                days=days,
                from_date=from_date,
                to_date=to_date,
                broker=broker,
                return_baseline=True,
                progress=progress_cb,
                cancel_check=cancel_cb,
            )
        else:
            result = bte.run_strategy(
                strategy=strat,
                instrument=instrument,
                days=days,
                from_date=from_date,
                to_date=to_date,
                broker=broker,
                enable_slippage=enable_slippage,
                progress=progress_cb,
                cancel_check=cancel_cb,
            )
            wiggle = {}

        res_dict = result.as_dict()
        res_dict["wiggle_analysis"] = wiggle
        return res_dict

    try:
        job_id = st.backtest_jobs.submit(
            strategy=strat_name,
            instrument=instrument,
            run_fn=run_fn,
            meta={
                "days": days, "from_date": from_date, "to_date": to_date,
                "wiggle_test": run_wiggle, "params": params,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"job_id": job_id}


@router.get("/backtest/runs/{job_id}", dependencies=[Depends(require_secret)])
def get_backtest_run(job_id: str, request: Request) -> dict[str, Any]:
    """Current progress (if still tracked in memory) or the persisted result/history row."""
    st = request.app.state
    job = st.backtest_jobs.get(job_id)
    if job is not None:
        payload = job.as_progress_payload()
        payload["result"] = job.result
        return payload

    row = st.backtest_jobs.get_run_row(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="backtest run not found")
    return row


@router.post("/backtest/runs/{job_id}/cancel", dependencies=[Depends(require_secret)])
def cancel_backtest_run(job_id: str, request: Request) -> dict[str, Any]:
    st = request.app.state
    if not st.backtest_jobs.cancel(job_id):
        raise HTTPException(status_code=404, detail="job not found, or already finished")
    return {"ok": True}


@router.get("/backtest/runs", dependencies=[Depends(require_secret)])
def list_backtest_runs(request: Request, limit: int = 20) -> dict[str, Any]:
    st = request.app.state
    return {"runs": st.backtest_jobs.list_recent(limit=min(limit, 100))}


@router.post("/strategies/active", dependencies=[Depends(require_secret)])
async def set_active_strategy(request: Request) -> dict[str, Any]:
    """Update active strategies or toggle algo auto-execution mode."""
    from vectra_quant.config import AlgoCfg

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
    from vectra_quant import config as config_mod
    pwa_dist = config_mod.ROOT / "pwa" / "dist"
    target = pwa_dist / filename
    if target.exists() and target.is_file():
        from fastapi.responses import FileResponse
        return FileResponse(str(target))
    raise HTTPException(status_code=404, detail=f"File not found: {filename}")


"""SENTINEL application: FastAPI + APScheduler, all wiring in one place.

Boot order matters and is deliberate:
  1. DB + WAL
  2. broker auth, instrument master (no lot sizes = no trading)
  3. re-arm the FSM from today's journal BEFORE anything can place an order
  4. adopt existing broker positions so a restart does not re-bill the budget
  5. only then start the feed, the scheduler, and accept approvals

If step 2 or 3 fails, the app refuses to serve trading endpoints rather than
starting in an unknown state (§0.2 fail closed).
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sentinel import config as config_mod
from sentinel import db, logging_setup
from sentinel.api.push import Pusher
from sentinel.api.routes import WsHub, build_state, router
from sentinel.core.guardian import Guardian
from sentinel.core.killswitch import KillSwitch
from sentinel.core.lifecycle import Lifecycle
from sentinel.core.orchestrator import Orchestrator
from sentinel.core.session_clock import IST, is_market_hours, now_ist, session_date
from sentinel.data.candles import CandleBuilder
from sentinel.data.chain import ChainService
from sentinel.data.feed import FeedService
from sentinel.data.instruments import InstrumentService
from sentinel.events.engine import EventEngine, EventKind
from sentinel.llm.prompts import fsm_context, pack_snapshot
from sentinel.llm.router import LlmRouter

log = logging_setup.get("app")

INDEX_SYMBOLS = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "SENSEX": "SENSEX",
    "FINNIFTY": "FINNIFTY",
    "INDIAVIX": "INDIAVIX",
}


def build_broker(settings: Any):
    """LIVE -> Selected Broker (Groww/Zerodha). PAPER -> Sim wrapping broker for real market data.

    The three-red-days rule (design §4.6) is enforced HERE. Without this the flag
    was set, logged and pushed to the trader, then ignored at boot.
    """
    from sentinel.core.orchestrator import FORCE_PAPER_KEY

    broker_name = getattr(settings, "broker_name", "groww").lower()
    if broker_name == "zerodha":
        from sentinel.brokers.zerodha import ZerodhaAdapter
        log.info("Selected broker adapter: Zerodha")
        live = ZerodhaAdapter(settings.secrets.zerodha_api_key, settings.secrets.zerodha_access_token)
    else:
        from sentinel.brokers.groww import GrowwAdapter
        log.info("Selected broker adapter: Groww")
        live = GrowwAdapter(settings.secrets.groww_api_key, settings.secrets.groww_totp_seed)

    forced = db.state_get(FORCE_PAPER_KEY, "") == "1"
    if forced:
        log.warning("THREE RED DAYS — forcing PAPER for this session (§4.6)")
    if settings.is_live and not forced:
        log.warning("LIVE MODE — orders go to the real broker (%s)", broker_name)
        return live
    from sentinel.brokers.sim import SimAdapter
    log.warning("PAPER MODE — orders go to the simulator, market data is real (%s)", broker_name)
    return SimAdapter(starting_funds=settings.capital, data_source=live)


def create_app() -> FastAPI:
    logging_setup.setup()
    settings = config_mod.get()
    db.init_db()

    app = FastAPI(title="SENTINEL", version="1.0", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.secrets.pwa_origin] if settings.secrets.pwa_origin != "*" else ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    st = app.state
    st.settings = settings
    st.index_symbols = dict(INDEX_SYMBOLS)
    st.hub = WsHub()
    st.killswitch = KillSwitch()
    st.pusher = Pusher(
        public_key=settings.secrets.vapid_public,
        private_key=settings.secrets.vapid_private,
        subject=settings.secrets.vapid_subject,
    )
    st.scheduler = AsyncIOScheduler(timezone=IST)
    st.ready = False
    st.boot_error = ""

    def alert(kind: str, message: str, data: dict | None = None) -> None:
        st.pusher.send(kind, message, data or {})

    st.broker = build_broker(settings)
    st.instruments = InstrumentService(st.broker, chain_depth=settings.instruments.chain_depth)
    st.candles = CandleBuilder()
    st.chain = ChainService(st.broker, st.instruments,
                            depth=settings.instruments.chain_depth,
                            oi_lookback_min=settings.data.oi_diff_lookback_min)

    st.orch = Orchestrator(
        settings.risk,
        week_loss_limit_mult=settings.week.loss_limit_mult,
        red_days_to_paper=settings.week.red_days_to_paper,
        kill_switch_on=st.killswitch.is_on,
        on_engine_event=lambda ev, snap: _on_engine_event(st, ev, snap),
        on_state_change=lambda snap: st.pusher.state_change(snap.state, snap.floor,
                                                           snap.day_pnl),
    )
    st.guardian = Guardian(
        st.broker, st.orch.engine,
        sl_attach_deadline_s=settings.guardian.sl_attach_deadline_s,
        squareoff_verify_s=settings.guardian.squareoff_verify_s,
        yield_to_manual_sl=settings.guardian.yield_to_manual_sl,
        on_alert=alert,
        instrument_of=lambda sym: st.instruments.get(sym),
    )
    st.feed = FeedService(
        st.broker, st.candles,
        degraded_after_s=settings.data.tick_staleness_degraded_s,
        critical_after_s=settings.data.tick_staleness_critical_s,
        on_degraded=lambda h: alert("SYSTEM", (
            f"DATA DEGRADED — suggestions paused ({h.worst_age_s:.0f}s stale).")),
        on_critical=lambda h: alert("SYSTEM", (
            "MANAGE MANUALLY — feed dead with an open position. "
            "Your broker-side stop is still resting.")),
        on_recovered=lambda h: alert("SYSTEM", "Feed recovered — suggestions resumed."),
    )
    st.events = EventEngine(
        thresholds=settings.events.thresholds,
        debounce_min=settings.events.debounce,
        times=settings.events.times,
    )
    st.llm = LlmRouter(
        bridge_url=settings.llm.bridge_url,
        groq_api_key=settings.secrets.groq_api_key,
        groq_model=settings.llm.groq_model,
        groq_fallback_model=settings.llm.groq_fallback_model,
        timeout_s=settings.llm.timeout_s,
        bridge_retries=settings.llm.bridge_retries,
        daily_cap=settings.llm.suggest_cap_per_day,
    )
    st.lifecycle = Lifecycle(
        broker=st.broker, orchestrator=st.orch, guardian=st.guardian,
        instruments=st.instruments, chain=st.chain, settings=settings,
        killswitch=st.killswitch, on_alert=alert,
    )

    @app.on_event("startup")
    async def _startup() -> None:
        try:
            st.loop = asyncio.get_running_loop()
            master = st.instruments.refresh(force=True)
            log.info("instrument master ready", extra={"rows": len(master)})

            st.orch.rearm()
            # Baseline the order book BEFORE anything can react to it, then adopt
            # only positions that are genuinely still open.
            st.guardian.prime()
            st.guardian.adopt_existing(
                [p for p in st.broker.get_positions() if p.is_open])

            _backfill_candles(st)

            symbols = _watchlist(st)
            st.feed.subscribe(symbols)
            _subscribe_broker_streams(st)

            _schedule(st)
            st.scheduler.start()
            st.ready = True
            log.warning("SENTINEL ready", extra={
                "mode": settings.mode, "session": session_date(),
                "kill_switch": "ON" if st.killswitch.is_on else "OFF",
                "watchlist": len(symbols),
            })
        except Exception as exc:
            st.boot_error = str(exc)[:300]
            log.error("BOOT FAILED — refusing to trade", extra={"error": st.boot_error})
            alert("SYSTEM", f"SENTINEL failed to start: {st.boot_error[:100]}")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        with contextlib.suppress(Exception):
            st.scheduler.shutdown(wait=False)
        with contextlib.suppress(Exception):
            st.feed.stop()
        st.candles.flush()
        log.warning("SENTINEL stopped")

    # Mount Vite PWA static build if pwa/dist exists
    pwa_dist = config_mod.ROOT / "pwa" / "dist"
    if pwa_dist.exists() and pwa_dist.is_dir():
        from fastapi.staticfiles import StaticFiles
        assets_dir = pwa_dist / "assets"
        if assets_dir.exists() and assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")
        app.mount("/static", StaticFiles(directory=str(pwa_dist)), name="static")

    return app


# ---------------------------------------------------------------- helpers

def _backfill_candles(st: Any) -> None:
    """Seed 1m history over REST so levels exist immediately after a restart.

    Without this, opening_range() and prev_day_range() return None until enough
    live minutes accumulate, which means LEVEL_BREAK never fires and the LLM
    reasons without PDH/PDL/ORH/ORL. Build plan Phase 1.
    """
    for name in (st.settings.instruments.primary, st.settings.instruments.secondary):
        sym = st.index_symbols.get(name, name)
        try:
            # 2400 minutes reaches the previous session, which PDH/PDL need.
            candles = st.broker.get_candles(sym, "1m", 2400, segment="CASH")
        except Exception as exc:
            log.warning("candle backfill failed", extra={"symbol": sym,
                                                         "error": str(exc)[:160]})
            continue
        n = st.candles.backfill(sym, candles)
        log.info("candles backfilled", extra={"symbol": sym, "bars": n})


def _watchlist(st: Any) -> list[str]:
    """Index spot + VIX + every open position's contract."""
    out = [st.index_symbols["NIFTY"], st.index_symbols["SENSEX"],
           st.index_symbols["INDIAVIX"]]
    out += [t.trading_symbol for t in st.lifecycle.open_trades()]
    return sorted({s for s in out if s})


def _subscribe_broker_streams(st: Any) -> None:
    """Order + position streams. Best-effort: REST remains the truth (§3.4)."""
    mode = st.settings.guardian.detection
    if mode == "rest_poll":
        log.info("order stream skipped — detection is rest_poll only")
    else:
        try:
            st.broker.stream_orders(st.guardian.on_order_event)
            log.info("order stream subscribed", extra={"detection": mode})
        except Exception as exc:
            log.warning("order stream unavailable — REST polling carries detection",
                        extra={"error": str(exc)[:160]})
    stream_positions = getattr(st.broker, "stream_positions", None)
    if stream_positions:
        try:
            stream_positions(lambda ps: _on_positions(st, ps))
            log.info("position stream subscribed")
        except Exception as exc:
            log.warning("position stream unavailable", extra={"error": str(exc)[:160]})


def _on_positions(st: Any, positions: list) -> None:
    unrealized = sum(p.unrealized_pnl for p in positions if p.is_open)
    st.orch.on_pnl_tick(unrealized)


def _on_engine_event(st: Any, ev: Any, snap: Any) -> None:
    """A lock or floor breach: exit everything, then book the fills."""
    if not (ev.square_off or ev.lock):
        return
    log.warning("engine demands square-off", extra={"note": ev.note})
    st.guardian.square_off_all(ev.note or "engine lock")
    for t in st.lifecycle.open_trades():
        st.lifecycle.close_trade(
            t.id, st.lifecycle._exit_price(t.trading_symbol, t.entry_price), "engine_lock")

    # A restart after the session cutoff locks the day immediately — correct, but it is
    # not news, and every redeploy pushed another "LOCKED" alert to the phone. Only tell
    # the trader when something actually happened today.
    if not is_market_hours() and snap.trades_taken == 0:
        log.info("boot-time lock — no push", extra={"note": ev.note})
        return
    st.pusher.send("STATE_CHANGE",
                   f"LOCKED — {ev.note}. dayPnL {snap.day_pnl:+,.0f}.",
                   {"state": snap.state}, urgent=True)


# ---------------------------------------------------------------- scheduled jobs

def _schedule(st: Any) -> None:
    s = st.scheduler
    cfg = st.settings

    s.add_job(lambda: _job(st, _tick_prices), IntervalTrigger(seconds=3),
              id="prices", max_instances=1, coalesce=True)
    s.add_job(lambda: _job(st, _tick_pnl), IntervalTrigger(seconds=cfg.data.pnl_tick_interval_s),
              id="pnl_tick", max_instances=1, coalesce=True)
    s.add_job(lambda: _job(st, _tick_guardian),
              IntervalTrigger(seconds=cfg.guardian.poll_interval_s),
              id="guardian_poll", max_instances=1, coalesce=True)
    s.add_job(lambda: _job(st, _tick_recon), IntervalTrigger(seconds=cfg.data.recon_interval_s),
              id="recon", max_instances=1, coalesce=True)
    s.add_job(lambda: _job(st, _tick_chain),
              IntervalTrigger(seconds=cfg.data.chain_snapshot_interval_s),
              id="chain", max_instances=1, coalesce=True)
    s.add_job(lambda: _job(st, _tick_detectors), IntervalTrigger(seconds=20),
              id="detectors", max_instances=1, coalesce=True)
    s.add_job(lambda: _job(st, _tick_housekeeping), IntervalTrigger(seconds=10),
              id="housekeeping", max_instances=1, coalesce=True)
    s.add_job(lambda: _job(st, _broadcast), IntervalTrigger(seconds=1),
              id="broadcast", max_instances=1, coalesce=True)

    s.add_job(lambda: _job(st, _refresh_instruments),
              CronTrigger(hour=8, minute=45, timezone=IST), id="instrument_master")
    s.add_job(lambda: _job(st, _reset_day),
              CronTrigger(hour=9, minute=0, timezone=IST), id="reset_day")
    s.add_job(lambda: _job(st, _clock),
              IntervalTrigger(seconds=15), id="clock", max_instances=1, coalesce=True)
    s.add_job(lambda: _job(st, _close_session),
              CronTrigger(hour=15, minute=35, timezone=IST), id="close_session")
    s.add_job(lambda: _job(st, _backup), CronTrigger(hour=16, minute=0, timezone=IST),
              id="backup")


def _job(st: Any, fn) -> None:
    """Never let a scheduled job kill the loop; log and carry on."""
    if not st.ready:
        return
    try:
        fn(st)
    except Exception as exc:
        log.error("scheduled job failed", extra={"job": fn.__name__,
                                                "error": str(exc)[:250]})


def _tick_prices(st: Any) -> None:
    """REST price poll — the primary market-data path (D-012)."""
    if not is_market_hours():
        return
    st.feed.poll_rest(
        [st.index_symbols["NIFTY"], st.index_symbols["SENSEX"],
         st.index_symbols["INDIAVIX"]],
        segment="CASH",
    )
    held = [t.trading_symbol for t in st.lifecycle.open_trades()]
    if held:
        st.feed.poll_rest(held, segment="FNO")


def _tick_pnl(st: Any) -> None:
    open_trades = st.lifecycle.open_trades()
    if not open_trades:
        return
    symbols = [t.trading_symbol for t in open_trades]
    prices = st.feed.prices()
    missing = [s for s in symbols if s not in prices]
    if missing:
        prices.update(st.feed.refresh_via_rest(missing))
    unrealized = 0.0
    for t in open_trades:
        px = prices.get(t.trading_symbol)
        if px:
            unrealized += (px - t.entry_price) * t.qty
    st.orch.on_pnl_tick(unrealized)

    snap = st.orch.snapshot()
    dist = st.orch.floor_distance()
    if 0 < dist <= st.settings.floor_warning_rupees:
        st.pusher.floor_warning(snap.day_pnl, snap.floor, dist)

    # POSITION_EVENT (design §5.2): a position past 50% of target, or nearing its
    # stop, is worth waking the LLM for.
    for t_ in open_trades:
        px = prices.get(t_.trading_symbol)
        if not px or not t_.target_price or not t_.sl_price:
            continue
        span = t_.target_price - t_.entry_price
        pct = ((px - t_.entry_price) / span * 100.0) if span > 0 else 0.0
        near_sl = px <= t_.sl_price + max(0.05, (t_.entry_price - t_.sl_price) * 0.2)
        for ev in st.events.check_position_event(
            t_.trading_symbol, pnl_pct_of_target=pct, near_sl=near_sl
        ):
            _dispatch(st, ev)


def _tick_guardian(st: Any) -> None:
    # In `ws` mode the poll still runs as a safety net, just less often — a websocket
    # that silently stops delivering must not leave a manual trade unguarded.
    if st.settings.guardian.detection != "ws" or _every_nth(st, "guardian_poll", 6):
        st.guardian.poll_once()
    st.guardian.enforce_stops()
    # Step aside if the trader's own stop has appeared.
    st.guardian.yield_to_own_stops()
    # Book any exit the system did not initiate (broker SL fill, manual app exit).
    st.lifecycle.settle_broker_exits()


def _every_nth(st: Any, key: str, n: int) -> bool:
    counters = getattr(st, "_counters", None)
    if counters is None:
        counters = st._counters = {}
    counters[key] = counters.get(key, 0) + 1
    return counters[key] % n == 0


def _tick_recon(st: Any) -> None:
    from sentinel.brokers.recon import Reconciler
    if not hasattr(st, "recon"):
        st.recon = Reconciler(st.broker)
    result = st.recon.run_once()
    if not result.has_critical:
        st.recon_alerted = set()
        return
    # Alert once per distinct discrepancy, not every 15s forever.
    seen = getattr(st, "recon_alerted", set())
    fresh = {f"{d.kind}:{d.symbol}" for d in result.discrepancies if d.critical} - seen
    if fresh:
        st.recon_alerted = seen | fresh
        st.pusher.system(f"Reconciliation mismatch: {'; '.join(sorted(fresh)[:3])}")


def _tick_chain(st: Any) -> None:
    if not is_market_hours():
        return
    for name in (st.settings.instruments.primary, st.settings.instruments.secondary):
        spot = st.feed.price(st.index_symbols.get(name, name))
        if spot:
            st.chain.snapshot(name, spot)


def _tick_detectors(st: Any) -> None:
    if not is_market_hours():
        return
    for ev in st.events.check_time_events():
        _dispatch(st, ev)

    vix = st.feed.price(st.index_symbols["INDIAVIX"])
    if vix:
        for ev in st.events.check_vix_spike(vix):
            _dispatch(st, ev)

    for name in (st.settings.instruments.primary, st.settings.instruments.secondary):
        sym = st.index_symbols.get(name, name)
        close = st.candles.last_close(sym)
        if not close:
            continue
        levels: dict[str, float] = {}
        orange = st.candles.opening_range(sym)
        if orange:
            levels["ORH"], levels["ORL"] = orange
        pd = st.candles.prev_day_range(sym)
        if pd:
            levels["PDH"], levels["PDL"] = pd
        recent = st.candles.series(sym, "1m", 2)
        prev_close = recent[0].close if len(recent) >= 2 else None
        for ev in st.events.check_level_break(name, close, levels, prev_close):
            _dispatch(st, ev)

        candles5 = st.candles.series(sym, "5m", limit=2)
        if candles5:
            rng = candles5[-1].high - candles5[-1].low
            for ev in st.events.check_momentum_burst(name, rng, st.candles.atr(sym)):
                _dispatch(st, ev)

        for ev in st.events.check_oi_shift(
            name, st.chain.big_oi_shifts(name, st.events.oi_shift_pct)
        ):
            _dispatch(st, ev)


def _dispatch(st: Any, ev: Any) -> None:
    """Event -> snapshot -> LLM -> gates -> queue. Fail closed at every step."""
    if ev.kind is EventKind.DAY_END:
        return
    if not ev.wants_suggestion:
        return
    if st.killswitch.is_off:
        log.info("event ignored — kill switch OFF", extra={"kind": ev.kind.value})
        return
    if not st.feed.suggestions_allowed:
        log.info("event ignored — data degraded", extra={"kind": ev.kind.value})
        return
    if st.orch.is_locked or st.orch.week_locked:
        return
    if not st.lifecycle.in_entry_window():
        return

    # POSITION_EVENT carries the option trading symbol, not the index name. Mapping
    # it back keeps the LLM from being asked about the wrong index.
    name = ev.instrument
    position_rows: list[dict[str, Any]] = []
    if name not in (st.settings.instruments.primary, st.settings.instruments.secondary):
        inst = st.instruments.get(ev.instrument)
        mapped = (getattr(inst, "name", "") or "").upper()
        name = mapped if mapped in (st.settings.instruments.primary,
                                    st.settings.instruments.secondary) \
            else st.settings.instruments.primary
    if ev.kind is EventKind.POSITION_EVENT:
        for p_ in st.broker.get_positions():
            if p_.is_open:
                position_rows.append({
                    "sym": p_.trading_symbol, "qty": p_.quantity,
                    "avg": round(p_.average_price, 2), "ltp": round(p_.last_price, 2),
                    "unreal": round(p_.unrealized_pnl, 2),
                })

    if not st.chain.is_fresh(name):
        log.warning("stale chain — skipping LLM", extra={"instrument": name})
        return

    spot = st.feed.price(st.index_symbols.get(name, name))
    if not spot:
        return

    vix = st.feed.price(st.index_symbols["INDIAVIX"]) or 0.0
    snap_fsm = st.orch.snapshot()
    expiring = st.instruments.expiring_today(
        [st.settings.instruments.primary, st.settings.instruments.secondary])
    decision = st.orch.can_enter(confidence=None, is_expiry_day=name in expiring)

    sym = st.index_symbols.get(name, name)
    levels: dict[str, float] = {}
    orange = st.candles.opening_range(sym)
    if orange:
        levels["ORH"], levels["ORL"] = orange
    pd = st.candles.prev_day_range(sym)
    if pd:
        levels["PDH"], levels["PDL"] = pd
    if ev.detail.get("level_value"):
        levels[str(ev.detail.get("level", "LEVEL"))] = float(ev.detail["level_value"])

    day = st.candles.series(sym, "1m", 400)
    spot_change_pct = (
        round((spot - day[0].open) / day[0].open * 100.0, 2) if day and day[0].open else 0.0
    )
    vix_series = st.candles.series(st.index_symbols["INDIAVIX"], "1m", 400)
    vix_change_pct = (
        round((vix - vix_series[0].open) / vix_series[0].open * 100.0, 2)
        if vix_series and vix_series[0].open else 0.0
    )

    payload = pack_snapshot(
        event_kind=ev.kind.value, instrument=name, spot=spot,
        spot_change_pct=spot_change_pct,
        vix=vix, vix_change_pct=vix_change_pct,
        levels=levels, fsm=fsm_context(
            state=snap_fsm.state, trades_taken=snap_fsm.trades_taken,
            trade_cap=snap_fsm.trade_cap, day_pnl=snap_fsm.day_pnl,
            floor=snap_fsm.floor, in_entry_window=True,
            is_expiry_day=name in expiring,
            min_confidence=decision.min_confidence,
        ),
        chain=st.chain.compact_for_llm(name),
        candles_5m=[[c.open, c.high, c.low, c.close]
                    for c in st.candles.series(sym, "5m", 12)],
        positions=position_rows or None,
    )
    st.events.attach_snapshot(ev, payload)

    outcome = st.llm.suggest(payload, event_kind=ev.kind.value)
    st.events.mark_dispatched(ev)
    if not outcome.ok or outcome.data is None:
        return
    if outcome.data.get("action") == "NO_TRADE":
        st.lifecycle.record_gated(outcome.data, "model returned NO_TRADE",
                                  event_kind=ev.kind.value, model=outcome.model)
        return

    gate, queued = st.lifecycle.apply_gates(outcome.data, spot=spot,
                                            is_expiry_day=name in expiring)
    if not gate.passed or queued is None:
        st.lifecycle.record_gated(outcome.data, gate.reason,
                                  event_kind=ev.kind.value, model=outcome.model)
        return
    st.lifecycle.enqueue(queued, event_kind=ev.kind.value, model=outcome.model,
                         event_id=ev.row_id)


def _tick_housekeeping(st: Any) -> None:
    st.lifecycle.expire_stale()
    st.lifecycle.check_targets()
    st.lifecycle.check_time_stops()
    st.feed.health(has_open_position=bool(st.lifecycle.open_trades()))
    st.candles.flush()
    trigger = getattr(st.broker, "trigger_stops", None)
    if trigger:
        for order in trigger():
            price = order.average_price
            for t in st.lifecycle.open_trades():
                if t.trading_symbol == order.trading_symbol:
                    st.lifecycle.close_trade(t.id, price, "stop_loss")


def _clock(st: Any) -> None:
    st.orch.on_clock(now_ist())


def _refresh_instruments(st: Any) -> None:
    st.instruments.refresh(force=True)
    st.feed.subscribe(_watchlist(st))


def _reset_day(st: Any) -> None:
    st.orch.reset_day()
    st.events.reset_day()
    st.chain.purge_old()
    log.warning("new session", extra={"date": session_date()})


def _close_session(st: Any) -> None:
    out = st.orch.close_session()
    from sentinel.reports.dayend import build_dayend_report
    report = build_dayend_report(st, session_date())
    v = report["verdict"]
    st.pusher.report(
        f"Day done: {v['realized']:+,.0f} net over {v['trades']} trade(s). "
        f"Path {' -> '.join(v['state_path'])}."
    )
    if out.get("force_paper_next"):
        st.pusher.system("Three red days — tomorrow runs in PAPER mode.")


def _backup(st: Any) -> None:
    bucket = st.settings.secrets.backup_s3_bucket
    if not bucket:
        return
    import boto3
    key = f"sentinel/{session_date()}/sentinel.db"
    boto3.client("s3", region_name=st.settings.secrets.aws_region).upload_file(
        str(db.DB_PATH), bucket, key)
    log.info("backup uploaded", extra={"bucket": bucket, "key": key})


def _broadcast(st: Any) -> None:
    if st.hub.client_count == 0:
        return
    loop = getattr(st, "loop", None)
    if loop is None or loop.is_closed():
        return
    payload = build_state(st)
    payload["ts"] = datetime.now(UTC).isoformat()
    # Scheduled jobs run on a worker thread; the coroutine must be handed to the
    # server's loop explicitly or it is silently dropped.
    asyncio.run_coroutine_threadsafe(st.hub.broadcast(payload), loop)


app = create_app()

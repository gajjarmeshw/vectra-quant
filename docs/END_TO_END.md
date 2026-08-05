# SENTINEL — End-to-End System Documentation

**Version:** v1.0 · **Date:** 2026-08-05 · **Status:** deployed, live-configured, has never placed a real order.

Every statement here was read out of the code at this commit. Where behaviour depends on an
unverified external fact (Groww's order stream, iOS push delivery), it is marked
**UNVERIFIED** rather than asserted.

---

## 0. What this system is

An AI-assisted, human-in-the-loop day-trading console for **Indian index options, buying only**
(NIFTY on NSE, SENSEX on BSE), running on ₹15,000 of capital.

**The invariant:** every rupee-protecting rule is deterministic Python. The LLM suggests; it
never sizes, never sets a limit, never executes. The trader taps; the risk engine can still
refuse. Automated actions are limited to **risk-reducing exits**.

Two enforcement modes coexist:

| Mode | Applies to | Power |
|---|---|---|
| **Gatekeeper** | orders approved in the PWA | absolute — the order cannot reach the broker without passing every check |
| **Guardian** | orders the trader places in the Groww app | cannot block entry; detects it, counts it, force-attaches a stop, enforces floors, logs violations |

---

## 1. Repository map

```
sentinel/
├── backend/sentinel/
│   ├── risk_engine.py          PROVIDED VERBATIM — the FSM. Never modified.
│   ├── app.py                  wiring, scheduler, all job bodies
│   ├── config.py               params.yaml + .env loader, frozen dataclasses
│   ├── db.py                   SQLAlchemy models, SQLite WAL, 10 tables
│   ├── logging_setup.py        JSON logs, secret redaction
│   ├── api/
│   │   ├── routes.py           15 REST endpoints + /live WebSocket
│   │   └── push.py             Web Push (VAPID), 6 push kinds, repeat throttle
│   ├── brokers/
│   │   ├── base.py             BrokerAdapter port (design §3.1)
│   │   ├── groww.py            live adapter: auth, rate limit, breaker, feed thread
│   │   ├── sim.py              SimAdapter — same code path, simulated fills
│   │   ├── costs.py            Indian F&O cost model
│   │   └── recon.py            three-source-truth reconciler
│   ├── core/
│   │   ├── orchestrator.py     sole owner of RiskEngine; week governor; re-arm
│   │   ├── lifecycle.py        gates → approve → entry+SL → exits → close
│   │   ├── guardian.py         manual-trade detection and protection
│   │   ├── sizing.py           lot sizing, cost cap, strike selection
│   │   ├── killswitch.py       master switch, typed re-enable
│   │   └── session_clock.py    IST clock, session date, market hours
│   ├── data/
│   │   ├── instruments.py      daily instrument master, strike resolution
│   │   ├── feed.py             price cache, staleness gates, REST fallback
│   │   ├── candles.py          1m/5m candles, ATR, OR, PDH/PDL, PDC
│   │   └── chain.py            option-chain snapshots, OI diffing
│   ├── events/engine.py        9 event detectors, per-key debounce
│   ├── llm/
│   │   ├── schema.py           strict JSON schema, extraction, validation, RR
│   │   ├── prompts.py          system prompt + few-shot
│   │   └── router.py           bridge → Groq → fail closed, one repair retry
│   └── reports/dayend.py       day-end verdict, journal, calibration
├── backend/tests/              145 tests
├── config/
│   ├── params.yaml             the ONLY tunables file (PWA-editable)
│   └── params.default.yaml     factory defaults for "Reset all"
├── pwa/                        React 18 + Vite + Tailwind, installable PWA
├── caddy/Caddyfile             TLS, static PWA, API proxy
├── scripts/                    provision, deploy, drills, WS probe
└── docs/                       this file, DECISIONS.md, RUNBOOK.md, design docs
```

---

## 2. Runtime topology

```
iPhone (installed PWA)
   │  HTTPS + WSS, header X-Sentinel-Key
   ▼
Caddy (:443, auto-TLS via sslip.io)  ──── serves /  → pwa/dist
   │                                 └──── proxies /state,/live,… → backend:8000
   ▼
FastAPI + APScheduler (uvicorn, container uid 10001)
   │            │                 │
   │            │                 └── SQLite WAL at /app/data/sentinel.db (bind mount)
   │            └── GrowwAdapter ──── REST (api.groww.in) + NATS WS
   └── LLM: Claude bridge (optional, Tailscale) → Groq
```

Host: AWS EC2 **t4g.small**, ap-south-1, Elastic IP **15.252.81.14**, site
`https://15-252-81-14.sslip.io`. `docker-compose` with two services (`backend`, `caddy`);
`./config` and `./data` are bind-mounted, so config edits survive image rebuilds.

---

## 3. Configuration

### 3.1 `config/params.yaml` — every tunable

Loaded once at boot into frozen dataclasses. Editable from the PWA (System → Open config)
or by editing the file. **Risk-engine values only take effect on restart**; windows, sizing,
guardian, event and data settings are read live from `st.settings`.

| Key | Value | Effect |
|---|---|---|
| `capital` | 15000 | sizing affordability check |
| `daily.target` | 2500 | T — drives EARNED/PROTECT/TRAIL thresholds |
| `daily.loss_limit` | 1500 | lock at `dayPnL ≤ −1500`; also week limit basis |
| `daily.risk_per_trade` | 1200 | r — max risk per entry (halved in PROTECT/TRAIL) |
| `trades.base` | 3 | base trade cap |
| `trades.bonus_at_pct_target` | 0.45 | realized ≥ 45% of T unlocks the 4th trade |
| `trades.cooldown_min` | 0 | no cooldown (user decision) |
| `trades.consecutive_loss_stop` | 2 | 2 losses in a row → LOCKED |
| `fsm.t1_keep_pct` | 0.70 | EARNED floor = 0.70 × dayPnL ("keep 70") |
| `fsm.protect.at` / `.floor_pct_target` / `.risk_scale` / `.min_conf` | 1.0 / 0.5 / 0.5 / 80 | PROTECT entry, floor, risk halving, confidence gate |
| `fsm.trail.at` / `.giveback` | 1.3 / 0.25 | TRAIL entry at 1.3×T, floor = 0.75 × peak |
| `windows.entry_open` / `entry_close` | 09:20 / 15:00 | entry window (IST) |
| `windows.squareoff` | 15:10 | hard session cutoff → LOCKED |
| `windows.expiry_entry_close` | 14:30 | earlier cutoff on expiry day |
| `windows.expiry_risk_scale` | **1.0** | expiry changes NOTHING but the cutoff (D-009) |
| `sizing.max_position_cost` | 14000 | absolute ₹ cap; 1 lot above this = **no trade** |
| `sizing.preferred_cost_min/max` | 7000 / 12000 | strike-selection preference band |
| `sizing.min_lots` | 1 | 1 lot always offered, flagged `over_risk` if it exceeds r |
| `sizing.max_lots` | SENSEX 4, NIFTY 1 | per-instrument lot ceiling |
| `strike_selection.*` | offsets ≤2, weighted score | cost band 3.0, ATM 2.0, OI 1.5, volume 1.0, spread penalty 2.0 |
| `default_sl_premium_pts` | NIFTY 12, SENSEX 35 | guardian's stop distance for manual trades |
| `instruments.primary/secondary` | SENSEX / NIFTY | lot economics at ₹15k |
| `instruments.chain_depth` | 3 | ATM ±3 strikes (rate-limit driven, D-013) |
| `week.loss_limit_mult` | 2.5 | week lock at −₹3,750 |
| `week.red_days_to_paper` | 3 | 3 red days → next session forced PAPER |
| `llm.min_conf` | 70 | confidence gate (80 in PROTECT/TRAIL) |
| `llm.suggest_cap_per_day` | 10 | hard daily suggestion budget |
| `llm.timeout_s` | 10 | per-provider timeout |
| `llm.groq_model` / `groq_fallback_model` | llama-3.3-70b-versatile / openai/gpt-oss-120b | |
| `data.tick_staleness_degraded_s` | 10 | > 10s stale → DEGRADED, suggestions suspended |
| `data.tick_staleness_critical_s` | 60 | > 60s with a position → MANAGE MANUALLY |
| `data.chain_snapshot_interval_s` | 60 | chain poll |
| `data.pnl_tick_interval_s` | 2 | P&L → FSM floor check |
| `data.recon_interval_s` | 15 | REST reconciliation |
| `data.oi_diff_lookback_min` | 30 | OI-shift comparison window |
| `guardian.detection` | hybrid | `hybrid` \| `ws` \| `rest_poll` |
| `guardian.poll_interval_s` | 2 | REST order-book poll |
| `guardian.sl_attach_deadline_s` | 60 | seconds before guardian attaches a stop |
| `guardian.yield_to_manual_sl` | true | guardian withdraws its stop if yours appears |
| `guardian.squareoff_verify_s` | 10 | flat-verification timeout |
| `events.debounce.*` | LEVEL_BREAK 15m, VIX 30m, OI 30m, MOMENTUM 20m | per (kind, key) |
| `events.thresholds.*` | level 0.05%, VIX 5%, OI 20%, momentum 2×ATR | |
| `events.times.*` | 09:00, 09:35, 12:30, 15:35 | pre-market, OR set, midday, day-end |
| `push.floor_warning_rupees` | 200 | distance-to-floor warning trigger |
| `mode` | **LIVE** | PAPER \| LIVE. Go-live gate removed (D-002) |

### 3.2 Secrets — `.env` only, never in code or logs

`ARJUN_ACCESS_KEY`, `ARJUN_SECRET_KEY`, `GROWW_TOTP_TOKEN` (API key), `GROWW_TOTP_CODE`
(TOTP seed), `GROQ_KEY`, `VAPID_PUBLIC/PRIVATE/SUBJECT`, `API_SHARED_SECRET`,
`SITE_ADDRESS`, `PWA_ORIGIN`, optional `CLAUDE_BRIDGE_URL`, optional `BACKUP_S3_BUCKET`.
`.env` is git-ignored; `logging_setup.redact()` strips secrets from logged payloads.

---

## 4. The risk engine (`risk_engine.py`) — provided code, never modified

One number drives everything: `dayPnL = realized + unrealized`, marked on live prices.
`peak = max(dayPnL)` intraday.

### 4.1 States and floors

| State | Entered when | Floor | Entries |
|---|---|---|---|
| **NORMAL** | day start | −1500 | up to 3 |
| **EARNED** | realized ≥ 0.45 × T (₹1,125) | `max(prev, 0.70 × dayPnL)`, ratchets | 4th unlocked |
| **PROTECT** | dayPnL ≥ T (₹2,500) | `max(prev, 0.5 × T)` = ₹1,250 | yes, risk r/2, conf ≥ 80 |
| **TRAIL** | dayPnL ≥ 1.3 × T (₹3,250) | `max(prev, 0.75 × peak)`, ratchets | yes, risk r/2, conf ≥ 80 |
| **LOCKED** | any lock below | — | none until next session |

Promotions are a one-way ladder; floors ratchet **up only**.

### 4.2 The four locks

1. `dayPnL ≤ −loss_limit` (−₹1,500) — checked on every P&L tick
2. `consecutive_losses ≥ 2` — checked on trade close
3. floor breach in EARNED / PROTECT / TRAIL — `dayPnL ≤ floor`
4. clock reaches `squareoff_at` (15:10) — checked every 15s

Every lock emits `EngineEvent(square_off=True, lock=True)`. `app._on_engine_event` then
squares off, books the fills, and pushes `STATE_CHANGE` — **except** a boot-time lock after
hours with zero trades taken, which logs only (no notification spam).

### 4.3 Entry gate — `can_enter()`

Rejection reasons, in evaluation order: `KILL_SWITCH` → `LOCKED` → `TRADE_CAP` →
`OUTSIDE_WINDOW` → `EXPIRY_CUTOFF` → `CONFIDENCE` → `RISK_EXCEEDED`. Returns the effective
`max_risk` (r, halved in PROTECT/TRAIL) and the required `min_confidence`.

### 4.4 Worked example (live params)

`+1500` → EARNED, floor 1050, cap 4 → runs to `2500` → PROTECT, floor 1250 → `3250` →
TRAIL, floor 2437.5 → peak `4000` → floor 3000 → pullback to 3000 → square-off, LOCKED,
day ends **+₹3,000**.

---

## 5. Sizing (`core/sizing.py`)

```
premium_per_lot = LTP × lot_size          (lot_size from TODAY's master, never literal)
risk_per_lot    = sl_points × lot_size
lots            = floor(max_risk / risk_per_lot), then max(min_lots), then min(max_lots)
trim lots while cost > max_position_cost  (never below min_lots)
```

Refusals: unknown lot size · no premium · no stop distance · one lot costs > ₹14,000 ·
cost > capital. **`over_risk`** is a flag, not a refusal: one lot is always offered even if
its risk exceeds r — and at tap time `proposed_risk` is deliberately not passed for those
cards, so the engine cannot reject what it was told to allow.

### Strike selection (D-010)

Candidates are ATM ±2 from the live chain, scored: inside preferred cost band (3.0) +
ATM proximity (2.0) + OI (1.5) + volume (1.0) − spread penalty (2.0). If a different
contract wins, the LLM's absolute premiums are **rescaled by the same ratios** onto the new
contract's live premium; if the ratios are not `0 < sl/mid < 1 < tgt/mid`, the trade is
refused rather than priced wrongly.

---

## 6. Data layer — what is polled, how, and in what priority

**Priority doctrine (design §3.4):** REST poll = truth · stream = speed · local DB = belief.

| What | Interval | Mechanism | Broker call |
|---|---|---|---|
| Index + option prices | **3s** | REST `get_ltp_batch`, one batch → one timestamp | yes |
| P&L → FSM floors | **2s** | reads the cached prices | no |
| Guardian order book | 2s | WS order stream first, REST every 6th cycle in `hybrid` | yes |
| Reconciliation | 15s | REST orders + positions | yes |
| Option chain (ATM±3) | 60s | REST quote batch | yes |
| Detectors | 20s | local candles | no |
| Housekeeping (targets, time stops, expiry, candle flush) | 10s | local | no |
| Broadcast to PWA | 1s | WebSocket `/live` | no |
| Clock / lock check | 15s | local | no |
| Instrument master | 08:45 IST | REST | yes |
| Day reset | 09:00 IST | local | no |
| Session close | 15:35 IST | local | no |
| DB backup | 16:00 IST | S3 if `BACKUP_S3_BUCKET` set — **currently unset, so no backups** | no |

**Why REST and not the 65ms stream:** the NATS feed connects but delivered **zero ticks in
30 seconds of live market** (D-012), so REST became the primary price path. Rate budget: ~1.2
of 4 req/s. Consequence: the screen and the FSM can be up to ~3s behind the market. The
**stop-loss is unaffected** — it is a real SL-M order resting at the exchange.

**Nothing is stored from a stream.** `ticks_1m` is built by `CandleBuilder` from the 3s REST
poll and flushed every 10s. Everything the LLM sees is REST-derived.

### Data-quality gates

- \> 10s stale on a subscribed key → **DEGRADED** → new suggestions suspended, positions still managed
- \> 60s with an open position → **critical** → `SYSTEM` push "manage manually"
- chain snapshot failure → LLM calls skipped (never reason on a stale chain)

### Candles and levels

`CandleBuilder` keeps `maxlen=2000` 1-minute bars per symbol, backfilled at boot with a
2400-minute span so the previous session is present. Derived: 5m aggregation, ATR(14),
opening range (09:15–09:30), PDH/PDL, PDC (for today's % change).

---

## 7. Event engine — code decides when to wake the LLM

The LLM never polls and never sees ticks. Nine detectors, each writing an `events` row and
packaging a snapshot. Debounce is per `(kind, key)`, so two different levels breaking do not
suppress each other.

| Event | Trigger | Debounce |
|---|---|---|
| `PRE_MARKET` | 09:00 | once/day |
| `OPENING_RANGE_SET` | 09:35 | once/day |
| `LEVEL_BREAK` | a **cross** of PDH/PDL/ORH/ORL confirmed by a 1-min close, ≥0.05% beyond | 15m per level |
| `VIX_SPIKE` | India VIX ±5% vs first reading of the day | 30m |
| `OI_SHIFT` | > 20% OI change at a tracked strike vs 30m ago | 30m per strike |
| `MOMENTUM_BURST` | 5-min range > 2 × ATR(14) | 20m |
| `POSITION_EVENT` | position past 50% of target, or nearing its stop | 5m per symbol |
| `MIDDAY_CHECK` | 12:30 | once/day |
| `DAY_END` | 15:35 | once/day (no LLM suggestion) |

`LEVEL_BREAK` requires a genuine cross, not proximity — distance alone re-fired every
debounce window and burned the daily LLM budget.

---

## 8. LLM layer

### 8.1 Routing

Attempt order: **Claude bridge** (`CLAUDE_BRIDGE_URL`, ×2 retries, only if configured) →
**Groq `llama-3.3-70b-versatile`** → **Groq `openai/gpt-oss-120b`** → fail closed. Timeout
10s each. Every attempt writes an `llm_calls` row (provider, model, latency, tokens, ok,
error). No provider configured → logged and nothing happens.

Daily budget: `suggest_cap_per_day: 10`, counted per suggestion purpose.

### 8.2 Output contract

Strict JSON: `action` (SUGGEST | NO_TRADE), `instrument`, `direction`, `strike_offset`,
`entry_zone{low,high}`, `stop_loss_premium`, `target_premium`, `time_stop_minutes`,
`confidence`, `thesis`, `invalidation`, `risk_reward`. **No field for size, lots, floors or
limits exists** — those tokens cannot leak into execution. Invalid output gets one repair
retry, then is dropped. `NO_TRADE` is stored as calibration data.

### 8.3 Code gates applied after parsing (`lifecycle.apply_gates`)

In order: schema valid → `can_enter` (state/window/cap/confidence) → **RR ≥ 1.5** → strike
resolvable from today's master → strike selection + price rescale → lot size known →
`size_with_cap` → queued with a **90-second TTL**.

---

## 9. Trade lifecycles

### 9.1 Gatekeeper path (PWA-approved)

```
event → snapshot → LLM → gates → PWA card (90s TTL, SUGGESTION push)
  → approve tap (Idempotency-Key header)
  → re-validation at tap time: kill switch, week lock, can_enter (state may have changed)
  → LIMIT BUY at entry_high, order_reference_id = idempotency key (broker-side de-dupe)
  → _confirm_fill: poll until filled / cancelled / timeout
       unfilled → cancel entry, delete trade row, REFUND the trade slot, SYSTEM push
       partial  → qty and stop mirror what actually filled
  → SL-M placed immediately at stop_loss (a position without a stop is never allowed)
  → monitored: target (tick-driven), time stop, FSM floors
  → exit → net_pnl with real costs → FSM update → journal row
```

Approval is guarded by an in-flight set, so a double tap cannot double-order; the broker also
de-dupes on `order_reference_id`.

### 9.2 Guardian path (manual Groww-app trade)

```
WS order event or 2s REST poll
  → prime() baseline: every order already on the book at boot is history, never re-adopted
  → skip if: not primed · already seen · created before process start · ours · not FILLED/PARTIAL · SELL
  → requires an actual open position (a completed round trip is not exposure)
  → counts against the trade budget (on_trade_opened)
  → trade row origin=manual; violation row if placed during LOCKED / outside window / over cap
  → GUARDIAN push
  → at 60s: if the uncovered quantity > 0, place SL-M for THE GAP ONLY
       (quantity-aware: a 20-qty stop does not protect 80 qty)
       no default stop points for that instrument → refuse and alert, never invent a distance
  → if YOUR stop appears later, guardian cancels ITS OWN so exactly one stop is ever live
```

### 9.3 Exits

| Exit | Mechanism |
|---|---|
| Stop loss | broker-resident **SL-M** order — fires at the exchange, independent of this system |
| Target | **tick-driven** (10s housekeeping): cancels only SENTINEL's own stop, then MARKET exit. Deliberately not a resting order — a second live exit beside your stop could double-sell into a short (D-019) |
| Time stop | same `_market_exit` path |
| Floor breach / lock | `guardian.square_off_all()` → market exit every FNO position, verify flat via REST within 10s, retry once, then alert |
| Manual | SQUARE OFF button |

### 9.4 The "never override" invariant (D-018)

`GrowwAdapter` records the `order_reference_id` of every order it places. `cancel_resting_stops`
cancels **only** its own; anything unrecognised is logged `leaving the trader's own stop in
place`. Accepted trade-off: a stop of yours can be left orphaned on a flattened symbol —
SENTINEL alerts instead of cancelling it.

### 9.5 Scope guard (D-020)

One Groww account also holds CASH, CURRENCY and COMMODITY (MCX). `get_orders`/`get_positions`
ask the server for `segment=FNO` **and** re-filter on `exchange ∈ {NSE, BSE}`. So an MCX or
equity trade of yours is invisible to SENTINEL: it cannot consume a trade slot, and
`square_off_all` cannot exit it.

---

## 10. Broker adapter (`brokers/groww.py`)

- **SDK:** `growwapi==1.5.0`. Auth = access token from API key + TOTP (`pyotp`), refreshed on expiry.
- **Idempotency:** Groww's native `order_reference_id`; `get_order_status_by_reference` is
  checked before placing, so a retry cannot double-place.
- **Rate limiting:** token bucket, 4 req/s, burst 8, across every call.
- **Circuit breaker:** 5 consecutive failures → 30s cooldown.
- **Feed thread:** `GrowwFeed` is constructed and driven on a dedicated thread with an
  installed-but-not-running event loop, because the SDK calls `run_until_complete` itself
  (D-011).
- **Order types:** MARKET, LIMIT, SL, SL_M. Exits use **SL-M / MARKET** — a guaranteed exit
  beats a hopeful price at this size.
- **`square_off_all`:** no bulk endpoint exists; positions are inverted one by one with a
  unique reference per attempt (a deterministic key made the retry a silent no-op).

**SimAdapter** implements the same port with simulated fills, 1-tick adverse slippage and the
real cost model. `mode: PAPER`, or a forced-paper day, selects it — the code path above is
otherwise identical.

### Cost model (`brokers/costs.py`)

Brokerage ₹20/order · STT 0.1% sell-side · exchange txn NSE 0.03503% / BSE 0.0325% · SEBI
turnover · GST 18% on (brokerage + txn + SEBI) · stamp duty buy-side only. A round trip is
~₹50–60, i.e. 4–5% of an r=₹1,200 budget. **Realized P&L is always net.** Unrealized is gross;
`/state` also reports `est_charges` and `net_unrealized` per position and `charges_today` /
`open_charges_est` for the day. The FSM marks on gross unrealized (D-022).

---

## 11. Reconciliation

`Reconciler.run_once()` every 15s: REST positions + orders vs local DB. REST always wins.
Detected discrepancies: broker flat but DB thinks open (DB corrected, critical), quantity
mismatch, unknown position, terminal order still open locally. `verify_flat()` polls after a
square-off. **The reconciler never places or cancels an order** — it only corrects belief.

---

## 12. Persistence

SQLite, WAL mode, at `/app/data/sentinel.db` (bind-mounted, survives redeploys).

| Table | Contents |
|---|---|
| `ticks_1m` | 1-minute candles per symbol |
| `chain_snapshots` | ATM±3 strike rows: LTP, OI, ΔOI, IV, volume |
| `events` | every detector firing, its snapshot, whether it reached the LLM |
| `suggestions` | every LLM output incl. NO_TRADE and GATED, with reason |
| `trades` | entry/exit, sl/target, gross, costs, net, origin, guardian note |
| `pnl_curve` | P&L, peak, floor, state — one row per P&L tick (~2s), not the 10s the design doc specifies |
| `violations` | manual trade during lock/window, week lock |
| `llm_calls` | provider, model, latency, tokens, ok/error |
| `system_state` | kill switch, red-day streak, forced-paper flag, week state |
| `push_subscriptions` | Web Push endpoints |

Restart recovery: `orchestrator.rearm()` replays today's **closed trades in order through the
real engine**, then applies the last known mark — so recovery cannot invent a state the FSM
would never produce.

---

## 13. API surface

Auth: `X-Sentinel-Key` header against `API_SHARED_SECRET` on everything except `/health`
and `/push/key`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness, mode, IST time, kill switch |
| GET | `/state` | the single payload the PWA renders |
| POST | `/suggestions/{id}/approve` | approve tap; `Idempotency-Key` header |
| POST | `/suggestions/{id}/reject` | reject with reason |
| POST | `/squareoff` | market-exit everything, verify flat |
| POST | `/killswitch` | `{action: ON\|OFF}`; ON requires typed `ENABLE` |
| GET | `/config` | params + factory defaults + `editable_now` + redacted secrets |
| PUT | `/config` | write params; requires `confirm:"CONFIRM"`; **423 between 09:15–15:30 IST** |
| POST | `/config/reset` | restore factory defaults; requires `confirm:"RESET"`; same lock |
| GET | `/push/key` | VAPID public key |
| POST | `/push/subscribe` | upsert subscription by endpoint |
| POST | `/push/test` | send a test push |
| GET | `/report/dayend` | day verdict, per-trade replay, discipline score |
| GET | `/report/journal` | expectancy, PF, win%, calibration, violations |
| WS | `/live` | state pushed every 1s |

Config writes are refused during market hours by design: you cannot loosen a risk rule
mid-tilt. Validation happens by constructing a `Settings` from the candidate dict **before**
touching disk, so a bad edit cannot leave an unbootable file. The first runtime write copies
the annotated original to `params.annotated.yaml` (safe_dump drops comments).

---

## 14. PWA

React 18 + Vite + Tailwind, installable, fonts self-hosted via `@fontsource` (no CDN).
Design tokens from `docs/design_spec.md`: paper `#FEFEFD`, ink `#0B0B0A`, profit `#0E9F6E`,
risk `#E5484D`, guardian `#D97706`, AI cobalt `#2447F5`. Colour is semantic only.

| Screen | Contents |
|---|---|
| **Today** | market strip with % change · day P&L hero · **Day Rail** (hatched lock zone, ratcheting floor tick, live dot) · trade dots · risk/trade · entry window · charges line · closed trades · status strip |
| **Positions** | per position P&L, after-charges, **distance-to-SL bar**, guardian log, SQUARE OFF |
| **Signals** | AI card (cobalt): model + trigger, thesis, entry/SL/target/time-stop, confidence bar, sizing note, Reject/Approve 1:2, 90s countdown; plus today's signal log with verdict chips |
| **Journal** | expectancy, PF, win%, avg win/loss, net, costs, calibration table, violations |
| **System** | master kill switch (typed `ENABLE`), connection health, mode, notifications, **Open config** |
| **Config** | every param as a typed field, default shown when changed, Save all, Reset all to defaults, read-only during market hours |
| **Locked** | ring, reason, day result, live unlock countdown, day-end report, Groww kill-switch link |
| **Day-end** | P&L, state path, trades, win%, costs, suggested-vs-taken, discipline score |

Service worker: HTML shell **network-first** (cache-first pinned installed apps to a stale
bundle), hashed `/assets/` cache-first, `/state` and `/report` never cached.

### Push (VAPID)

Kinds: `SUGGESTION` · `FLOOR_WARNING` · `STATE_CHANGE` · `GUARDIAN` · `SYSTEM` · `REPORT`.
Repeat suppression by `(kind, message)`: FLOOR_WARNING and SYSTEM 300s, STATE_CHANGE and
GUARDIAN 60s, **SUGGESTION never suppressed**, unknown kinds 120s. Without this,
FLOOR_WARNING fired from the 2s P&L tick sent ~150 notifications in five minutes.
On every app load the PWA re-asserts its existing subscription with the server, because a
subscription made while the backend was down would otherwise never reach it.

**UNVERIFIED:** end-to-end push delivery to the iPhone has not been observed. iOS requires
home-screen installation and iOS ≥ 16.4.

---

## 15. Overtrading defence stack

1. **Gatekeeper** — PWA orders pass the FSM in code
2. **Guardian** — manual trades detected in ~2s, counted, stopped
3. **Capital starvation** — only ₹15k in the account; transfer delay kills impulse re-loads
4. **Escalating friction** — floor-warning push at ₹200, lock screen with reason and countdown
5. **Violation ledger** — permanent rows, surfaced in the day-end report and journal
6. **Kill switch** — one tap off; typed `ENABLE` to re-enable; broker deep link in the lock screen
7. **Week governor** — week loss ≥ ₹3,750 → locked for the week; 3 red days → next session forced PAPER (persisted, read at boot)

---

## 16. Deployment and operations

```bash
scripts/provision_ec2.sh          # one-time: docker, buildx, compose, dirs, systemd
scripts/deploy.sh <ip>            # build PWA locally, rsync, build image, start, health-check
scripts/deploy.sh <ip> --no-start # ship code/image without starting the backend
```

`--no-start` never stops a backend that is already running; it warns that the running
container is on the previous image.

Container runs as uid 10001; `./data` is chowned to it (SQLite needs write access), and the
`growwapi` package directory is chowned in the image because the SDK writes its instrument
CSV into site-packages.

Other scripts: `premarket_drill.py` (pre-open readiness), `ws_probe.py` (D-001 — does the WS
order feed report app-placed orders; needs a cancellable FNO order during market hours).

Monthly cost: Groww ₹499 + LLM ₹300–800 + EC2 (free-tier eligible) ≈ ₹800–1,700.

---

## 17. Test suite — 145 tests

| File | Covers |
|---|---|
| `test_risk_engine.py` | **17 provided tests** — the executable spec of the FSM. Never weakened. |
| `test_orchestrator.py` | promotions, floors, week governor, re-arm, forced paper, slot refund |
| `test_lifecycle.py` | gate chain, RR, rescale-on-substitution, fill confirm, unfilled refund, target exit sparing your stop |
| `test_guardian.py` | priming, startup cutoff, quantity-aware protection, yield-to-your-stop, violations |
| `test_sizing.py` | lot maths, cost cap, per-instrument ceiling, over_risk flag |
| `test_costs.py` | hand-computed legs, STT/stamp sides, BSE vs NSE, net P&L |
| `test_sim.py` | SimAdapter fills, slippage, stop triggering |
| `test_killswitch.py` | typed re-enable, entry guard |
| `test_broker_scope.py` | FNO-only reads; square-off never touches MCX |
| `test_config_io.py` | write/reload round trip, invalid edit never reaches disk, reset, annotated backup |
| `test_push_throttle.py` | repeat suppression, suggestions never throttled, cooldown expiry |

Run: `python -m pytest backend/tests -q` · Lint: `ruff check backend scripts`
(`risk_engine.py` is ruff-excluded — it is provided verbatim).

---

## 18. Known state, gaps and unverified items

**Verified working:** boot against live Groww · REST prices (NIFTY 24,639.85 / SENSEX
78,846.63 / VIX 11.89 observed) · 750 candles backfilled per index with PDH/PDL present ·
auth rejection without the key · guardian priming after the poisoned-DB incident ·
after-hours boot locks without issuing a square-off · config write/reset · 145 tests.

**Never exercised:**
- **SENTINEL has never placed a real order.** The gatekeeper entry path has run only against SimAdapter.
- Push delivery to the iPhone.
- **D-001** — whether Groww's WS order feed reports orders placed in the Groww app. Resolves passively via `/state → health.guardian_detected_by`, or actively with `scripts/ws_probe.py`.
- Guardian against a real manual trade.
- A real floor breach and square-off with money at risk.

**Known gaps:**
- `BACKUP_S3_BUCKET` unset → **no database backups**.
- Prices are ~3s behind the market; the 65ms WS path is unproven (D-012).
- Nothing the LLM sees comes from a stream.
- LLM confidence calibration has no samples yet; the §6.3 kill-criterion (70+ bucket winning < 45% after 60 suggestions → demote AI to commentary) cannot fire yet.
- The go-live gate was **deliberately deleted** (D-002). There is no paper-trading requirement in code.

---

## 19. Decision log

Full rationale in `docs/DECISIONS.md` (D-001 … D-022). Highlights:

| ID | Decision |
|---|---|
| D-001 | Guardian runs hybrid detection until the WS order feed is proven |
| D-002 | Go-live gate removed at the trader's explicit instruction |
| D-003 | No %-of-capital premium cap; absolute ₹14,000 cost cap instead |
| D-009 | Expiry day changes only the entry cutoff — risk is unchanged |
| D-010 | Strike selection scores toward a ₹7–12k cost band |
| D-011 | `GrowwFeed` needs its own thread with a non-running loop |
| D-012 | REST is the primary price path; NATS delivered nothing |
| D-013 | Token-bucket rate limiting; chain 60s, depth 3 |
| D-014 | Guardian primes the order book at boot (a restart once fabricated 29 trades and 58 violations) |
| D-015 | Candle backfill wired up; PDH/PDL/levels were permanently `None` |
| D-016 | Guardian withdraws its own stop when yours appears |
| D-018 | Never override an order the trader placed — enforced on every order-touching path |
| D-019 | Target exit is tick-driven, not a resting order |
| D-020 | Broker reads scoped to FNO on NSE/BSE |
| D-021 | Every parameter editable from the phone, with factory reset |
| D-022 | Charges surfaced per position and per day |

---

## 20. Tomorrow morning

1. `docker compose up -d backend` (or leave it running overnight)
2. 08:45 — instrument master refreshes; check `/state` mode `LIVE`, kill switch `ON`
3. 09:00 — pre-market event; `PRE_MARKET` brief
4. 09:20 — entry window opens
5. Watch `health.guardian_detected_by` after your first manual trade — that answers D-001
6. 15:00 entries close · 15:10 square-off · 15:35 day-end report

**The first approve tap places a real order with real money.** Nothing in this system has
done that yet.

# SENTINEL — Decisions log

Every non-obvious choice made during the build, with its reason. Referenced by
`claude.md` §0 and the build plan's `[HUMAN]` checkpoints.

---

## D-001 · Guardian manual-trade detection: REST polling primary
**Date:** 2026-08-05 (built overnight, market shut)
**Status:** provisional — resolve at the 08:45 drill

Build plan Phase 0 §6 requires a live check: does Groww's order feed emit events
for orders placed **in the phone app**, or only for API-placed orders?

Markets were closed during the build, so this could not be answered. `growwapi
1.5.0` does expose `subscribe_fno_order_updates` / `subscribe_equity_order_updates`
with `on_data_received` callbacks, but whether app-placed orders traverse them is
unverified.

**Decision:** implement BOTH paths. `guardian.detection: rest_poll` is the default
(5s REST order-book diff — the conservative option, worst case ~5s detection lag).
The WS subscription also runs and is treated as an accelerator: whichever source
sees an unknown order first wins, deduped by broker order id.

SUPERSEDED — see "D-001 REVISED" below. Kept for history.
order arriving over WS, flip `guardian.detection: ws` in `params.yaml`.

---

## D-002 · Go-live gate removed
**Date:** 2026-08-05
**Requested by:** user, explicitly, after being shown the consequence

Build plan Phase 6 and design doc §9 specify a code-enforced boot refusal: `mode:
LIVE` must not start without `GO_LIVE_CHECKLIST.md` fully ticked plus >=40 paper
sessions, >=60 suggestions, positive expectancy, discipline >=95%, guardian 10/10.

User directed that the gate be deleted entirely and the system go live on
2026-08-06 morning. Offered alternatives (typed override with logging; override
gated on a pre-market drill PASS) were declined.

**Consequence, recorded plainly:** no order path has ever executed against real
Groww, guardian detection is unproven (D-001), and instrument/strike mapping is
untested against live data. `mode: LIVE` therefore boots with real capital behind
logic validated only by unit, integration and replay tests.

**Retained mitigations** (not part of the deleted gate): kill switch, broker-resident
SL-M orders, three-source reconciliation, fail-closed LLM, idempotency keys,
`max_position_cost` hard cap.

---

## D-003 · Position cost cap: absolute Rs.14,000
**Date:** 2026-08-05

Design doc §4.4/§6.2 specified `premium <= 0.35 x C`; the v1.1 amendment said "no
premium-%-of-capital cap"; `params.yaml` said `1.0`. Direct contradiction (blocker B4).

**User decision:** no percentage cap. Instead a hard absolute ceiling — allow 1 lot
only when total position cost is **under Rs.14,000**, else reject the trade.

Implemented in `core/sizing.py` as an additive gate. `risk_engine.size_position`
is untouched (build plan §0.1). At capital Rs.15,000 this leaves ~Rs.1,000 headroom
for costs. Configurable: `sizing.max_position_cost`.

---

## D-004 · Groq model pin
**Date:** 2026-08-05

Queried the live models endpoint with the user's key: 15 models available.
Design §6.1 asks for "Groq Llama-3.x-70B".

**Pinned:** `llama-3.3-70b-versatile`, fallback `openai/gpt-oss-120b`.
Both support JSON-shaped output, which the strict schema in §6.2 needs.

---

## D-005 · No webhooks exist — WS only
**Date:** 2026-08-05

Confirmed by inspecting `growwapi 1.5.0`: zero webhook references. Live P&L comes
from `subscribe_fno_position_updates` + `subscribe_ltp`, feeding `on_pnl_tick` at
`data.pnl_tick_interval_s`. REST remains the 15s truth per §3.4.

---

## D-006 · Deploy target: EC2 t4g.small, ap-south-1
**Date:** 2026-08-05

Cloud Run rejected: ephemeral filesystem breaks SQLite+WAL, and long-lived WS
connections bill as request duration (measured on the user's own `xau-cockpit`:
697 active instance-hours/month). Oracle Always Free rejected on reliability —
no SLA, idle reclamation, Mumbai ARM capacity waits.

**Chosen:** EC2 `t4g.small` (2 GB, ARM Graviton) in `ap-south-1`, 20 GB gp3,
Elastic IP. ~$14/month against the user's $124 of credits (115 days remaining),
so effectively free for the near term. In-region: Groww's NATS origin is in India.

HTTPS via Caddy + `<elastic-ip>.sslip.io`, which resolves to the IP and gets a
real Let's Encrypt certificate — required because iOS Web Push refuses
self-signed certs. No domain purchase needed.

---

## D-009 · Expiry day: entry cutoff only, no risk reduction
**Date:** 2026-08-05 · **Requested by:** user

Design doc §4.1 and the amendment both halved `r` on expiry days
(`expiry_risk_scale: 0.5`). User directed that expiry change **nothing** except the
earlier entry cutoff (`expiry_entry_close: 14:30`).

Set `windows.expiry_risk_scale: 1.0`. No engine edit — `RiskConfig` already reads
this from YAML, so `risk_engine.py` and its 17 tests stay untouched (§0.1). The
provided test `test_expiry_cutoff_and_half_risk` still asserts 600 because it
constructs a bare `RiskConfig()` with the dataclass default of 0.5; that is the
library default, not the running config. Runtime behaviour comes from params.yaml.

Consequence: on expiry afternoons full `r` is at risk against gamma/theta, which
§4.1 called out as the reason for halving. Accepted deliberately.

---

## D-010 · Strike selection: prefer Rs.7,000-12,000 one-lot cost
**Date:** 2026-08-05 · **Requested by:** user

Target a 1-lot position costing **Rs.7,000-12,000**, with cheaper or dearer allowed
when the band has nothing suitable. Hard ceiling stays Rs.14,000 (D-003).

At current lot sizes the band maps to: SENSEX (lot 20) premium ~Rs.350-600 ·
NIFTY (lot 65) premium ~Rs.108-185. Both are normal ATM weekly ranges, so the
band is reachable rather than aspirational.

"Good possibility of movement" is scored, not asserted — `strike_selection.weights`
in params.yaml rank candidates on cost band, ATM proximity, open interest, volume,
and a bid/ask spread penalty. Only ATM +-2 is considered, which deliberately excludes
deep-OTM tickets that need a huge move to pay.

The scorer only *ranks* candidates. It cannot create a trade, override a risk gate,
or raise size — all of that stays with the engine (§0.1, §0.6).

---

## D-007 · Python 3.11
**Date:** 2026-08-05

Local default is 3.14.5; spec says 3.11+. Pinned to **3.11** for both the venv and
the Docker image (`python:3.11-slim`) so local tests and production run the same
interpreter, and to stay on well-supported wheels for SQLAlchemy 2.x and pywebpush.

---

## D-011 · Groww feed must be built and driven on its own thread
**Date:** 2026-08-05 · found by booting the real app

`GrowwFeed` captures the ambient asyncio loop when constructed, and its subscribe
methods call `loop.run_until_complete` internally. Constructed on the main thread
under uvicorn it captured the *running* server loop, so every subscribe raised
`Cannot run the event loop while another loop is running` — the tick feed silently
never connected. Symptom would have been a system that boots clean, reports healthy,
and receives no market data at all.

**Fix:** `_FeedThread` in `brokers/groww.py` — a worker thread with an *installed but
not running* loop. `GrowwFeed` is constructed there and all subscribe/unsubscribe
calls are marshalled onto it. Idle iterations pump the loop briefly so NATS
callbacks are serviced.

Verified: `Socket connection successful` + `tick feed subscribed count=3` at boot.
Tick *delivery* is still unverified because the market was shut — confirm at the
08:45 drill that prices actually populate.

---

## D-012 · REST polling is the primary price path; NATS is an accelerator
**Date:** 2026-08-05, verified during live market hours

D-011 got the NATS subscription connecting (`Socket connection successful`), but no
ticks were ever delivered — 30s of live market with zero prices. The SDK's feed
depends on its captured loop being driven continuously, which does not survive being
marshalled onto a worker thread.

Rather than fight it with real money on the line, the price path was inverted:

- **Primary:** `FeedService.poll_rest()` — batch `get_ltp` over REST every 1s for the
  watchlist (2 indices + VIX + any open contracts, so ~5 symbols). Feeds the same
  `on_tick`, so candles, staleness gates and the FSM are unchanged.
- **Accelerator:** the NATS subscription still runs. If ticks arrive they are used;
  if they never come, nothing breaks.

This also matches §3.4, which already makes REST the source of truth. A 1s REST
cadence is comfortably inside the 2s FSM P&L requirement and the 1-minute candles.

Verified live at 10:58 IST: NIFTY 24,639.85 · SENSEX 78,846.63 · VIX 11.89,
feed age 0.9s, `feed_degraded: false`, chain fresh for both indices, candles building.

---

## D-013 · Groww rate limits force a global token bucket
**Date:** 2026-08-05, hit live

The first live deploy immediately produced `Rate limit has breached for your
request` on both `get_quote` and `get_ltp`. Cause: a 1s price poll *plus* a 30s
chain sweep that made one `get_quote` call per contract — ATM±5 on two indices is
44 individual calls every 30 seconds.

**Fixes:**
- `_RateLimiter` in `brokers/groww.py` — one token bucket (4 req/s, burst 8) in
  front of every broker call, so no job needs to know about any other job's cadence.
- Rate-limit responses are now retried with escalating backoff rather than counted
  as hard failures.
- `chain_depth` 5 → 3, `chain_snapshot_interval_s` 30 → 60, price poll 1s → 3s.

Chain coverage drops to ATM±3, which still comfortably covers the ATM±2 that strike
selection considers (D-010).

---

## Review findings (two passes, all fixed)

| Defect | Consequence had it shipped |
|---|---|
| `check_time_stops` called `square_off_all()` | One timed-out trade would market-exit **every** position, including manual ones being managed by hand |
| Exits booked at `last_price = 0.0` | Fabricates a ~100% loss straight into the FSM, tripping the loss limit on a winning trade |
| Recon alerted every 15s | Push spam for any standing discrepancy; real alerts get ignored |
| WS broadcast used `asyncio.get_event_loop()` on a scheduler worker thread | Coroutine dropped silently — the PWA would never receive a live update |
| `POSITION_EVENT` detector never invoked | Dead code; no LLM wake-up at 50%-of-target or stop-approach |
| LLM snapshot shipped `levels={}` and zeroed % changes | The model reasoned without levels or momentum — materially worse suggestions |
| `has_resting_stop` returned `False` when the order book was unreadable | Would stack a second stop on a position that already had one → double exit → unintended short |
| Guardian re-polled positions and orders once per pending position | N+1 broker calls on a safety path, now throttled by the rate limiter |
| Lifecycle tests read the wall clock | Suite passed before 15:00 IST and failed after — now pinned with `freezegun` |

`docs/RUNBOOK.md` "Why it is stopped" records the guardian-vs-your-OCO conflict,
which is a decision for the trader, not a defect.

---

## D-014 · Guardian must baseline the order book at boot
**Date:** 2026-08-05 · found by starting the deployed app and reading `/state`

**Symptom.** First real start mid-session reported `LOCKED · dayPnL -Rs.7,153.78 ·
trades 29/3 · 9 consecutive losses · 58 violations`. All fabricated.

**Cause.** `poll_once()` iterated the whole day's order book and treated every
unseen FILLED BUY as new manual exposure — including round trips closed hours
earlier. That blew the 3-trade cap, manufactured a 9-loss streak, locked the FSM,
and the engine-lock handler then closed all 29 phantom trades at entry price,
booking ~Rs.247 of round-trip costs each (29 x 247 ~ Rs.7,153).

Any mid-day restart would have bricked the session at boot. It was invisible until
the app ran against an account with real same-day history.

**Fix (three parts):**
1. `Guardian.prime()` — called at boot before anything can react, marks every
   existing order as seen. Baselined orders consume no budget and get no stop.
   If the book cannot be read it primes anyway and acts on nothing (fail closed).
2. `_consider()` ignores any order stamped before process start, and refuses to
   act at all until primed.
3. A fill only counts as exposure when the broker still reports an open position
   in that symbol. A completed round trip is history, not risk. When positions
   cannot be verified it assumes exposure and protects.

Only genuinely open positions are adopted, via `adopt_existing`.

**Tests:** four regressions in `test_guardian.py` reproduce the 29-order scenario
and assert zero trades, zero violations, no lock — plus one proving a fill arriving
*after* boot is still caught.

**Data:** the poisoned SQLite file was moved aside on the host as
`data/sentinel.db.poisoned-20260805` rather than deleted, in case the phantom rows
are worth inspecting. A fresh DB is created on next boot.

---

## D-015 · Candle backfill was specified but never wired
**Date:** 2026-08-05 · found during a dead-code audit

`CandleBuilder.backfill()` existed with zero call sites. Build plan Phase 1 requires
"1m/5m candle builder with REST backfill on boot", and without it the consequences
were quiet but real: after every restart `opening_range()` and `prev_day_range()`
returned `None`, so LEVEL_BREAK could not fire and the LLM snapshot shipped
`levels={}`. The system looked healthy and simply never saw a level.

Two sizing details mattered:
- **span 2400 minutes**, not 800. 800 reaches only this morning; PDH/PDL need the
  previous session. Verified: 2400 returns 750 bars across 2 sessions.
- **`CandleBuilder.maxlen` 500 -> 2000.** 500 is smaller than the 750-bar backfill,
  so the deque silently evicted the previous day and PDH/PDL vanished again — a
  bug that would have hidden behind a working-looking backfill.

Verified live: NIFTY ORH/ORL 24669.2/24611.45, PDH/PDL 24703.9/24428.2 ·
SENSEX 79055.38/78791.71, 79132.97/78212.57 · ATR(14,5m) 19.59 / 57.23.

---

## Removed as unnecessary
**Date:** 2026-08-05

- `tenacity` dependency — retry and circuit-breaking are hand-rolled in `groww.py`;
  nothing imported it.
- `app_state()` in `api/routes.py` — unused helper.
- `asyncio.get_event_loop_policy()` in the approve route — a genuine no-op line.
- `FeedService.add()` / `FeedService.reconnect()` — dead once REST became the
  primary price path (D-012).
- `InstrumentService.is_stale()` — no callers; `refresh()` already self-checks
  the calendar day.
- Empty `bridge/` directory. The laptop Claude bridge (build plan Phase 3 §6) is
  NOT built. `LlmRouter` treats an unset `CLAUDE_BRIDGE_URL` as "skip to Groq", so
  this degrades cleanly rather than failing.

`pandas`, `py-vapid` and `pydantic` are kept: not imported directly, but required
at runtime by `growwapi` (DataFrame instrument master), `pywebpush` and `fastapi`
respectively.

---

## D-001 REVISED · Detection is hybrid; the docs cannot answer it
**Date:** 2026-08-05 · docs checked, config defect found

**Docs verdict: silent.** The Groww Feed page says only *"Subscribe and get the
latest updates on execution of orders for both equity and derivatives."* Neither the
Feed page nor the SDK reference states whether app-placed orders traverse the
websocket. There is no wording about scope in either direction, so the question
cannot be settled from documentation.

**What IS proven:** REST `get_order_list` returns every order on the account,
including app-placed ones. Demonstrated involuntarily — guardian detected all 29 of
the trader's manual orders through the REST path (see D-014).

**Config defect found while checking this.** `guardian.detection` was read in exactly
one place — reported in `/state` — and branched nothing. Both paths ran
unconditionally regardless of its value, so a config reading `rest_poll` was running
the websocket too. The key was decorative.

**Now functional, three modes:**
- `hybrid` (default) — websocket subscribed for immediacy, 5s REST poll alongside.
  Deduped by broker order id, so a trade is never counted twice. Trader prefers the
  websocket; this gives it without depending on undocumented behaviour.
- `ws` — stream primary, REST poll drops to every 6th cycle (~30s) as a backstop.
  Not zero: a websocket that silently stops delivering must not leave a manual trade
  unguarded.
- `rest_poll` — no stream at all.

**D-001 now answers itself.** `Guardian.detected_by` counts which path saw each
manual trade first and is exposed at `/state` as `health.guardian_detected_by`.
After a few manual trades: if `ws` stays 0 while `rest` climbs, the websocket does
not report app-placed orders. No drill needed — live usage produces the answer.

---

## D-016 · Guardian yields to the trader's own stop
**Date:** 2026-08-05 · **Requested by:** user — "check after 60s if SL is placed, if
not place it. Don't override my SL."

Deadline stays at **60s**, as instructed. The collision I was worried about is
solved directly instead of by delaying:

`Guardian.yield_to_own_stops()` runs on every guardian tick. It remembers the order
id of any stop guardian placed itself. The moment a *different* stop order is resting
on that symbol — i.e. one the trader placed — guardian cancels its own and pushes a
GUARDIAN alert saying so. Exactly one stop is ever live.

Why it matters: guardian's stop and the trader's OCO stop both firing would sell the
position twice, flipping a long into an unprotected **short**. Observed timings from
the 2026-08-05 order book show OCO stops landing 2-3 minutes after entry, well past
a 60s deadline, so the overlap was real rather than theoretical.

Config: `guardian.yield_to_manual_sl: true`. Set false to keep both stops.

Guardian already sized its stop from `abs(position.quantity)`, so an 80-qty position
always got an 80-qty stop, never one lot. No change needed there.

---

## D-017 · Per-instrument lot ceiling: SENSEX 4, NIFTY 1
**Date:** 2026-08-05 · **Requested by:** user — "SENSEX max lot size 80 (4x20)"

`sizing.max_lots: {SENSEX: 4, NIFTY: 1}`. SENSEX lot 20 -> up to 80 qty, matching how
the trader actually trades. NIFTY lot 65 stays at 1 because two lots costs more than
capital at any normal premium.

Three limits now apply and the tightest wins:

| Limit | SENSEX at premium 65 | at premium 330 |
|---|---|---|
| risk gate (`r`=1200) | depends on stop distance | same |
| cost cap (Rs.14,000) | 10 lots | 2 lots |
| lot ceiling | **4** | 4 |
| **result** | **4 lots, Rs.5,200** | **2 lots, Rs.13,200** |

Reaching 4 lots requires a stop of ~15 premium points or tighter: 4 x 20 x 15 = 1200,
exactly `r`. A 35-point stop yields 1 lot on risk grounds alone, which is arithmetic,
not a bug — a 4-lot position with a 35-point stop risks Rs.2,800, i.e. 2.7x the
Rs.1,050 daily loss limit. The trader's observed stops are ~11-15 points, so 4 lots
is reachable in practice.

Tests: 4-lot case, ceiling binding below what risk would allow, and cost cap beating
the ceiling.

---

## D-018 · Never override an order the trader placed
**Date:** 2026-08-05 · **Requested by:** user — "never override what I have placed already"

Elevated from a preference to an invariant. Every path that could touch a broker
order now distinguishes SENTINEL's own orders from the trader's:

- `GrowwAdapter` records `make_reference(client_id)` for every order it places, in
  `_own_refs`.
- `cancel_resting_stops(only_refs=...)` cancels ONLY those. A stop it does not
  recognise is logged (`leaving the trader's own stop in place`) and left alone.
- `square_off_all()` uses that, so an emergency flatten no longer cancels the
  trader's stops. It previously cancelled every resting stop, mine and theirs — a
  defect introduced by the review fix for the orphan-stop problem.
- `Lifecycle._cancel_own_stops_for(trade)` pulls only `trade.sl_order_id`, the stop
  SENTINEL rested for that trade.
- `Guardian.yield_to_own_stops()` (D-016) already cancelled only its own.

**Accepted trade-off, stated plainly:** if the trader's stop is still resting after
the system flattens their position, that stop is orphaned and can fire on a flat
book, opening a short. The system now alerts instead of cancelling it. That is the
direct consequence of the rule, and it is the trader's call to make.

---

## D-019 · Target exit is tick-driven, not a resting order
**Date:** 2026-08-05 · resolves review finding #8

`target_premium` was validated, stored, and used only to compute a POSITION_EVENT
percentage. No target order existed and nothing exited at target, so a trade that
touched its target could ride back to a scratch or a loss.

Two ways to fix it. A resting target SELL (or a Groww OCO smart order) is the obvious
one, and it is the wrong one here: a second live exit order sits alongside the
trader's own stop, and both firing sells twice and flips the position short — exactly
the failure D-016 exists to prevent, and it would also mean placing an order that
interacts with theirs (D-018).

So the target is **monitored**, per design §8's "tick-watch target/time-stop":
`Lifecycle.check_targets()` runs on the housekeeping tick, and when the premium
reaches the target it cancels only SENTINEL's own stop and exits with a MARKET order.
No resting order of any kind is created.

Cost: the exit depends on the price feed being alive. If the feed is degraded the
target is not monitored — but the broker-resident stop is still protecting the
downside, so the failure mode is a missed profit, never an unbounded loss.

`check_time_stops()` was refactored onto the same `_market_exit()` helper, so both
profit-side and time-side exits are single-contract and leave the trader's orders
untouched.

---

## D-020 · Broker reads are scoped to FNO on NSE/BSE
**Date:** 2026-08-05 · found while answering "will I see an MCX trade in the app?"

The Groww SDK speaks CASH, CURRENCY, COMMODITY (MCX/NCDEX) and US as well as FNO, and
one account holds them all. `get_orders()`/`get_positions()` were called with
`segment=None`, so what came back depended on an undocumented server default.

Two failure modes, one of them expensive:
1. Guardian would count a commodity or equity fill against the day's *option* trade
   budget and try to attach an option stop to it.
2. `square_off_all()` iterates positions, so a floor breach could have market-exited
   the trader's MCX position — a direct violation of D-018.

`GrowwAdapter` now asks the server for `segment=FNO` **and** re-filters rows on
`exchange in {NSE, BSE}`. A row that omits both fields is kept, because the
instrument-master lookup is the second gate. Out-of-scope rows are counted and logged,
never acted on. Answer to the original question: an MCX trade is invisible to SENTINEL,
by construction rather than by luck.

---

## D-021 · Every parameter is editable from the phone, with a reset
**Date:** 2026-08-05

`params.yaml` claimed in its own header that every value was editable from the PWA
System screen. `GET/PUT /config` existed and were correct; **no UI ever called them**.

Added a Config panel (System → Open config): the whole params tree rendered as typed
fields, the default value shown beside anything that differs, Save all, and Reset all
to defaults. `POST /config/reset` restores from `config/params.default.yaml`, which is
never written to. The market-hours lock (09:15-15:30 IST) is enforced server-side and
mirrored in the UI as read-only.

`yaml.safe_dump` drops comments, so the first runtime write copies the annotated
original to `params.annotated.yaml` once. If the defaults snapshot is missing, reset
degrades to a no-op rather than wiping the config.

---

## D-022 · Charges are shown, not hidden
**Date:** 2026-08-05

Realized P&L was already net of real costs (brokerage, STT, exchange txn, SEBI, GST,
stamp) via `costs.net_pnl`. Unrealized P&L was gross, so an open position looked
~Rs.50-60 better than it could actually be closed for.

`/state` now carries `est_charges` and `net_unrealized` per position, plus
`charges_today` and `open_charges_est` for the day. Positions show "after charges" and
Today shows the day's charges line.

The FSM still marks on **gross** unrealized. Feeding it charge-adjusted P&L would trip
floors slightly earlier, which is arguably more honest, but it changes risk behaviour
and the risk engine is provided code — not a place for an unrequested change.

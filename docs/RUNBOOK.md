# SENTINEL — Runbook

Host `15.252.81.14` · console `https://15-252-81-14.sslip.io` · region `ap-south-1`
Instance `i-0d1632f4452c9d928` (t4g.small) · key `~/.ssh/sentinel-key.pem`

---

## Current state: BACKEND IS STOPPED

Deliberately. See "Why it is stopped" below. Caddy is still up and serving the PWA.

```bash
SSH="ssh -i ~/.ssh/sentinel-key.pem ec2-user@15.252.81.14"
$SSH 'cd /opt/sentinel && docker compose ps'
```

## Start / stop / inspect

```bash
$SSH 'cd /opt/sentinel && docker compose up -d backend'      # start
$SSH 'cd /opt/sentinel && docker compose stop backend'       # stop — no orders, ever
$SSH 'cd /opt/sentinel && docker compose restart backend'    # restart (FSM re-arms from DB)
$SSH 'cd /opt/sentinel && docker compose logs -f backend'    # follow
$SSH 'curl -s localhost:8000/health'                         # liveness
```

Stopping the container is the hardest kill available: nothing can place an order.
The software kill switch (System screen, or `POST /killswitch`) is softer — it stops
entries and suggestions but deliberately still permits risk-reducing exits.

## Redeploy after a code change

```bash
cd <repo>/sentinel && ./scripts/deploy.sh 15.252.81.14
```

Builds the PWA locally, rsyncs, rebuilds the image, restarts. `.env` is copied
separately at 0600 and never enters git.

## Mode

`config/params.yaml` → `mode: LIVE | PAPER`. Restart to apply.
LIVE sends orders to Groww. PAPER routes everything to the simulator while still
using real market data.

**Entries always require a tap in the PWA.** Even in LIVE, the system cannot open a
position on its own. What it does autonomously is only risk-reducing: attach a stop
to an unprotected trade, and square off at a floor breach or the 15:10 cutoff.

## Config changes without touching code

Every tunable lives in `config/params.yaml` and is editable from the System screen
(typed `CONFIRM`), except between 09:15 and 15:30 IST — the lock is deliberate.
Risk-engine values need a restart to take effect.

## Why it is stopped

Stopped mid-session on 2026-08-05 after the first live deploy, for reasons that
must be resolved before it runs again:

1. **You trade manually with your own OCO stops.** 20+ manual orders were placed
   that morning, each followed within minutes by your own `FNO_OCO_V2` stop.
   SENTINEL's guardian is built to attach an SL-M to any position it finds without
   one (§4.5 layer 2). If it attaches a stop and your OCO then also fires, both
   sell — and you end up **short** a contract you meant to be flat on.
   `has_resting_stop()` guards the common case, but not a stop you place *after*
   the guardian's 60-second deadline has already passed.
   → Decide before restarting: keep guardian SL-attach on, or set
   `guardian.sl_attach_deadline_s` well above your usual OCO delay.

2. **Your live sizing exceeds the configured rules.** Observed fills of 100 qty
   (5 lots) against a `max_position_cost` of ₹14,000 and `base: 3` trades/day. A
   running SENTINEL counts each manual trade against the budget and will square
   off everything at the floor — including positions you are managing yourself.

3. **No code review pass had run** when it was first deployed. One has since; the
   second has not.

## Verified working

Groww auth · instrument master (135k rows) · live prices via REST · option chain
with OI diffing · LLM round trip through Groq · FSM crash-recovery · kill switch ·
sizing gates · 97 tests green · HTTPS with a real Let's Encrypt cert.

## Not yet verified

Order placement through SENTINEL (it has never placed one) · guardian SL attach
against a real position · Web Push on your iPhone · whether Groww's WS order feed
reports app-placed orders (D-001).

## Costs

~$14/month against $124 of credits. Teardown: `./scripts/provision_ec2.sh destroy`.

## Backups

Daily 16:00 IST SQLite upload to S3, only if `BACKUP_S3_BUCKET` is set in `.env`.
It is currently unset, so **there are no backups**. The DB lives at
`/opt/sentinel/data/sentinel.db` on the instance's EBS volume.

---

## Settling the websocket question (D-001)

Groww's docs do not state whether the order-update websocket reports orders placed
in the phone app. Two ways to find out:

**Passive — no order needed.** `guardian.detection: hybrid` runs both paths and
counts which saw each manual trade first. Read it any time:

```bash
curl -s -H "X-Sentinel-Key: $SECRET" https://15-252-81-14.sslip.io/state \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["health"]["guardian_detected_by"])'
```

`{"ws": 0, "rest": 7}` after several manual trades means the websocket does not
carry app-placed orders. If `ws` climbs, switch to `detection: ws`.

**Active — one cancellable order, gives latency numbers.**

```bash
PYTHONPATH=backend .venv/bin/python scripts/ws_probe.py
```

Then place a far-from-market LIMIT order on a NIFTY/SENSEX option in the app and
cancel it. Unfilled orders cost nothing. Must be during market hours.

**It must be FNO. Do not use MCX.** The SDK exposes only
`subscribe_equity_order_updates`, `subscribe_fno_order_updates` and
`subscribe_fno_position_updates`. There is no commodity order feed, so an MCX order
would show `ws: 0` and look like a failure that isn't one.

## Detection lag, as configured

- websocket delivering: effectively instant
- websocket silent: **2s** worst case (`guardian.poll_interval_s: 2`)
- total broker load: ~1.2 req/s against a 4 req/s limiter — 70% headroom

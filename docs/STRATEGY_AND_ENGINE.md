# VECTRA_QUANT — Strategy & Engine Master Document

**Version:** 2.0 (Consolidated)
**Purpose:** This document serves as the single source of truth for the VECTRA_QUANT algorithmic trading engine, backtesting procedures, architecture, and operational runbook.

---

## 1. System Architecture & Engine

VECTRA_QUANT is an AI-assisted, human-in-the-loop day-trading console for Indian index options (NIFTY on NSE, SENSEX on BSE). It operates on a decoupled client-server architecture.

### 1.1 Architecture Map
- **Backend**: Python-based REST and WebSocket server built with FastAPI (`0.115.6`), Uvicorn, and SQLAlchemy.
  - **Risk Engine** (`risk_engine.py`): Deterministic state machine that enforces daily loss limits, position sizing, and maximum trade counts.
  - **Guardian** (`guardian.py`): Monitors manual trades placed directly in the broker app, attaches stop-losses autonomously, and forces square-offs at daily limits.
  - **Broker Adapters**: Interfaces for Groww (`growwapi`) and DhanHQ (`dhanhq`) with live token refresh capabilities.
- **Frontend (PWA)**: React-based Single Page Application built with Vite and TailwindCSS.
- **Database**: SQLite (via SQLAlchemy) running in WAL mode, handling trade journals, LLM calls, and configuration parameters.

### 1.2 Enforcement Modes
VECTRA_QUANT runs two parallel enforcement modes to protect capital:
| Mode | Scope | Power |
|---|---|---|
| **Gatekeeper** | Orders approved inside the PWA | Absolute. Orders cannot reach the broker if they violate risk checks. |
| **Guardian** | Orders placed manually in the broker app | Passive detection. Detects entries, counts them against daily limits, force-attaches stop-losses (SL-M) to unprotected trades, and executes forced square-offs at hard limits. |

### 1.3 AI Integration
- **LLM Pipeline**: Uses `llama-3.3-70b-versatile` on Groq (fallback to OpenAI) to provide human-readable trade thesis generation.
- **Strict Boundary**: The LLM *only* suggests. It never sizes, sets limits, or executes trades. The deterministic Python engine dictates all execution logic.

---

## 2. Institutional Strategy

VECTRA_QUANT executes algorithmic trading logic, heavily relying on institutional footprint detection.

### 2.1 Core Strategy (Institutional Breakout)
- Identifies "Long Buildup" (Whale Buying) and "Short Buildup" (Whale Selling) using Option Chain Open Interest (OI) diffing and Put-Call Ratios (PCR).
- Tracks algorithmic regime states: `LONG_BUILDUP`, `SHORT_BUILDUP`, `SHORT_COVERING`, `LONG_UNWINDING`, `NEUTRAL`.
- Targets breakout entries at critical Call/Put wall distances.

### 2.2 Sizing & Risk Caps
- **Absolute Ceiling**: No percentage-based sizing. The maximum permitted position cost is **₹14,000**.
- **Base Trades**: Limited to 3 trades per day.
- **Auto-Kill**: If capital drops below the daily drawdown threshold, the kill-switch engages and no further entries are permitted.

---

## 3. Backtesting Process

VECTRA_QUANT features a custom, on-demand backtesting engine built to validate strategy efficacy over historical 1-minute candle data before live deployment.

### 3.1 On-Demand Backtesting Engine
- Accessible via the PWA (Backtest Tab) or the `/strategies/backtest` API.
- Replays historical 1-minute and 5-minute candles to simulate strategy entry, stop-loss trailing, and target hits.
- Supports **Parameter Wiggle Analysis** to test strategy robustness against minor input variations.
- Includes **Slippage Simulation** to model adverse fill prices in live market conditions.

### 3.2 The 23-Point Gauntlet
Strategies must pass the "Gauntlet Scorecard" to be approved for live trading:
1. Must demonstrate positive statistical expectancy.
2. Must survive simulated slippage and transaction costs.
3. Must remain profitable across parameter wiggles (proving the edge is not over-fitted).
- **Verdict**: Only strategies scoring "INCUBATE" (passes >19 checks and maintains Profit Factor > 1.3) are approved for real capital.

---

## 4. Runbook & Operations

### 4.1 Deployment (AWS EC2)
- **Host**: `t4g.small` (ARM Graviton), `ap-south-1` region.
- **Routing**: Caddy acting as a reverse proxy with Let's Encrypt TLS.
- **Commands**:
  - Start: `docker compose up -d backend`
  - Stop: `docker compose stop backend` (Hard kill - absolutely no orders)
  - Logs: `docker compose logs -f backend`
  - Deploy Code: `./scripts/deploy.sh <ip>`

### 4.2 Known Operational Caveats
- **Manual Trade Conflicts (OCO Stops)**: If you manually place a trade in Groww and immediately attach an OCO stop, VECTRA_QUANT's Guardian might simultaneously attach an SL-M (within 60s). If the market triggers both, you will end up holding a naked short position. Ensure `guardian.sl_attach_deadline_s` is calibrated or manually disable Guardian if using OCO aggressively.
- **Backups**: Daily SQLite dumps to S3 happen at 16:00 IST (requires `BACKUP_S3_BUCKET` in `.env`).

### 4.3 Configurations
- All tunables live in `config/params.yaml` and are hot-editable from the PWA System screen.
- Edits are locked between 09:15 and 15:30 IST to prevent emotional meddling during market hours.

# VECTRA_QUANT

An AI-assisted, human-in-the-loop day-trading console for Indian index options. 

VECTRA_QUANT is designed to provide institutional-grade trading execution and management with built-in risk controls, advanced algorithmic strategy integrations, and an intuitive Progressive Web App (PWA) interface.

## Documentation & Architecture

All details regarding the core trading strategy (Institutional Breakout), the risk engine, enforcement modes, backtesting procedures, and operational runbooks have been consolidated into a single master document:

👉 **[Read the Strategy & Engine Master Document](docs/STRATEGY_AND_ENGINE.md)**

## Quick Start

### Backend Setup

**macOS / Linux**
1. Navigate to the project root.
2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in your credentials.
5. Start the backend server:
   ```bash
   cd backend
   uvicorn vectra_quant.app:create_app --factory --reload
   ```

**Windows (PowerShell)**
1. Navigate to the project root.
2. Create and activate a virtual environment:
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```
   (If script execution is disabled, run once as admin: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`)
3. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
4. Copy `.env.example` to `.env` and fill in your credentials:
   ```powershell
   Copy-Item .env.example .env
   ```
5. Start the backend server:
   ```powershell
   cd backend
   uvicorn vectra_quant.app:create_app --factory --reload
   ```

### PWA Setup

**macOS / Linux**
1. Navigate to the `pwa` directory:
   ```bash
   cd pwa
   ```
2. Install dependencies:
   ```bash
   npm install
   ```
3. Run the development server:
   ```bash
   npm run dev
   ```

**Windows (PowerShell)**
1. Install Node.js LTS if you don't already have it:
   ```powershell
   winget install -e --id OpenJS.NodeJS.LTS
   ```
   (Open a new terminal afterward so `node`/`npm` are on `PATH`.)
2. Navigate to the `pwa` directory:
   ```powershell
   cd pwa
   ```
3. Install dependencies:
   ```powershell
   npm install
   ```
4. Run the development server:
   ```powershell
   npm run dev
   ```

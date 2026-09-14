# SENTINEL

An AI-assisted, human-in-the-loop day-trading console for Indian index options. 

SENTINEL is designed to provide institutional-grade trading execution and management with built-in risk controls, advanced algorithmic strategy integrations, and an intuitive Progressive Web App (PWA) interface.

## Documentation & Architecture

All details regarding the core trading strategy (Institutional Breakout), the risk engine, enforcement modes, backtesting procedures, and operational runbooks have been consolidated into a single master document:

👉 **[Read the Strategy & Engine Master Document](docs/STRATEGY_AND_ENGINE.md)**

## Quick Start

### Backend Setup
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
   uvicorn backend.sentinel.main:app --reload
   ```

### PWA Setup
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

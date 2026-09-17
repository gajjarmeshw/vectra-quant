"""Live order-flow signal engine for the daily depth-driven credit spread.

Everything under this package depends on REAL Level-2 order-book depth from
Dhan's WebSocket feed. That data does not exist historically (the archive
has OHLC+OI+IV, never resting-order depth), so nothing here can be
backtested — see `docs/orderflow_assumptions.md` for the full list of
unverified API assumptions that MUST be checked against a live connection
before this is trusted with real orders.
"""

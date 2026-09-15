"""BrokerAdapter port. Design doc §3.1 — all business logic depends ONLY on this.

Every order carries a `client_id` (uuid4). Adapters MUST treat a repeated
client_id as the same order and never place it twice (build plan §0.4).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

OrderId = str


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"        # stop-loss limit
    SL_M = "SL_M"    # stop-loss market — what we use for exits (§10.9)


class Product(str, Enum):
    NRML = "NRML"
    MIS = "MIS"
    CNC = "CNC"


class OrderStatus(str, Enum):
    NEW = "NEW"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OrderRequest:
    trading_symbol: str
    exchange: str                 # NSE | BSE
    segment: str                  # FNO | CASH
    side: Side
    quantity: int                 # absolute units, already lots x lot_size
    order_type: OrderType
    product: Product = Product.NRML
    price: float = 0.0            # limit price
    trigger_price: float = 0.0    # for SL / SL_M
    client_id: str = ""           # uuid4 — idempotency key, REQUIRED
    validity: str = "DAY"
    tag: str = ""                 # suggestion uuid, for reconciliation


@dataclass(frozen=True)
class OrderPatch:
    quantity: int | None = None
    price: float | None = None
    trigger_price: float | None = None


@dataclass
class Order:
    order_id: OrderId
    trading_symbol: str
    side: Side
    quantity: int
    filled_quantity: int
    status: OrderStatus
    order_type: OrderType
    price: float = 0.0
    trigger_price: float = 0.0
    average_price: float = 0.0
    client_id: str = ""
    tag: str = ""
    exchange: str = ""
    segment: str = "FNO"
    created_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass
class Position:
    trading_symbol: str
    quantity: int                 # signed: +long, -short
    average_price: float
    last_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    lot_size: int = 0
    exchange: str = ""
    segment: str = "FNO"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.quantity != 0


@dataclass
class Funds:
    available: float
    used: float = 0.0
    total: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Quote:
    trading_symbol: str
    last_price: float
    open_interest: float = 0.0
    implied_volatility: float = 0.0
    volume: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    ts: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Instrument:
    trading_symbol: str
    exchange: str
    segment: str
    lot_size: int                 # read DAILY from the master, never hardcoded (§0.3)
    instrument_type: str = ""     # CE | PE | FUT | EQ
    name: str = ""                # NIFTY | SENSEX
    strike: float = 0.0
    expiry: str = ""              # YYYY-MM-DD
    exchange_token: str = ""


class InstrumentMaster:
    """Today's tradable universe. The only source of lot sizes and strikes."""

    def __init__(self, instruments: list[Instrument], fetched_at: datetime):
        self.fetched_at = fetched_at
        self._all = instruments
        self._by_symbol = {i.trading_symbol: i for i in instruments}

    def __len__(self) -> int:
        return len(self._all)

    def get(self, trading_symbol: str) -> Instrument | None:
        return self._by_symbol.get(trading_symbol)

    def resolve_token(self, trading_symbol: str) -> str:
        inst = self.get(trading_symbol)
        if not inst or not inst.exchange_token:
            raise KeyError(f"No exchange token found for {trading_symbol}")
        return inst.exchange_token

    def lot_size(self, trading_symbol: str) -> int:
        inst = self._by_symbol.get(trading_symbol)
        if inst is None or inst.lot_size <= 0:
            raise KeyError(f"lot size unknown for {trading_symbol!r} — refusing to guess")
        return inst.lot_size

    def options(self, name: str, expiry: str | None = None) -> list[Instrument]:
        out = [
            i for i in self._all
            if i.name.upper() == name.upper() and i.instrument_type in ("CE", "PE")
        ]
        if expiry:
            out = [i for i in out if i.expiry == expiry]
        return out

    def expiries(self, name: str) -> list[str]:
        return sorted({i.expiry for i in self.options(name) if i.expiry})

    def nearest_expiry(self, name: str, on_or_after: str) -> str | None:
        return next((e for e in self.expiries(name) if e >= on_or_after), None)

    def strikes(self, name: str, expiry: str) -> list[float]:
        return sorted({i.strike for i in self.options(name, expiry) if i.strike > 0})

    def find_option(self, name: str, expiry: str, strike: float, side: str) -> Instrument | None:
        side = side.upper()
        for i in self.options(name, expiry):
            if i.instrument_type == side and abs(i.strike - strike) < 1e-6:
                return i
        return None


class Subscription(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class BrokerAdapter(Protocol):
    """Design doc §3.1. Implementations: GrowwAdapter, SimAdapter."""

    name: str

    # --- execution ---
    def place_order(self, o: OrderRequest) -> OrderId: ...
    def modify_order(self, oid: OrderId, o: OrderPatch) -> None: ...
    def cancel_order(self, oid: OrderId) -> None: ...
    def square_off_all(self) -> list[OrderId]: ...

    # --- state ---
    def get_orders(self) -> list[Order]: ...
    def get_positions(self) -> list[Position]: ...
    def get_funds(self) -> Funds: ...

    # --- data ---
    def get_instruments(self) -> InstrumentMaster: ...
    def get_quote(self, keys: list[str]) -> dict[str, Quote]: ...
    def get_candles(self, key: str, tf: str, span: int) -> list[Candle]: ...

    # --- streams ---
    def stream_ticks(self, keys: list[str], on_tick: Callable[[Quote], None]) -> Subscription: ...
    def stream_orders(self, on_order_event: Callable[[Order], None]) -> Subscription: ...


class BrokerError(RuntimeError):
    """Any adapter failure. Carries the broker's verbatim reason (§8 edge handling)."""

    def __init__(self, message: str, *, retryable: bool = False, raw: Any = None):
        super().__init__(message)
        self.retryable = retryable
        self.raw = raw

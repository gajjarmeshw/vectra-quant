"""SENTINEL's universe is FNO index options on NSE/BSE.

One Groww account also holds CASH, CURRENCY and COMMODITY (MCX). Reading the book
unscoped meant a commodity trade the trader took on their own could be counted
against the day's option-trade budget — and, far worse, market-exited by
square_off_all, which is exactly the "never override what I placed" rule.
"""
import threading

from sentinel.brokers.groww import GrowwAdapter, _in_scope


class FakeApi:
    """Returns a mixed-segment book, as an unscoped server call would."""

    ORDERS = [
        {"groww_order_id": "o-fno", "trading_symbol": "NIFTY26808241850CE",
         "segment": "FNO", "exchange": "NSE", "transaction_type": "BUY",
         "quantity": 65, "filled_quantity": 65, "order_status": "EXECUTED"},
        {"groww_order_id": "o-mcx", "trading_symbol": "GOLDM26AUGFUT",
         "segment": "COMMODITY", "exchange": "MCX", "transaction_type": "BUY",
         "quantity": 1, "filled_quantity": 1, "order_status": "EXECUTED"},
        {"groww_order_id": "o-cash", "trading_symbol": "RELIANCE",
         "segment": "CASH", "exchange": "NSE", "transaction_type": "BUY",
         "quantity": 10, "filled_quantity": 10, "order_status": "EXECUTED"},
    ]
    POSITIONS = [
        {"trading_symbol": "NIFTY26808241850CE", "quantity": 65, "segment": "FNO",
         "exchange": "NSE", "average_price": 141.0, "last_price": 160.0},
        {"trading_symbol": "GOLDM26AUGFUT", "quantity": 1, "segment": "COMMODITY",
         "exchange": "MCX", "average_price": 71000.0, "last_price": 71500.0},
    ]

    def get_order_list(self, **kw):
        self.orders_segment = kw.get("segment")
        return {"payload": {"order_list": self.ORDERS}}

    def get_positions_for_user(self, **kw):
        self.positions_segment = kw.get("segment")
        return {"payload": {"positions": self.POSITIONS}}


def adapter() -> tuple[GrowwAdapter, FakeApi]:
    a = GrowwAdapter.__new__(GrowwAdapter)   # no auth, no network
    api = FakeApi()
    a._api = api
    a._auth_lock = threading.RLock()
    a._token_at = float("inf")               # never looks stale -> never re-auths
    a._token_ttl_s = float("inf")
    a._call = lambda _name, fn, **kw: fn(**kw)
    a._lookup = lambda _sym: None
    return a, api


def test_scope_predicate():
    assert _in_scope({"segment": "FNO", "exchange": "NSE"})
    assert _in_scope({"segment": "FNO", "exchange": "BSE"})
    assert not _in_scope({"segment": "COMMODITY", "exchange": "MCX"})
    assert not _in_scope({"segment": "CASH", "exchange": "NSE"})
    assert not _in_scope({"segment": "FNO", "exchange": "MCX"})
    # A payload that omits the fields is not excluded — the instrument-master
    # lookup downstream is the second gate.
    assert _in_scope({"trading_symbol": "NIFTY26808241850CE"})


def test_order_book_is_fno_only():
    a, api = adapter()
    orders = a.get_orders()
    assert [o.order_id for o in orders] == ["o-fno"]
    assert api.orders_segment == "FNO"        # the server is asked to scope too


def test_positions_exclude_other_segments():
    a, api = adapter()
    ps = a.get_positions()
    assert [p.trading_symbol for p in ps] == ["NIFTY26808241850CE"]
    assert api.positions_segment == "FNO"


def test_square_off_never_touches_a_commodity_position():
    """The regression that matters: a floor breach must not sell the trader's gold."""
    a, _ = adapter()
    placed = []
    a.cancel_resting_stops = lambda **kw: []
    a.place_order = lambda req: (placed.append(req), "oid")[1]
    a._own_refs = set()

    a.square_off_all()
    assert [r.trading_symbol for r in placed] == ["NIFTY26808241850CE"]

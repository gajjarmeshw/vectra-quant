"""Indian options cost model. Design doc §9 — "model them from day one".

At r=Rs.1,200 a round trip near Rs.50-60 is ~5% of the risk budget, so costs are not
a rounding error; they decide whether an edge is real. Every rate is a config value
because statutory charges change (STT on option sales moved to 0.1% in Oct 2024).

Charged per LEG, then summed for the round trip:
  brokerage        flat per executed order
  STT              sell side only, on premium turnover
  exchange txn     both sides, exchange-specific
  SEBI turnover    both sides
  stamp duty       buy side only
  GST              18% on (brokerage + exchange txn + SEBI)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostRates:
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.001          # 0.1% of sell premium turnover
    exchange_txn_pct_nse: float = 0.0003503
    exchange_txn_pct_bse: float = 0.000325
    sebi_turnover_pct: float = 0.000001  # Rs.10 per crore
    gst_pct: float = 0.18
    stamp_duty_buy_pct: float = 0.00003  # Rs.300 per crore, buy side only


# NSE equity-derivatives FUTURES rates (post the 2023 STT hike) — materially
# cheaper than options per rupee of turnover, which is exactly why the
# credit-spread wrapper was found to be destroying a real signal edge (see
# backtest/research.py): 0.02% futures STT vs 0.1% options STT is a 5x
# difference before anything else is counted.
FUTURES_COST_RATES = CostRates(
    brokerage_per_order=20.0,
    stt_sell_pct=0.0002,           # 0.02% of sell-side turnover (futures, not premium)
    exchange_txn_pct_nse=0.0000173,  # ~Rs.1.73 per lakh, NSE futures
    exchange_txn_pct_bse=0.0000375,
    sebi_turnover_pct=0.000001,
    gst_pct=0.18,
    stamp_duty_buy_pct=0.00002,    # 0.002% of buy-side turnover (futures)
)


@dataclass(frozen=True)
class LegCost:
    turnover: float
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return round(
            self.brokerage + self.stt + self.exchange_txn + self.sebi + self.stamp + self.gst, 2
        )


def leg_cost(
    price: float,
    quantity: int,
    side: str,
    *,
    exchange: str = "NSE",
    rates: CostRates | None = None,
) -> LegCost:
    """Cost of one executed leg. `quantity` is absolute units (lots x lot_size)."""
    r = rates or CostRates()
    turnover = max(0.0, float(price) * int(quantity))
    is_sell = side.upper() == "SELL"

    brokerage = r.brokerage_per_order if quantity > 0 else 0.0
    stt = turnover * r.stt_sell_pct if is_sell else 0.0
    txn_pct = r.exchange_txn_pct_bse if exchange.upper() == "BSE" else r.exchange_txn_pct_nse
    exchange_txn = turnover * txn_pct
    sebi = turnover * r.sebi_turnover_pct
    stamp = 0.0 if is_sell else turnover * r.stamp_duty_buy_pct
    gst = (brokerage + exchange_txn + sebi) * r.gst_pct

    return LegCost(
        turnover=round(turnover, 2),
        brokerage=round(brokerage, 2),
        stt=round(stt, 2),
        exchange_txn=round(exchange_txn, 4),
        sebi=round(sebi, 4),
        stamp=round(stamp, 4),
        gst=round(gst, 4),
    )


def round_trip_cost(
    entry_price: float,
    exit_price: float,
    quantity: int,
    *,
    exchange: str = "NSE",
    rates: CostRates | None = None,
) -> float:
    """Total cost of a buy-then-sell option round trip, in rupees."""
    buy = leg_cost(entry_price, quantity, "BUY", exchange=exchange, rates=rates)
    sell = leg_cost(exit_price, quantity, "SELL", exchange=exchange, rates=rates)
    return round(buy.total + sell.total, 2)


def net_pnl(
    entry_price: float,
    exit_price: float,
    quantity: int,
    *,
    exchange: str = "NSE",
    rates: CostRates | None = None,
) -> tuple[float, float, float]:
    """(gross, costs, net) for a long option round trip."""
    gross = round((float(exit_price) - float(entry_price)) * int(quantity), 2)
    costs = round_trip_cost(entry_price, exit_price, quantity, exchange=exchange, rates=rates)
    return gross, costs, round(gross - costs, 2)

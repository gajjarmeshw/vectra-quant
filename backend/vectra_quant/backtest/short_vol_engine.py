"""Intraday short-volatility engine: sell the ATM straddle at the open, buy it
back before the close, same session. No overnight exposure.

WHY THIS NEEDS ITS OWN ENGINE. `BacktestEngine` drives a candle loop and a
single directional position per day; this strategy is a two-leg option
structure priced off the real option chain, with its own entry/exit clocks and
per-leg costs. `WeeklySpreadEngine` solved the same mismatch for the multi-day
credit spread -- this is the intraday analogue.

WHAT THE RESEARCH FOUND (see scripts/backtest_gamma_scalp_multiyear.py).
Measured over 396 sessions from 2024-01 on real chain data:

    mean +Rs193/day/lot, median +Rs786, win 62.1%, Sharpe 0.83
    worst day -Rs23,245, max drawdown -Rs51,290
    2024 -Rs30,246 | 2025 +Rs33,379 | 2026 +Rs73,270

The edge is the variance risk premium: implied vol is priced above what the
session actually realises about 74% of the time, and the seller collects that
difference. It is a REAL but MODEST edge with a fat left tail -- one losing
year in three, and a drawdown roughly equal to a year of profit.

COSTS ARE MEASURED, NOT ASSUMED. `DEFAULT_COST_PER_LEG` comes from the real
captured depth book (2026-09-18): ATM half-spread ~0.14 index points (~Rs9 at
lot 65) plus brokerage, STT on the sold premium, exchange charges and GST.
An earlier Rs100/leg guess made this strategy look unprofitable; measuring it
flipped the sign. NOTE the measurement is from ONE calm session -- spreads
widen in stressed markets, exactly when the large losses occur, so realised
tail costs are worse than modelled here.

RISK THIS ENGINE DOES NOT MODEL: a naked short straddle has unbounded loss.
The sample begins after the March-2020 crash, so the worst tail event of the
modern era is absent from these statistics. Size positions accordingly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from vectra_quant.backtest import iea_data
from vectra_quant.logging_setup import get

log = get("backtest.short_vol")

NIFTY_LOT_SIZE = 65
STRIKE_STEP = 50.0

# Measured from the real captured depth book, not assumed -- see module docstring.
DEFAULT_COST_PER_LEG = 40.0
DEFAULT_ENTRY_TIME = "09:20:00"
DEFAULT_EXIT_TIME = "15:15:00"


@dataclass
class ShortVolTrade:
    date: str
    expiry: str
    dte: int
    strike: float
    spot_entry: float
    spot_exit: float
    ce_entry: float
    pe_entry: float
    ce_exit: float
    pe_exit: float
    credit: float
    buyback: float
    gross_pnl: float
    costs: float
    net_pnl: float
    win: bool
    entry_iv: float
    exit_reason: str = "SESSION_CLOSE"

    @property
    def spot_move(self) -> float:
        return self.spot_exit - self.spot_entry


@dataclass
class ShortVolResult:
    instrument: str
    start_date: str
    end_date: str
    trades: list[ShortVolTrade] = field(default_factory=list)
    skipped_days: int = 0
    data_source: str = "IEA_OPTION_CHAIN"

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.win)

    @property
    def losses(self) -> int:
        return self.total_trades - self.wins

    @property
    def win_pct(self) -> float:
        return 100.0 * self.wins / self.total_trades if self.total_trades else 0.0

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def total_costs(self) -> float:
        return sum(t.costs for t in self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def profit_factor(self) -> float:
        gains = sum(t.net_pnl for t in self.trades if t.net_pnl > 0)
        pains = -sum(t.net_pnl for t in self.trades if t.net_pnl < 0)
        return gains / pains if pains > 0 else 0.0

    @property
    def max_drawdown(self) -> float:
        """Peak-to-trough of the cumulative net P&L curve (negative number)."""
        peak, worst, run = 0.0, 0.0, 0.0
        for t in self.trades:
            run += t.net_pnl
            peak = max(peak, run)
            worst = min(worst, run - peak)
        return worst

    @property
    def worst_day(self) -> float:
        return min((t.net_pnl for t in self.trades), default=0.0)

    @property
    def best_day(self) -> float:
        return max((t.net_pnl for t in self.trades), default=0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument, "start_date": self.start_date, "end_date": self.end_date,
            "total_trades": self.total_trades, "skipped_days": self.skipped_days,
            "wins": self.wins, "losses": self.losses, "win_pct": round(self.win_pct, 1),
            "profit_factor": round(self.profit_factor, 2), "gross_pnl": round(self.gross_pnl, 2),
            "total_costs": round(self.total_costs, 2), "net_pnl": round(self.net_pnl, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "worst_day": round(self.worst_day, 2), "best_day": round(self.best_day, 2),
            "data_source": self.data_source,
        }


class ShortVolEngine:
    """One trade per session: sell ATM straddle at `entry_time`, close at `exit_time`."""

    def run(
        self,
        instrument: str = "NIFTY",
        from_date: str | None = None,
        to_date: str | None = None,
        lots: int = 1,
        cost_per_leg: float = DEFAULT_COST_PER_LEG,
        entry_time: str = DEFAULT_ENTRY_TIME,
        exit_time: str = DEFAULT_EXIT_TIME,
        min_dte: int = 0,
        lot_size: int = NIFTY_LOT_SIZE,
    ) -> ShortVolResult:
        """`min_dte` skips sessions too close to expiry. DTE=0 measured WORST in
        research (-Rs907/day, 41.7% win) because expiry-day gamma is violent, but
        the per-DTE buckets are small and noisy, so the default (0) keeps every
        session rather than baking in a filter fitted on thin data."""
        sessions = sorted(iea_data.load_index_window(instrument, from_date, to_date).keys())
        result = ShortVolResult(
            instrument=instrument,
            start_date=sessions[0] if sessions else (from_date or ""),
            end_date=sessions[-1] if sessions else (to_date or ""),
        )
        if not sessions:
            log.warning("short_vol: no sessions for %s %s..%s", instrument, from_date, to_date)
            return result

        for date_str in sessions:
            trade = self._run_session(
                instrument, date_str, lots, cost_per_leg,
                entry_time, exit_time, min_dte, lot_size,
            )
            if trade is None:
                result.skipped_days += 1
            else:
                result.trades.append(trade)
        return result

    def _run_session(
        self, instrument: str, date_str: str, lots: int, cost_per_leg: float,
        entry_time: str, exit_time: str, min_dte: int, lot_size: int,
    ) -> ShortVolTrade | None:
        expiry = iea_data.nearest_expiry_on_or_after(date_str)
        if not expiry:
            return None
        dte = (datetime.strptime(expiry, "%Y-%m-%d") - datetime.strptime(date_str, "%Y-%m-%d")).days
        if dte < min_dte:
            return None

        idx = iea_data.load_option_day_index(instrument, date_str)
        day = iea_data.load_option_day(instrument, date_str)
        if idx is None or day is None or day.empty:
            return None

        entry_at = datetime.strptime(f"{date_str} {entry_time}", "%Y-%m-%d %H:%M:%S")
        exit_at = datetime.strptime(f"{date_str} {exit_time}", "%Y-%m-%d %H:%M:%S")

        spot_entry = self._spot_at(day, entry_at)
        spot_exit = self._spot_at(day, exit_at)
        if not spot_entry or not spot_exit:
            return None
        strike = round(spot_entry / STRIKE_STEP) * STRIKE_STEP

        ce_in = idx.nearest_bar(strike, "CE", entry_at)
        pe_in = idx.nearest_bar(strike, "PE", entry_at)
        ce_out = idx.nearest_bar(strike, "CE", exit_at)
        pe_out = idx.nearest_bar(strike, "PE", exit_at)
        if not all((ce_in, pe_in, ce_out, pe_out)):
            return None

        ce_entry, pe_entry = float(ce_in["close"]), float(pe_in["close"])
        ce_exit, pe_exit = float(ce_out["close"]), float(pe_out["close"])
        if min(ce_entry, pe_entry) <= 0:
            return None

        qty = lots * lot_size
        credit = (ce_entry + pe_entry) * qty
        buyback = (ce_exit + pe_exit) * qty
        gross = credit - buyback                       # short: profit when premium decays
        costs = 4 * cost_per_leg * lots                # 2 legs, sold then bought back
        net = gross - costs

        entry_iv = float(((ce_in.get("iv") or 0.0) + (pe_in.get("iv") or 0.0)) / 2.0)
        return ShortVolTrade(
            date=date_str, expiry=expiry, dte=dte, strike=float(strike),
            spot_entry=spot_entry, spot_exit=spot_exit,
            ce_entry=ce_entry, pe_entry=pe_entry, ce_exit=ce_exit, pe_exit=pe_exit,
            credit=round(credit, 2), buyback=round(buyback, 2),
            gross_pnl=round(gross, 2), costs=round(costs, 2), net_pnl=round(net, 2),
            win=net > 0, entry_iv=round(entry_iv, 2),
        )

    @staticmethod
    def _spot_at(day, at: datetime) -> float | None:
        """Chain-implied spot nearest a timestamp; the chain carries a real
        `spot` column per bar, so no proxy is needed."""
        if "spot" not in day.columns or day.empty:
            return None
        pos = (day["ts"] - at).abs().idxmin()
        val = float(day.loc[pos, "spot"])
        return val if val > 0 else None

"""Futures execution path — for a signal whose option-spread wrapper destroys
more edge than it captures.

Found by hand while investigating Renko (see backtest/research.py): in 2025,
box=75 measured +18.0 underlying points/signal of genuine edge, and the
credit-spread wrapper still turned that into -Rs669 net. STT alone is 5x
cheaper on futures than on options premium (0.02% vs 0.1%), and a single
futures leg replaces 2-4 option legs of slippage and spread-crossing cost.

**This is a model, not real futures ticks.** The archive holds no expired-
futures history (Dhan does not serve it) — only whichever 3 contracts were
live during the backfill window. So entries/exits are priced directly off
the underlying INDEX with a slippage allowance, and `data_source` always
says `INDEX_PROXY`. `measure_futures_basis` uses those 3 real contracts to
quantify exactly how good that proxy is — never assume it, measure it.
"""
from __future__ import annotations

import statistics as st
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any, Callable

from vectra_quant.backtest import iea_data
from vectra_quant.backtest.engine import (
    INDEX_LOT_SIZES,
    BacktestCancelled,
    ProgressFn,
    _PushSignalCollector,
    load_candles_for_backtest,
)
from vectra_quant.brokers.costs import FUTURES_COST_RATES, net_pnl
from vectra_quant.risk_engine import RiskConfig, RiskEngine, TradeResult

# NIFTY futures are the most liquid derivative on the exchange — far tighter
# than a 2-4 leg option spread's spread-crossing cost (which used 0.8-1.2 pts
# per LEG). This is still a modelling assumption, not measured tick data.
FUTURES_SLIPPAGE_PTS = 0.25

DEFAULT_MAX_LOTS = 20


def size_position(risk_budget: float, stop_distance_pts: float, lot_size: int, max_lots: int = DEFAULT_MAX_LOTS) -> int:
    """lots = floor(risk_budget / (stop_distance_pts * lot_size)), floored at 1 lot, capped at max_lots.

    This is the position-sizing rule the plan calls for — capital only
    matters once there's a rule connecting it to size. Before this, `lots`
    was hardcoded to 1 regardless of capital or stop distance.
    """
    if stop_distance_pts <= 0 or lot_size <= 0 or risk_budget <= 0:
        return 0
    lots = int(risk_budget // (stop_distance_pts * lot_size))
    return max(1, min(lots, max_lots))


@dataclass
class FuturesTrade:
    id: str
    symbol: str
    direction: str  # LONG | SHORT
    opened_at: str
    closed_at: str
    lots: int
    qty: int
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str
    win: bool


@dataclass
class FuturesBacktestResult:
    instrument: str
    strategy_name: str
    start_date: str
    end_date: str
    days: int
    total_trades: int
    wins: int
    losses: int
    win_pct: float
    profit_factor: float
    final_pnl: float
    gross_pnl: float
    total_costs: float
    max_drawdown: float
    trades: list[FuturesTrade] = field(default_factory=list)
    data_source: str = "INDEX_PROXY"
    data_warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument, "strategy_name": self.strategy_name,
            "start_date": self.start_date, "end_date": self.end_date, "days": self.days,
            "total_trades": self.total_trades, "wins": self.wins, "losses": self.losses,
            "win_pct": round(self.win_pct, 1),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor else 0.0,
            "final_pnl": round(self.final_pnl, 2), "gross_pnl": round(self.gross_pnl, 2),
            "total_costs": round(self.total_costs, 2), "max_drawdown": round(self.max_drawdown, 2),
            "data_source": self.data_source, "data_warning": self.data_warning,
            "trades": [vars(t) for t in self.trades],
        }


class FuturesBacktestEngine:
    """Drives a push-mode strategy's own entry/exit signals (unchanged) through
    a futures P&L model instead of the options credit-spread wrapper."""

    def __init__(self, risk_cfg: RiskConfig | None = None):
        self.risk_cfg = risk_cfg or RiskConfig()

    def run_strategy(
        self,
        strategy,
        instrument: str = "NIFTY",
        from_date: str | None = None,
        to_date: str | None = None,
        allow_multiday: bool = False,
        max_lots: int = DEFAULT_MAX_LOTS,
        progress: ProgressFn | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> FuturesBacktestResult:
        strat = strategy
        if not getattr(strat, "USES_PUSH_SIGNALS", False):
            raise ValueError(
                f"{getattr(strat, 'name', strat)} does not emit push signals — "
                "the futures path needs on_candle-driven entries/exits, same as Renko."
            )

        inst = instrument.upper()
        candles_data, data_src_note, warning = load_candles_for_backtest(instrument=inst, from_date=from_date, to_date=to_date)
        lot_size = INDEX_LOT_SIZES.get(inst, 65)

        collector = _PushSignalCollector()
        strat._emit_signal = collector

        trades: list[FuturesTrade] = []
        total_realized = total_gross = total_costs = 0.0
        wins = losses = 0
        peak_pnl = max_drawdown = 0.0

        in_trade = False
        trade_dir = ""
        trade_symbol = f"{inst}-FUT"
        trade_open_ts = ""
        trade_entry_price = 0.0
        trade_lots = 0

        risk_engine = RiskEngine(self.risk_cfg, kill_switch_on=True)

        sorted_dates = sorted(candles_data.keys())
        total_sessions = len(sorted_dates)
        for session_num, date_str in enumerate(sorted_dates, start=1):
            if cancel_check and cancel_check():
                raise BacktestCancelled()
            if progress:
                progress("FUTURES_SIM", 0, 1, session_num, total_sessions, date_str)

            bars = candles_data[date_str]
            if len(bars) < 30:
                continue

            # Day-scoped risk bookkeeping (trade cap, day P&L, LOCKED/PROTECT
            # state) resets every session regardless of `allow_multiday` — those
            # are daily concepts by definition. What `allow_multiday` actually
            # controls is narrower: whether an OPEN POSITION gets force-closed
            # at 15:15. Conflating the two here originally meant a multi-day
            # run's trade cap and day_pnl floor were never cleared after day 1,
            # so the very first loss could (and did, in testing) permanently
            # LOCK the risk engine for the rest of the backtest.
            risk_engine.reset_day()

            for bar in bars:
                try:
                    bar_dt = datetime.strptime(bar.timestamp, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    bar_dt = datetime.strptime(bar.timestamp, "%Y-%m-%d %H:%M:00")
                bar_time = bar_dt.time()

                collector.pending = None
                strat.on_candle(inst, "1m", bar)
                pending = collector.pending

                force_exit = (not allow_multiday) and bar_time >= time(15, 15)
                strategy_exit = bool(pending and pending.get("is_exit"))

                if in_trade and (strategy_exit or force_exit):
                    exit_price = round(bar.close - FUTURES_SLIPPAGE_PTS if trade_dir == "LONG" else bar.close + FUTURES_SLIPPAGE_PTS, 2)
                    qty = trade_lots * lot_size
                    # net_pnl(buy_price, sell_price, ...) assigns STT/stamp to the
                    # correct side regardless of direction — for a SHORT, the
                    # "sell" happened at entry and the "buy to cover" at exit.
                    buy_px, sell_px = (trade_entry_price, exit_price) if trade_dir == "LONG" else (exit_price, trade_entry_price)
                    gross, costs, net = net_pnl(buy_px, sell_px, qty, rates=FUTURES_COST_RATES)

                    reason = (pending["thesis"] if strategy_exit and pending.get("thesis") else "STRATEGY_EXIT") if strategy_exit else "TIME_EXIT (15:15 IST)"

                    trades.append(FuturesTrade(
                        id=str(uuid.uuid4())[:8], symbol=trade_symbol, direction=trade_dir,
                        opened_at=trade_open_ts, closed_at=f"{date_str} {bar_time.strftime('%H:%M')}",
                        lots=trade_lots, qty=qty, entry_price=trade_entry_price, exit_price=exit_price,
                        gross_pnl=round(gross, 2), costs=round(costs, 2), net_pnl=round(net, 2),
                        exit_reason=reason, win=net > 0,
                    ))
                    total_realized += net
                    total_gross += gross
                    total_costs += costs
                    if net > 0:
                        wins += 1
                    else:
                        losses += 1
                    peak_pnl = max(peak_pnl, total_realized)
                    max_drawdown = max(max_drawdown, peak_pnl - total_realized)
                    risk_engine.on_trade_opened()
                    risk_engine.on_trade_closed(TradeResult(pnl=net))
                    in_trade = False

                elif (not in_trade) and pending and not pending.get("is_exit") and time(9, 20) <= bar_time <= time(14, 30):
                    ctx = getattr(strat, "active_trade_context", None)
                    if ctx is None:
                        continue
                    decision = risk_engine.can_enter(bar_dt, confidence=75)
                    if not decision.allowed:
                        # Strategy already registered this internally (on_candle ran
                        # before this gate) — undo it so it isn't stuck forever
                        # waiting to exit a position that never opened. Same fix
                        # applied in the options engine for the identical desync.
                        strat.active_trade_context = None
                        continue

                    stop_distance = abs(float(ctx.entry_underlying) - float(ctx.stop_underlying))
                    legs = pending.get("legs") or []
                    leg_dir = legs[0].get("direction") if legs else "PE"
                    trade_dir = "LONG" if leg_dir == "PE" else "SHORT"
                    trade_lots = size_position(self.risk_cfg.risk_per_trade, stop_distance, lot_size, max_lots)
                    if trade_lots <= 0:
                        strat.active_trade_context = None
                        continue
                    trade_entry_price = round(bar.close + FUTURES_SLIPPAGE_PTS if trade_dir == "LONG" else bar.close - FUTURES_SLIPPAGE_PTS, 2)
                    trade_open_ts = f"{date_str} {bar_time.strftime('%H:%M')}"
                    in_trade = True

        win_pct = (wins / len(trades) * 100.0) if trades else 0.0
        gross_wins = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        gross_losses = abs(sum(t.net_pnl for t in trades if t.net_pnl < 0))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (gross_wins if gross_wins else 0.0)

        return FuturesBacktestResult(
            instrument=inst, strategy_name=getattr(strat, "name", ""),
            start_date=sorted_dates[0] if sorted_dates else "", end_date=sorted_dates[-1] if sorted_dates else "",
            days=len(sorted_dates), total_trades=len(trades), wins=wins, losses=losses, win_pct=win_pct,
            profit_factor=profit_factor, final_pnl=round(total_realized, 2), gross_pnl=round(total_gross, 2),
            total_costs=round(total_costs, 2), max_drawdown=round(max_drawdown, 2), trades=trades,
            data_source=f"INDEX_PROXY (slippage {FUTURES_SLIPPAGE_PTS}pt, from {data_src_note})",
            data_warning=warning,
        )


@dataclass
class BasisStats:
    contract: str
    n_minutes: int
    mean_basis: float
    stdev_basis: float
    max_abs_basis: float
    mean_abs_basis: float
    mean_intraday_drift: float  # avg(max-min basis within a session) — see note below
    max_intraday_drift: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract, "n_minutes": self.n_minutes,
            "mean_basis": self.mean_basis, "stdev_basis": self.stdev_basis,
            "max_abs_basis": self.max_abs_basis, "mean_abs_basis": self.mean_abs_basis,
            "mean_intraday_drift": self.mean_intraday_drift, "max_intraday_drift": self.max_intraday_drift,
        }


def measure_futures_basis(contract_symbol: str, instrument: str = "NIFTY",
                           from_date: str | None = None, to_date: str | None = None) -> BasisStats | None:
    """How far the real futures contract traded from the index, minute by
    minute, over the contract's real (live, not expired) history.

    This is the only honesty check available for the index-proxy model: the
    archive has no expired-futures ticks, so we can't validate the proxy
    against the PAST — but these 3 live contracts show what a realistic basis
    looks like right now, and that number bounds how much error the proxy
    model can be hiding.
    """
    fut_by_day = iea_data.load_futures_contract_window(contract_symbol, from_date, to_date)
    idx_by_day = iea_data.load_index_window(instrument, from_date=from_date, to_date=to_date)
    if not fut_by_day or not idx_by_day:
        return None

    idx_by_ts: dict[str, float] = {}
    for bars in idx_by_day.values():
        for b in bars:
            idx_by_ts[b.timestamp] = b.close

    diffs: list[float] = []
    intraday_drifts: list[float] = []
    for bars in fut_by_day.values():
        day_diffs = []
        for b in bars:
            idx_px = idx_by_ts.get(b.timestamp)
            if idx_px is not None:
                d = b.close - idx_px
                diffs.append(d)
                day_diffs.append(d)
        if len(day_diffs) > 1:
            intraday_drifts.append(max(day_diffs) - min(day_diffs))

    if not diffs:
        return None

    # Basis is a slowly-varying cost-of-carry premium (grows with time to
    # expiry), NOT noise around zero — mean_basis for a 2-month-out contract
    # can be 300+ points. That premium roughly cancels over a round trip; what
    # actually bounds the index-proxy model's error for a held position is how
    # much the basis itself MOVES during the hold, hence `intraday_drift`.
    return BasisStats(
        contract=contract_symbol, n_minutes=len(diffs),
        mean_basis=round(st.mean(diffs), 2), stdev_basis=round(st.pstdev(diffs), 2) if len(diffs) > 1 else 0.0,
        max_abs_basis=round(max(abs(d) for d in diffs), 2), mean_abs_basis=round(st.mean(abs(d) for d in diffs), 2),
        mean_intraday_drift=round(st.mean(intraday_drifts), 2) if intraday_drifts else 0.0,
        max_intraday_drift=round(max(intraday_drifts), 2) if intraday_drifts else 0.0,
    )

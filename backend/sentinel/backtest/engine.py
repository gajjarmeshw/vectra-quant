"""Historical Backtesting & Candle Replay Engine.

Replays minute-by-minute candles for NIFTY, BANKNIFTY, SENSEX, or FINNIFTY through
CandleBuilder, Breakout Technical Strategy, and RiskEngine.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, time, UTC
from typing import Any

from sentinel.brokers.costs import net_pnl
from sentinel.data.candles import Candle, CandleBuilder
from sentinel.risk_engine import DayState, RiskConfig, RiskEngine, TradeResult


INDEX_LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
    "FINNIFTY": 40,
}

DEFAULT_SL_POINTS = {
    "NIFTY": 12.0,
    "BANKNIFTY": 30.0,
    "SENSEX": 35.0,
    "FINNIFTY": 20.0,
}


@dataclass
class BacktestTrade:
    id: str
    symbol: str
    instrument: str
    direction: str
    opened_at: str
    closed_at: str
    qty: int
    entry_price: float
    exit_price: float
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str
    win: bool


@dataclass
class BacktestResult:
    instrument: str
    days: int
    start_date: str
    end_date: str
    initial_capital: float
    final_pnl: float
    gross_pnl: float
    total_costs: float
    total_trades: int
    wins: int
    losses: int
    win_pct: float
    profit_factor: float
    max_drawdown: float
    trades: list[BacktestTrade] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "days": self.days,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_capital": self.initial_capital,
            "final_pnl": round(self.final_pnl, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "total_costs": round(self.total_costs, 2),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_pct": round(self.win_pct, 1),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor else 0.0,
            "max_drawdown": round(self.max_drawdown, 2),
            "trades": [asdict(t) for t in self.trades],
        }


def generate_synthetic_candles(
    instrument: str = "NIFTY",
    days: int = 5,
    start_base_price: float = 24500.0,
) -> dict[str, list[Candle]]:
    """Generate realistic 1-minute OHLCV candles for backtesting across N trading days."""
    import random
    rng = random.Random(42)

    base_px = start_base_price if instrument.upper() != "SENSEX" else 80500.0
    if instrument.upper() == "BANKNIFTY":
        base_px = 52000.0
    elif instrument.upper() == "FINNIFTY":
        base_px = 23000.0

    daily_candles: dict[str, list[Candle]] = {}
    current_date = datetime.now(UTC).date() - timedelta(days=days)

    for d in range(days):
        session_dt = current_date + timedelta(days=d)
        if session_dt.weekday() >= 5:  # Skip weekends
            continue

        date_str = session_dt.strftime("%Y-%m-%d")
        bars: list[Candle] = []

        px = base_px * (1.0 + rng.uniform(-0.005, 0.005))
        trend = rng.choice([-1.0, 1.0])

        for m in range(375):
            minute_time = time(9, 15)
            m_dt = datetime.combine(session_dt, minute_time) + timedelta(minutes=m)

            volatility = base_px * 0.0008
            change = trend * (volatility * 0.3) + rng.gauss(0, volatility)
            open_px = px
            close_px = max(100.0, open_px + change)
            high_px = max(open_px, close_px) + abs(rng.gauss(0, volatility * 0.5))
            low_px = min(open_px, close_px) - abs(rng.gauss(0, volatility * 0.5))

            bars.append(
                Candle(
                    symbol=instrument,
                    timestamp=m_dt.strftime("%Y-%m-%d %H:%M:00"),
                    open=round(open_px, 2),
                    high=round(high_px, 2),
                    low=round(low_px, 2),
                    close=round(close_px, 2),
                    volume=float(int(rng.uniform(500, 5000))),
                )
            )
            px = close_px

        base_px = px
        daily_candles[date_str] = bars

    return daily_candles


class BacktestEngine:
    def __init__(self, risk_cfg: RiskConfig | None = None):
        self.risk_cfg = risk_cfg or RiskConfig(
            capital=15000.0,
            target=2500.0,
            loss_limit=1500.0,
            risk_per_trade=1200.0,
        )

    def run(
        self,
        instrument: str = "NIFTY",
        days: int = 5,
        candles_by_day: dict[str, list[Candle]] | None = None,
    ) -> BacktestResult:
        inst = instrument.upper()
        candles_data = candles_by_day or generate_synthetic_candles(inst, days=days)

        trades: list[BacktestTrade] = []
        total_realized = 0.0
        total_gross = 0.0
        total_costs = 0.0
        wins = 0
        losses = 0
        peak_pnl = 0.0
        max_drawdown = 0.0

        sl_pts = DEFAULT_SL_POINTS.get(inst, 15.0)
        lot_size = INDEX_LOT_SIZES.get(inst, 65)

        for date_str, bars in candles_data.items():
            if len(bars) < 30:
                continue

            engine = RiskEngine(self.risk_cfg, kill_switch_on=True)
            cb = CandleBuilder()

            open_range_high = max(b.high for b in bars[:20])
            open_range_low = min(b.low for b in bars[:20])

            in_trade = False
            trade_entry_px = 0.0
            trade_dir = ""
            trade_open_ts = ""
            trade_sl = 0.0
            trade_target = 0.0
            trade_option_entry = 150.0

            for bar in bars:
                cb.on_tick(inst, bar.close)
                try:
                    bar_dt = datetime.strptime(bar.timestamp, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    bar_dt = datetime.now(UTC)
                bar_time = bar_dt.time()

                if in_trade:
                    hit_sl = (trade_dir == "CE" and bar.low <= trade_sl) or (trade_dir == "PE" and bar.high >= trade_sl)
                    hit_target = (trade_dir == "CE" and bar.high >= trade_target) or (trade_dir == "PE" and bar.low <= trade_target)
                    time_exit = bar_time >= time(15, 0)

                    if hit_sl or hit_target or time_exit:
                        if hit_target:
                            option_exit = trade_option_entry * 1.4
                            reason = "target"
                        elif hit_sl:
                            option_exit = max(5.0, trade_option_entry - sl_pts)
                            reason = "stop_loss"
                        else:
                            option_exit = trade_option_entry * (1.1 if bar.close > trade_entry_px else 0.9)
                            reason = "session_close"

                        gross, costs, net = net_pnl(trade_option_entry, option_exit, lot_size, exchange="NSE")
                        is_win = net > 0

                        trades.append(
                            BacktestTrade(
                                id=str(uuid.uuid4())[:8],
                                symbol=f"{inst}_{trade_dir}",
                                instrument=inst,
                                direction=trade_dir,
                                opened_at=trade_open_ts,
                                closed_at=bar_time.strftime("%H:%M"),
                                qty=lot_size,
                                entry_price=round(trade_option_entry, 2),
                                exit_price=round(option_exit, 2),
                                gross_pnl=round(gross, 2),
                                costs=round(costs, 2),
                                net_pnl=round(net, 2),
                                exit_reason=reason,
                                win=is_win,
                            )
                        )

                        total_realized += net
                        total_gross += gross
                        total_costs += costs
                        if is_win:
                            wins += 1
                        else:
                            losses += 1

                        peak_pnl = max(peak_pnl, total_realized)
                        drawdown = peak_pnl - total_realized
                        max_drawdown = max(max_drawdown, drawdown)

                        engine.on_trade_opened()
                        engine.on_trade_closed(TradeResult(pnl=net))
                        in_trade = False

                elif time(9, 35) <= bar_time <= time(14, 30) and engine.state not in (DayState.LOCKED, DayState.PROTECT):
                    decision = engine.can_enter(bar_dt, confidence=75)
                    if decision.allowed:
                        if bar.close > open_range_high:
                            in_trade = True
                            trade_dir = "CE"
                            trade_entry_px = bar.close
                            trade_open_ts = bar_time.strftime("%H:%M")
                            trade_sl = bar.close - (sl_pts * 1.5)
                            trade_target = bar.close + (sl_pts * 3.0)
                            trade_option_entry = 150.0
                        elif bar.close < open_range_low:
                            in_trade = True
                            trade_dir = "PE"
                            trade_entry_px = bar.close
                            trade_open_ts = bar_time.strftime("%H:%M")
                            trade_sl = bar.close + (sl_pts * 1.5)
                            trade_target = bar.close - (sl_pts * 3.0)
                            trade_option_entry = 150.0

        dates_sorted = sorted(candles_data.keys())
        start_d = dates_sorted[0] if dates_sorted else ""
        end_d = dates_sorted[-1] if dates_sorted else ""

        win_pct = (wins / len(trades) * 100.0) if trades else 0.0
        gross_wins = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        gross_losses = abs(sum(t.net_pnl for t in trades if t.net_pnl < 0))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (gross_wins if gross_wins else 1.0)

        return BacktestResult(
            instrument=inst,
            days=days,
            start_date=start_d,
            end_date=end_d,
            initial_capital=self.risk_cfg.capital,
            final_pnl=total_realized,
            gross_pnl=total_gross,
            total_costs=total_costs,
            total_trades=len(trades),
            wins=wins,
            losses=losses,
            win_pct=win_pct,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            trades=trades,
        )

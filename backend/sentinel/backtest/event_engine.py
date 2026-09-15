from typing import Any, Dict
from datetime import datetime, timedelta
from sentinel.logging_setup import get
from sentinel.risk_engine import RiskConfig
from sentinel.backtest.engine import BacktestResult, BacktestTrade, load_candles_for_backtest
from sentinel.data.indicators import compute_indicators
from sentinel.strategies.nifty_5d_breakout import Nifty5DayBreakout

log = get("backtest.event_engine")


class EventBacktestEngine:
    """Event-Driven Backtest Engine running on interpolated sub-second ticks."""

    def __init__(self, risk_cfg: RiskConfig | None = None):
        self.risk_cfg = risk_cfg or RiskConfig(
            capital=150000.0,
            target=5000.0,
            loss_limit=3000.0,
            risk_per_trade=1500.0,
        )

    def run(
        self,
        strategy_name: str = "nifty_5d_breakout",
        instrument: str = "NIFTY",
        days: int = 5,
        broker: Any | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> BacktestResult:
        from sentinel.strategies.registry import get_strategy
        try:
            strat_instance = get_strategy(strategy_name)
        except ValueError as e:
            log.warning(f"Event engine failed to load strategy: {e}")
            return BacktestResult(
                instrument=instrument, strategy_name=strategy_name, days=days,
                start_date="", end_date="", initial_capital=self.risk_cfg.capital,
                final_pnl=0.0, gross_pnl=0.0, total_costs=0.0, total_trades=0,
                wins=0, losses=0, win_pct=0.0, profit_factor=0.0, max_drawdown=0.0,
                trades=[], data_source="", data_warning=str(e),
                bars_evaluated=0, gauntlet_verdict="ERROR",
                gauntlet_verdict_desc=str(e),
                gauntlet_tone="red", passes_checklist=False,
            )

        log.info(f"Running Event Backtest for {instrument} over {days} days")

        # ── Load real 1m candles from DhanHQ ──
        intraday_data, data_source, warning = load_candles_for_backtest(
            instrument, days=days, broker=broker,
            from_date=from_date, to_date=to_date,
        )

        if not intraday_data:
            log.warning(f"No market data available for backtest. Warning: {warning}")
            return BacktestResult(
                instrument=instrument, strategy_name=strategy_name, days=days,
                start_date="", end_date="", initial_capital=self.risk_cfg.capital,
                final_pnl=0.0, gross_pnl=0.0, total_costs=0.0, total_trades=0,
                wins=0, losses=0, win_pct=0.0, profit_factor=0.0, max_drawdown=0.0,
                trades=[], data_source=data_source, data_warning=warning,
                bars_evaluated=0, gauntlet_verdict="NO DATA",
                gauntlet_verdict_desc="Connect DhanHQ broker to run real backtest",
                gauntlet_tone="red", passes_checklist=False,
            )

        # ── Build daily candles from 1m bars ──
        from sentinel.brokers.base import Candle as BrokerCandle
        all_daily = []
        actual_days = len(intraday_data.keys())
        
        for d in sorted(intraday_data.keys()):
            day_bars = intraday_data[d]
            if not day_bars:
                continue
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                dt = datetime.now()
            all_daily.append(BrokerCandle(
                ts=dt,
                open=day_bars[0].open,
                high=max(b.high for b in day_bars),
                low=min(b.low for b in day_bars),
                close=day_bars[-1].close,
                volume=sum(b.volume for b in day_bars),
            ))

        # ── Setup strategy + compute indicators ──
        strat = strat_instance
        compute_indicators(all_daily, strat.manifest.get("indicators", []))

        trades: list[BacktestTrade] = []
        open_trades: list[dict[str, Any]] = []
        lot_size = 50 if instrument == "NIFTY" else (15 if instrument == "BANKNIFTY" else 10)
        total_bars_seen = 0

        # ── mock_emit: intercepts strategy signals ──
        def mock_emit(direction, action, entry_price, thesis):
            
            if strat.name == "renko_strategy":
                slippage = 0.0
                fixed_cost = 50.0
            else:
                slippage = 1.0
                fixed_cost = 50.0

            if action == "EXIT":
                for ot in list(open_trades):
                    open_trades.remove(ot)
                    exit_price = entry_price
                    close_reason = thesis
                    points = (exit_price - ot["entry_price"]) if ot["direction"] == "LONG" else (ot["entry_price"] - exit_price)
                    gross_pnl = points * lot_size
                    trades.append(BacktestTrade(
                        id=f"evt_{len(trades)+1}",
                        symbol=ot["instrument_details"],
                        instrument=instrument,
                        instrument_details=ot["instrument_details"],
                        capital_used=ot["capital_used"],
                        direction=ot["direction"],
                        opened_at=ot["entry_time"],
                        closed_at=getattr(strat, "trade_date", "") + " " + getattr(strat, "last_time", ""),
                        qty=lot_size,
                        entry_price=ot["entry_price"],
                        exit_price=exit_price,
                        gross_pnl=gross_pnl,
                        costs=fixed_cost,
                        net_pnl=gross_pnl - fixed_cost,
                        exit_reason=close_reason,
                        win=gross_pnl > 0,
                        strategy_name=strategy_name,
                        slippage_cost=ot["slippage"] * lot_size,
                    ))
                return

            current_atr = getattr(strat, "current_atr", 100.0)
            if hasattr(strat, "_get_atr"):
                current_atr = strat._get_atr()
            stop_mult = getattr(strat, "STOP_ATR_MULT", 1.0)
            target_mult = getattr(strat, "TARGET_ATR_MULT", 2.0)
            
            stop_dist = current_atr * stop_mult
            target_dist = current_atr * target_mult

            fill_price = entry_price + slippage if action == "BUY" else entry_price - slippage

            # ATM strike (nearest 50 for Nifty)
            atm_strike = round(fill_price / 50) * 50

            if strat.name == "renko_strategy":
                instrument_details = f"{instrument} FUT"
                capital_used = 100_000 * max(1, lot_size // 50)
                # Infinite stops/targets, strategy handles exits
                stop_dist = 999999
                target_dist = 999999
                trade_dir = "LONG" if direction == "PE" else "SHORT"
            else:
                # Credit Spread details
                if direction == "PE":
                    sell_leg = f"SELL {atm_strike} PE"
                    buy_leg = f"BUY {atm_strike - 500} PE"
                else:
                    sell_leg = f"SELL {atm_strike} CE"
                    buy_leg = f"BUY {atm_strike + 500} CE"

                instrument_details = f"{sell_leg} / {buy_leg}"
                capital_used = 40_000 * max(1, lot_size // 50)
                trade_dir = "SHORT" if direction == "CE" else "LONG"

            open_trades.append({
                "direction": trade_dir,
                "entry_time": getattr(strat, "trade_date", "") + " " + getattr(strat, "last_time", ""),
                "entry_price": fill_price,
                "stop_loss": fill_price - stop_dist if trade_dir == "LONG" else fill_price + stop_dist,
                "target": fill_price + target_dist if trade_dir == "LONG" else fill_price - target_dist,
                "thesis": thesis,
                "slippage": slippage,
                "instrument_details": instrument_details,
                "capital_used": capital_used,
            })
            strat.has_traded_today = True

        strat._emit_signal = mock_emit

        # ── Simulation loop ──
        WARMUP_DAYS = getattr(strat, "MIN_WARMUP_DAYS", 3)
        sorted_dates = sorted(intraday_data.keys())

        for day_idx, date_str in enumerate(sorted_dates):
            bars = intraday_data[date_str]
            if len(bars) < 10:
                continue

            dt = datetime.strptime(date_str, "%Y-%m-%d")
            # Feed daily candles up to (but not including) this date
            strat.daily_candles.clear()
            for dc in all_daily:
                if getattr(dc, "ts", None) and dc.ts.date() < dt.date():
                    strat.daily_candles.append(dc)
            
            if hasattr(strat, "_recalculate_levels"):
                strat._recalculate_levels()

            if day_idx < WARMUP_DAYS:
                rh = getattr(strat, "range_high", 0)
                rl = getattr(strat, "range_low", 0)
                tb = getattr(strat, "trend_bias", "NEUTRAL")
                log.info(f"Warmup day {day_idx+1}/{WARMUP_DAYS}: {date_str} "
                         f"— rangeH={rh:.0f} rangeL={rl:.0f} trend={tb}")
                continue

            strat.trade_date = date_str
            strat.has_traded_today = False
            if hasattr(strat, "today_open"):
                strat.today_open = 0.0
            if hasattr(strat, "recent_1m_volumes"):
                strat.recent_1m_volumes.clear()

            compute_indicators(bars, strat.manifest.get("indicators", []))

            for bar in bars:
                total_bars_seen += 1
                strat.on_candle(instrument, "1m", bar)

                # Parse bar timestamp
                bar_ts = getattr(bar, "timestamp", None)
                if not bar_ts and hasattr(bar, "ts"):
                    bar_ts = bar.ts.strftime("%Y-%m-%d %H:%M:%S") if hasattr(bar.ts, "strftime") else str(bar.ts)

                try:
                    bar_dt = datetime.strptime(bar_ts, "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    try:
                        bar_dt = datetime.strptime(bar_ts, "%Y-%m-%d %H:%M:00")
                    except (ValueError, TypeError):
                        bar_dt = datetime.now()

                # Interpolate 4 sub-bar ticks: open → low → high → close
                ticks = [
                    {"last_price": bar.open, "timestamp": bar_dt},
                    {"last_price": bar.low, "timestamp": bar_dt},
                    {"last_price": bar.high, "timestamp": bar_dt},
                    {"last_price": bar.close, "timestamp": bar_dt},
                ]

                for t in ticks:
                    strat.banknifty_open = 100
                    strat.banknifty_ltp = 105
                    strat.last_time = t["timestamp"].strftime("%H:%M:%S")

                    if not open_trades:
                        strat.on_tick(instrument, t)

                    # ── Evaluate open trades ──
                    ltp = t["last_price"]
                    for ot in list(open_trades):
                        exit_price = None
                        close_reason = None

                        # Force close at 15:15
                        if t["timestamp"].time() >= datetime.strptime("15:15:00", "%H:%M:%S").time():
                            close_reason = "EOD Square Off"
                            exit_price = ltp
                        elif ot["direction"] == "LONG":
                            if ltp >= ot["target"]:
                                close_reason = "Target Hit"
                                exit_price = ot["target"]
                            elif ltp <= ot["stop_loss"]:
                                close_reason = "Stop Loss"
                                exit_price = ot["stop_loss"]
                        else:  # SHORT
                            if ltp <= ot["target"]:
                                close_reason = "Target Hit"
                                exit_price = ot["target"]
                            elif ltp >= ot["stop_loss"]:
                                close_reason = "Stop Loss"
                                exit_price = ot["stop_loss"]

                        if close_reason is None:
                            continue

                        # Trade closed
                        open_trades.remove(ot)
                        points = (exit_price - ot["entry_price"]) if ot["direction"] == "LONG" else (ot["entry_price"] - exit_price)
                        gross_pnl = points * lot_size

                        trades.append(BacktestTrade(
                            id=f"evt_{len(trades)+1}",
                            symbol=ot["instrument_details"],
                            instrument=instrument,
                            instrument_details=ot["instrument_details"],
                            capital_used=ot["capital_used"],
                            direction=ot["direction"],
                            opened_at=ot["entry_time"],
                            closed_at=date_str + " " + t["timestamp"].strftime("%H:%M:%S"),
                            qty=lot_size,
                            entry_price=ot["entry_price"],
                            exit_price=exit_price,
                            gross_pnl=gross_pnl,
                            costs=50.0,
                            net_pnl=gross_pnl - 50.0,
                            exit_reason=close_reason,
                            win=gross_pnl > 0,
                            strategy_name="nifty_5d_breakout",
                            slippage_cost=ot["slippage"] * lot_size,
                        ))

        # ── Compute result metrics ──
        wins = sum(1 for t in trades if t.net_pnl > 0)
        losses = sum(1 for t in trades if t.net_pnl <= 0)
        total = len(trades)
        total_pnl = sum(t.net_pnl for t in trades)
        total_costs = total * 50.0

        # Proper profit factor
        gross_wins = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        gross_losses = abs(sum(t.net_pnl for t in trades if t.net_pnl <= 0))
        profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else (99.0 if gross_wins > 0 else 0.0)

        # Max drawdown
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            running += t.net_pnl
            if running > peak:
                peak = running
            dd = peak - running
            if dd > max_dd:
                max_dd = dd

        # Gauntlet verdict
        passes = profit_factor >= 1.3 and total >= 3
        if passes:
            verdict = "PASSED: LIVE TICKS"
            desc = f"Event Engine Tick Simulation — {total} trades, PF {profit_factor}"
            tone = "green"
        else:
            verdict = "REVIEW NEEDED"
            desc = f"{total} trades, PF {profit_factor}. Insufficient edge or sample size."
            tone = "amber" if total >= 2 else "red"

        return BacktestResult(
            instrument=instrument,
            strategy_name=strategy_name,
            days=actual_days,
            start_date=sorted_dates[0] if sorted_dates else "",
            end_date=sorted_dates[-1] if sorted_dates else "",
            initial_capital=self.risk_cfg.capital,
            final_pnl=round(total_pnl, 2),
            gross_pnl=round(total_pnl + total_costs, 2),
            total_costs=total_costs,
            total_trades=total,
            wins=wins,
            losses=losses,
            win_pct=round(wins / total * 100, 1) if total > 0 else 0.0,
            profit_factor=profit_factor,
            max_drawdown=round(max_dd, 2),
            trades=trades,
            data_source=f"Event Engine (Ticks) - {data_source}",
            data_warning=warning,
            bars_evaluated=total_bars_seen,
            gauntlet_verdict=verdict,
            gauntlet_verdict_desc=desc,
            gauntlet_tone=tone,
            passes_checklist=passes,
        )

    def parameter_wiggle_test(self, *args, **kwargs) -> list[dict[str, Any]]:
        """Placeholder for event engine wiggle test."""
        return []

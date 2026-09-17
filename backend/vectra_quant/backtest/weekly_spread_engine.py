"""Multi-day weekly credit-spread backtest engine.

Models the structure reverse-engineered from the user's own trade log +
source videos (backtest_trades_corrected.xlsx): every position is a single,
same-week-expiry, exactly-200-point-wide NIFTY vertical credit spread,
entered once a week and held across multiple sessions to (or near) expiry.

This is a SEPARATE engine from `engine.py` on purpose: `engine.py`'s day
loop resets `in_trade` every session (it force-closes intraday), so it
cannot model a position that survives from Wednesday to the following
Tuesday. `futures_engine.py` solved the same multi-day problem for futures;
this does the options equivalent, using the real per-day option chain
(`iea_data.load_option_day_index`) for every day of the hold, not just entry.

Direction is decided independently of any price/EMA signal, per instruction:
real open-interest positioning (put-call OI ratio) at entry. PCR = total PE
OI / total CE OI over strikes within `pcr_band_pct` of spot on the entry day.
PCR above `pcr_bullish_above` -> heavy put writing -> bullish -> sell a bull
put spread. PCR below `pcr_bearish_below` -> heavy call writing -> bearish
-> sell a bear call spread. Inside the band -> no clear positioning -> skip
the week (matches the source data's occasional skipped weeks).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from vectra_quant.backtest import iea_data
from vectra_quant.brokers.costs import CostRates, leg_cost

SPREAD_WIDTH_PTS = 200.0
NIFTY_LOT_SIZE = 65
NIFTY_STRIKE_STEP = 50.0


@dataclass
class WeeklySpreadTrade:
    entry_date: str
    exit_date: str
    right: str              # "C" or "P"
    sell_strike: float
    buy_strike: float
    pcr_at_entry: float
    sell_entry: float
    buy_entry: float
    sell_exit: float
    buy_exit: float
    net_credit: float
    max_risk: float
    gross_pnl: float
    costs: float
    net_pnl: float
    exit_reason: str
    win: bool
    hold_days: int


@dataclass
class WeeklySpreadResult:
    instrument: str
    start_date: str
    end_date: str
    total_trades: int
    skipped_weeks: int
    wins: int
    losses: int
    win_pct: float
    profit_factor: float
    gross_pnl: float
    total_costs: float
    net_pnl: float
    max_drawdown: float
    trades: list[WeeklySpreadTrade] = field(default_factory=list)
    data_source: str = "IEA_REAL_OPTION_CHAIN"

    def as_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument, "start_date": self.start_date, "end_date": self.end_date,
            "total_trades": self.total_trades, "skipped_weeks": self.skipped_weeks,
            "wins": self.wins, "losses": self.losses, "win_pct": round(self.win_pct, 1),
            "profit_factor": round(self.profit_factor, 2), "gross_pnl": round(self.gross_pnl, 2),
            "total_costs": round(self.total_costs, 2), "net_pnl": round(self.net_pnl, 2),
            "max_drawdown": round(self.max_drawdown, 2), "data_source": self.data_source,
        }


def _pcr_at(underlying: str, date_str: str, spot: float, band_pct: float) -> float | None:
    df = iea_data.load_option_day(underlying, date_str)
    if df is None or df.empty:
        return None
    at = datetime.strptime(f"{date_str} 09:20:00", "%Y-%m-%d %H:%M:%S")
    snap = iea_data.oi_snapshot(df, at)
    lo, hi = spot * (1 - band_pct), spot * (1 + band_pct)
    ce_oi = sum(v["ce_oi"] for k, v in snap.items() if lo <= k <= hi)
    pe_oi = sum(v["pe_oi"] for k, v in snap.items() if lo <= k <= hi)
    if ce_oi <= 0:
        return None
    return pe_oi / ce_oi


def _iv_skew_at(opt_index, spot: float, otm_pct: float, at: datetime) -> float | None:
    """put_iv - call_iv for the OTM call/put roughly `otm_pct` away from spot.

    Standard equity-index skew is negative-sloped (OTM puts richer than OTM
    calls) most of the time -- this returns the RAW skew (usually positive
    here since put_iv > call_iv), so a higher value means relatively more
    put-side hedging demand priced in that day.
    """
    call_strike = round((spot * (1 + otm_pct)) / NIFTY_STRIKE_STEP) * NIFTY_STRIKE_STEP
    put_strike = round((spot * (1 - otm_pct)) / NIFTY_STRIKE_STEP) * NIFTY_STRIKE_STEP
    call_bar = opt_index.nearest_bar(call_strike, "C", at)
    put_bar = opt_index.nearest_bar(put_strike, "P", at)
    if call_bar is None or put_bar is None:
        return None
    call_iv, put_iv = call_bar.get("iv"), put_bar.get("iv")
    if not call_iv or not put_iv or call_iv <= 0 or put_iv <= 0:
        return None
    return put_iv - call_iv


def _leg_price(opt_index, strike: float, right: str, at: datetime) -> float | None:
    bar = opt_index.nearest_bar(strike, right, at)
    return float(bar["close"]) if bar is not None else None


def _leg_volume(opt_index, strike: float, right: str, at: datetime) -> float | None:
    bar = opt_index.nearest_bar(strike, right, at)
    return float(bar["volume"]) if bar is not None else None


class WeeklySpreadEngine:
    def __init__(self, cost_rates: CostRates | None = None, lot_size: int = NIFTY_LOT_SIZE):
        self.rates = cost_rates or CostRates()
        self.lot_size = lot_size

    def run(
        self,
        instrument: str = "NIFTY",
        from_date: str = "2024-01-01",
        to_date: str = "2026-09-14",
        entry_weekday: int = 2,          # Monday=0 ... Wednesday=2
        direction_mode: str = "pcr",     # "pcr" | "iv_skew_trend" | "iv_skew_contrarian"
        pcr_band_pct: float = 0.03,
        pcr_bullish_above: float = 1.05,
        pcr_bearish_below: float = 0.95,
        iv_skew_otm_pct: float = 0.02,
        iv_skew_high: float = 4.17,        # top quintile of observed skew (calibrated on 2024-2026 real chain)
        iv_skew_low: float = 1.64,         # bottom quintile of observed skew
        max_hold_days: int = 7,
        stop_loss_mult_of_credit: float | None = None,   # None matches the disclosure: no stop on the structure itself
        futures_target_pts_early: float = 40.0,   # exit once the underlying moves this far in our favour from entry
        futures_target_pts_late: float = 70.0,    # widened target once `futures_target_widen_from_day` is reached
        futures_target_widen_from_day: int = 5,
        moneyness_offset_pct: float = 0.0,       # sell strike ITM depth as a fraction of spot (0 = ATM, per disclosure)
        min_entry_volume: float = 0.0,           # skip the trade if either entry leg's day volume is below this
    ) -> WeeklySpreadResult:
        index_data = iea_data.load_index_window(instrument, from_date, to_date)
        all_days = sorted(index_data.keys())
        if not all_days:
            return WeeklySpreadResult(instrument, from_date, to_date, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

        entry_days = [d for d in all_days if datetime.strptime(d, "%Y-%m-%d").weekday() == entry_weekday]

        trades: list[WeeklySpreadTrade] = []
        skipped = 0
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        last_exit_date = ""

        for entry_date in entry_days:
            if entry_date <= last_exit_date:
                continue  # don't overlap with a position still open from the prior week

            day_candles = index_data.get(entry_date)
            if not day_candles or len(day_candles) < 5:
                continue
            spot_entry = day_candles[min(4, len(day_candles) - 1)].close  # ~09:20 candle

            opt_index_entry = iea_data.load_option_day_index(instrument, entry_date)
            if opt_index_entry is None:
                continue

            at_entry_signal = datetime.strptime(f"{entry_date} 09:20:00", "%Y-%m-%d %H:%M:%S")
            pcr = None
            skew = None
            if direction_mode == "pcr":
                pcr = _pcr_at(instrument, entry_date, spot_entry, pcr_band_pct)
                if pcr is None:
                    continue
                if pcr >= pcr_bullish_above:
                    right, direction_bias = "P", "BULLISH"
                elif pcr <= pcr_bearish_below:
                    right, direction_bias = "C", "BEARISH"
                else:
                    skipped += 1
                    continue
            elif direction_mode in ("iv_skew_trend", "iv_skew_contrarian"):
                skew = _iv_skew_at(opt_index_entry, spot_entry, iv_skew_otm_pct, at_entry_signal)
                if skew is None:
                    continue
                elevated_put_demand = skew >= iv_skew_high
                elevated_call_demand = skew <= iv_skew_low
                if not elevated_put_demand and not elevated_call_demand:
                    skipped += 1
                    continue
                if direction_mode == "iv_skew_trend":
                    # trend-following: heavy put hedging -> expect the fear is
                    # warranted -> lean bearish; heavy call demand -> bullish.
                    right, direction_bias = ("C", "BEARISH") if elevated_put_demand else ("P", "BULLISH")
                else:
                    # contrarian: extreme put-side fear often marks capitulation
                    # -> bullish; extreme call-side greed -> bearish.
                    right, direction_bias = ("P", "BULLISH") if elevated_put_demand else ("C", "BEARISH")
            else:
                raise ValueError(f"unknown direction_mode: {direction_mode}")

            # moneyness_offset_pct > 0 pushes the sold strike into-the-money by
            # that fraction of spot -- deep ITM strikes have delta near 1, so
            # the spread behaves like a leveraged directional bet (closer to
            # the source video's actual strikes) rather than a theta-harvest.
            itm_shift = spot_entry * moneyness_offset_pct
            if right == "C":
                sell_strike = round((spot_entry - itm_shift) / NIFTY_STRIKE_STEP) * NIFTY_STRIKE_STEP
                buy_strike = sell_strike + SPREAD_WIDTH_PTS
            else:
                sell_strike = round((spot_entry + itm_shift) / NIFTY_STRIKE_STEP) * NIFTY_STRIKE_STEP
                buy_strike = sell_strike - SPREAD_WIDTH_PTS

            at_entry = datetime.strptime(f"{entry_date} 09:20:00", "%Y-%m-%d %H:%M:%S")
            sell_entry = _leg_price(opt_index_entry, sell_strike, right, at_entry)
            buy_entry = _leg_price(opt_index_entry, buy_strike, right, at_entry)
            if sell_entry is None or buy_entry is None or sell_entry <= 0:
                continue

            if min_entry_volume > 0:
                sv = _leg_volume(opt_index_entry, sell_strike, right, at_entry) or 0.0
                bv = _leg_volume(opt_index_entry, buy_strike, right, at_entry) or 0.0
                if sv < min_entry_volume or bv < min_entry_volume:
                    continue  # too thin to trust a realistic fill at this strike

            net_credit = (sell_entry - buy_entry) * self.lot_size
            max_risk = max(0.0, (SPREAD_WIDTH_PTS * self.lot_size) - net_credit)
            if net_credit <= 0:
                continue  # not actually a credit spread at these strikes -- skip rather than mis-model

            # Pin the contract's own expiry at entry. `load_option_day_index`
            # resolves whichever expiry is nearest-on-or-after a given date,
            # so once a hold crosses past THIS contract's real expiry, the
            # same strike number on a later date silently resolves to a
            # DIFFERENT (next) week's chain -- a different contract entirely,
            # not the position we're holding. Capping the hold window at the
            # pinned expiry is what a real trader is forced to do anyway
            # (the contract stops existing), and it's what prevents mixing
            # two different expiries' prices into one trade's P&L.
            session_expiry = iea_data.nearest_expiry_on_or_after(entry_date)
            entry_idx = all_days.index(entry_date)
            hold_window = [
                d for d in all_days[entry_idx + 1: entry_idx + 1 + max_hold_days]
                if session_expiry is None or d <= session_expiry
            ]

            exit_date, exit_reason = entry_date, "SAME_DAY_NO_MORE_DATA"
            sell_exit = buy_exit = None
            hold_days_used = 0

            for hd, day in enumerate(hold_window, start=1):
                opt_index_day = iea_data.load_option_day_index(instrument, day)
                if opt_index_day is None:
                    continue
                at_check = datetime.strptime(f"{day} 15:15:00", "%Y-%m-%d %H:%M:%S")
                s_px = _leg_price(opt_index_day, sell_strike, right, at_check)
                b_px = _leg_price(opt_index_day, buy_strike, right, at_check)
                if s_px is None or b_px is None:
                    continue
                hold_days_used = hd
                exit_date, sell_exit, buy_exit = day, s_px, b_px

                if stop_loss_mult_of_credit is not None:
                    # cost to close now = (s_px - b_px) * lot; compare against credit banked at entry
                    cost_to_close = (s_px - b_px) * self.lot_size
                    pnl_if_closed_now = net_credit - cost_to_close
                    if pnl_if_closed_now <= -stop_loss_mult_of_credit * abs(net_credit):
                        exit_reason = "STOP_LOSS"
                        break

                # Linked-futures monitor: track the underlying's move from
                # entry in the trade's favour and exit once it reaches the
                # weekly target -- the disclosed product has NO stop on the
                # option structure itself (SL/Target 0/0); this target,
                # widened late in the hold, is its only non-clock exit.
                day_candles = index_data.get(day)
                if day_candles:
                    spot_now = day_candles[-1].close
                    favourable_move = (spot_now - spot_entry) if right == "P" else (spot_entry - spot_now)
                    target = futures_target_pts_late if hd >= futures_target_widen_from_day else futures_target_pts_early
                    if favourable_move >= target:
                        exit_reason = "FUTURES_TARGET"
                        break

                if hd >= max_hold_days:
                    exit_reason = "MAX_HOLD"
                    break
            else:
                exit_reason = "EXPIRY_OR_END_OF_WINDOW"

            if sell_exit is None or buy_exit is None:
                continue  # never found a valid exit price anywhere in the hold window

            sell_leg_cost = leg_cost(sell_entry, self.lot_size, "SELL", rates=self.rates).total \
                + leg_cost(buy_exit, self.lot_size, "BUY", rates=self.rates).total
            buy_leg_cost = leg_cost(buy_entry, self.lot_size, "BUY", rates=self.rates).total \
                + leg_cost(sell_exit, self.lot_size, "SELL", rates=self.rates).total
            costs = round(sell_leg_cost + buy_leg_cost, 2)

            gross = round(net_credit - (sell_exit - buy_exit) * self.lot_size, 2)
            net = round(gross - costs, 2)

            equity += net
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
            last_exit_date = exit_date

            trades.append(WeeklySpreadTrade(
                entry_date=entry_date, exit_date=exit_date, right=right,
                sell_strike=sell_strike, buy_strike=buy_strike,
                pcr_at_entry=round(pcr if pcr is not None else (skew if skew is not None else 0.0), 3),
                sell_entry=sell_entry, buy_entry=buy_entry, sell_exit=sell_exit, buy_exit=buy_exit,
                net_credit=round(net_credit, 2), max_risk=round(max_risk, 2),
                gross_pnl=gross, costs=costs, net_pnl=net, exit_reason=exit_reason,
                win=net > 0, hold_days=hold_days_used,
            ))

        wins = sum(1 for t in trades if t.win)
        losses = len(trades) - wins
        gross_total = sum(t.gross_pnl for t in trades)
        costs_total = sum(t.costs for t in trades)
        net_total = sum(t.net_pnl for t in trades)
        gross_wins = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        gross_losses = abs(sum(t.net_pnl for t in trades if t.net_pnl < 0))
        pf = (gross_wins / gross_losses) if gross_losses > 0 else (gross_wins if gross_wins else 0.0)

        return WeeklySpreadResult(
            instrument=instrument, start_date=from_date, end_date=to_date,
            total_trades=len(trades), skipped_weeks=skipped, wins=wins, losses=losses,
            win_pct=(wins / len(trades) * 100.0) if trades else 0.0, profit_factor=pf,
            gross_pnl=round(gross_total, 2), total_costs=round(costs_total, 2), net_pnl=round(net_total, 2),
            max_drawdown=round(max_dd, 2), trades=trades,
        )

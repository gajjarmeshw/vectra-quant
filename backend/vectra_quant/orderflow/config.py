"""All [cfg] thresholds for the order-flow daily strategy in one place.

Every number here is a guess at a reasonable starting point, NOT a
calibrated value — there is no historical depth data to calibrate against.
Calibration can only happen by recording live data and walk-forward
testing on it (Phase 2). Nothing in this file should be read as "the
strategy's known-good settings."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time


@dataclass(frozen=True)
class OrderFlowCfg:
    # --- schedule ---
    entry_window_start: time = time(10, 30)
    entry_window_end: time = time(14, 30)
    area_cutoff_time: time = time(14, 30)   # afternoon cutoff for the signed-area decision
    force_exit_time: time = time(15, 10)
    safety_sweep_time: time = time(15, 20)

    # --- big-order filter (Chart 1) ---
    big_order_percentile: float = 95.0      # top-5% of this instrument's own historical level sizes
    big_order_min_samples: int = 500        # minimum observations before the filter activates at all

    # --- Chart 2 interval ---
    imbalance_interval_seconds: int = 60    # [cfg, e.g. 1 min]

    # --- direction decision ---
    theta_cross: float = 5.0                # |Chart2| early-fire threshold — GUESS, needs calibration
    eps_neutral: float = 0.5                # |x| < eps -> NEUTRAL — GUESS, needs calibration

    # --- low-VIX contrarian filter ---
    vix_filter_enabled: bool = True
    theta_vix: float = 14.0                 # "mid-teens" per spec — GUESS
    theta_conv: float = 2.0                 # weak-read threshold on |Chart2| — GUESS

    # --- structure ---
    wing_width_pts: float = 200.0
    strike_step: float = 50.0

    # --- daily-cadence variant (this project's own extension, not in the
    # disclosed weekly product): trade every day rather than only Wednesday,
    # and hold for a much shorter window so paper-trading gets a result
    # every session instead of once a week. ---
    daily_mode: bool = True
    daily_max_hold_days: int = 1
    daily_always_enter: bool = True         # take the default-neutral direction rather than skip

    # --- futures monitor / exit ---
    futures_target_pts_early: float = 40.0    # GUESS — widened late in the hold per the spec
    futures_target_pts_late: float = 70.0
    futures_target_widen_from_day: int = 5    # days into the hold before the wider target applies

    # --- recorder ---
    recorder_start_time: time = time(9, 0)
    recorder_stop_time: time = time(15, 35)
    reconnect_backoff_seconds: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)
    ws_ping_timeout_seconds: float = 40.0     # server closes after 40s without a response
    max_depth_connections: int = 5            # budget: >5 sockets disconnects the oldest (code 805)

    extra: dict[str, float] = field(default_factory=dict)  # escape hatch for anything added later


DEFAULT_CFG = OrderFlowCfg()


@dataclass(frozen=True)
class ThunderboltCfg:
    """hedged121 ("Nifty Thunderbolt") -- intraday 1x2 ratio backspread.

    Every threshold is a labeled GUESS; the disclosure withholds all real
    theta values as proprietary. Nothing here is calibrated -- there is no
    historical order-book data to calibrate against (same constraint as
    OrderFlowCfg). This is a genuinely different signal family from the
    weekly hedged131 strategy: it reads the raw per-interval imbalance
    series itself, not a cumulative sum.
    """
    # --- schedule ---
    signal_window_start: time = time(9, 16)
    # The disclosure's own window is 09:16-12:00; extended through the
    # afternoon (to 10 minutes before force_exit_time) per operator request
    # so an afternoon-forming crossing/breakout can still be paper-traded
    # instead of being silently skipped for the day.
    signal_window_end: time = time(15, 0)
    force_exit_time: time = time(15, 10)
    skip_tuesday: bool = True
    vix_skip_at_or_above: float = 22.0   # prior-session VIX >= this -> skip the whole day

    # --- volatility regime classification ---
    regime_low_max: float = 0.6          # avg recent realized vol below this -> LOW
    regime_high_min: float = 1.2         # at/above this -> HIGH; between -> MEDIUM
    regime_lookback_sessions: int = 5
    regime_fallback: str = "HIGH"        # conservative default when data is missing

    # --- MEDIUM/HIGH: crossing detector ---
    theta_cross: float = 0.15            # |ir| crossing level that fires a candidate
    reversal_check_lookback_ok: bool = True  # compare against opposite side's strongest prior reading

    # --- LOW: swing/breakout detector ---
    swing_retracement_pct: float = 0.3   # fraction of the swing's range needed to "confirm" an extreme
    swing_breakout_margin: float = 0.05  # must clear the confirmed extreme by this much
    swing_min_abs_reading: float = 0.1   # breakout candidate must at least reach this magnitude

    # --- filter chain thresholds ---
    opposite_gate_floor: float = 0.1     # opposite side's strength must exceed this to even matter
    opposite_gate_ratio: float = 1.5     # signal must exceed opposite strength by this ratio to survive
    pre_open_lock_threshold: float = 0.3   # a pre-open reading beyond this locks out the opposite direction
    liquidity_reversal_early_threshold: float = 0.3   # "heavy early one-way liquidity"
    liquidity_reversal_push_threshold: float = 0.15   # "meaningful opposite-side push" after open
    over_stretch_bound: float = 0.6      # |reading| at/beyond this at signal time -> vetoed entirely

    # --- structure ---
    long_offset_pts: float = 100.0
    strike_step: float = 50.0
    long_lots: int = 2
    short_lots: int = 1


DEFAULT_THUNDERBOLT_CFG = ThunderboltCfg()


@dataclass(frozen=True)
class BreadthCfg:
    """Cross-sectional order-flow breadth (100 NIFTY100 stocks -> one
    market-wide reading). This is our own signal, not from any disclosure
    -- every number here is a starting guess, not a calibrated value.
    Structure is deliberately the simplest possible (single-leg long
    option), unlike Thunderbolt's disclosed 1x2 backspread, because there
    is no prescribed structure to match and a low-confidence new signal
    should carry the least structural risk while it's being evaluated.
    """
    # --- schedule ---
    signal_window_start: time = time(9, 20)
    signal_window_end: time = time(15, 0)
    force_exit_time: time = time(15, 10)

    # --- breadth aggregation ---
    bucket_seconds: int = 15
    bullish_stock_threshold: float = 0.1   # a stock counts as "bullish" at/above this per-stock imbalance
    bearish_stock_threshold: float = -0.1
    theta_cross: float = 0.10              # mean-breadth crossing threshold -- GUESS, needs calibration
    min_stocks_reporting: int = 20         # don't trust breadth from a thin, mostly-silent snapshot

    # --- structure ---
    strike_step: float = 50.0
    lots: int = 1
    stop_loss_pct_premium: float = 0.30    # exit if premium drops this fraction from entry
    target_pct_premium: float = 0.50       # exit if premium rises this fraction from entry
    max_hold_minutes: int = 15


DEFAULT_BREADTH_CFG = BreadthCfg()

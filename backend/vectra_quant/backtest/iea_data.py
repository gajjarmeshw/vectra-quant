"""Loader for the offline IEA historical archive (Dhan backfill dump).

The archive is a local `IEA_data_*/data/` folder dropped at the repo root
(never committed — see .gitignore). It holds years of real NIFTY market data
that the live broker's on-demand API cannot serve for backtesting:

- `raw/dhan/intraday/i1/NIFTY/*.json.gz` — real NIFTY index 1-minute OHLCV,
  2021-10-01 onward, chunked into ~quarterly files named
  `YYYY-MM-DD_YYYY-MM-DD.json.gz`.
- `store/options/expiry=YYYY-MM-DD/date=YYYY-MM-DD/bars.parquet` — real NIFTY
  option chain 1-minute bars (OHLCV + genuine open interest + IV), covering
  every strike/expiry, floor 2020-08-03.

Only NIFTY is covered by the archive today; other indices fall back to the
existing broker/synthetic paths in `backtest/engine.py`.

Performance notes (see docs/plan for the profiling that drove this):
- `load_index_window` only opens the gz chunks whose filename date range
  overlaps the requested window, instead of parsing the whole archive.
- `OptionDayIndex` builds one small numpy lookup per (strike, right) per day
  so per-bar option lookups are `np.searchsorted` instead of a pandas
  boolean-mask scan over the whole day's ~15,800-row chain.
"""
from __future__ import annotations

import gzip
import json
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from vectra_quant.config import ROOT
from vectra_quant.core.session_clock import IST
from vectra_quant.data.candles import Candle
from vectra_quant.logging_setup import get

log = get("backtest.iea_data")

_SUPPORTED_INDEX_SYMBOLS = {"NIFTY"}
_CHUNK_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json\.gz$")


def _find_data_root() -> Path | None:
    """Locate the archive's `data/` folder: env override, else newest IEA_data_*/data at repo root."""
    override = os.getenv("VECTRA_QUANT_IEA_DATA_DIR")
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    candidates = sorted(ROOT.glob("IEA_data_*/data"), reverse=True)
    return candidates[0] if candidates else None


_DATA_ROOT = _find_data_root()
_INDEX_DIR = (_DATA_ROOT / "raw" / "dhan" / "intraday" / "i1") if _DATA_ROOT else None
_OPTIONS_STORE = (_DATA_ROOT / "store" / "options") if _DATA_ROOT else None

AVAILABLE = _DATA_ROOT is not None


# ---------------------------------------------------------------- index candles

def _chunk_files(symbol: str) -> list[tuple[str, str, Path]]:
    """(chunk_start, chunk_end, path) for every real gz chunk of `symbol`, sorted chronologically."""
    if not _INDEX_DIR:
        return []
    sym_dir = _INDEX_DIR / symbol.upper()
    if not sym_dir.is_dir():
        return []
    out = []
    for p in sorted(sym_dir.glob("*.json.gz")):
        m = _CHUNK_NAME_RE.match(p.name)
        if not m:
            continue  # e.g. macOS "._*" AppleDouble sidecars
        out.append((m.group(1), m.group(2), p))
    return out


@lru_cache(maxsize=32)
def _load_chunk(symbol: str, path_str: str) -> dict[str, list[Candle]]:
    """Parse one gz chunk into {date_str: [Candle,...]}. Timestamps are vectorized (pandas), not per-bar."""
    path = Path(path_str)
    try:
        with gzip.open(path, "rt") as f:
            chunk = json.load(f)
    except Exception:
        log.warning(f"Failed to read IEA archive chunk {path}")
        return {}

    opens = chunk.get("open", [])
    if not opens:
        return {}
    highs = chunk.get("high", [])
    lows = chunk.get("low", [])
    closes = chunk.get("close", [])
    vols = chunk.get("volume", [0.0] * len(opens))
    ts = pd.to_datetime(chunk.get("timestamp", []), unit="s", utc=True).tz_convert(IST)
    date_strs = ts.strftime("%Y-%m-%d").to_numpy()
    time_strs = ts.strftime("%Y-%m-%d %H:%M:00").to_numpy()

    by_day: dict[str, list[Candle]] = {}
    for i in range(len(opens)):
        by_day.setdefault(str(date_strs[i]), []).append(
            Candle(
                symbol=symbol,
                timestamp=str(time_strs[i]),
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(vols[i]),
            )
        )
    for day_bars in by_day.values():
        day_bars.sort(key=lambda c: c.timestamp)
    return by_day


def load_index_window(
    symbol: str,
    from_date: str | None = None,
    to_date: str | None = None,
    tail_sessions: int | None = None,
) -> dict[str, list[Candle]]:
    """Real 1m index candles for a date range, or the last `tail_sessions` sessions if no range given.

    Only opens the gz chunks whose filename range overlaps what's needed —
    parsing the whole ~5-year archive for a 5-day window was the single
    biggest cost in the old loader (measured ~6.5s vs ~0.3s here).
    """
    symbol = symbol.upper()
    if symbol not in _SUPPORTED_INDEX_SYMBOLS:
        return {}
    chunks = _chunk_files(symbol)
    if not chunks:
        return {}

    if from_date or to_date:
        lo = from_date or chunks[0][0]
        hi = to_date or chunks[-1][1]
        selected = [p for (cs, ce, p) in chunks if ce >= lo and cs <= hi]

        by_day: dict[str, list[Candle]] = {}
        for p in selected:
            by_day.update(_load_chunk(symbol, str(p)))
        by_day = {k: v for k, v in by_day.items() if lo <= k <= hi}
        return by_day

    # No explicit range — return the most recent `tail_sessions` real trading
    # sessions. Chunks are ~quarterly but the newest one is only ever
    # partially filled (the backfill is still adding to it), so an estimate
    # like "63 sessions/chunk" undercounts it and stops too early. Instead,
    # parse chunks one at a time from the newest until the actual count is
    # enough — bounded to a couple of chunks for any realistic `days`.
    need = max(tail_sessions or 5, 1)
    by_day: dict[str, list[Candle]] = {}
    for _cs, _ce, p in reversed(chunks):
        by_day.update(_load_chunk(symbol, str(p)))
        if len(by_day) >= need:
            break

    sorted_keys = sorted(by_day.keys())
    chosen = sorted_keys[-need:] if len(sorted_keys) >= need else sorted_keys
    return {k: by_day[k] for k in chosen}


# ---------------------------------------------------------------- option chain

@lru_cache(maxsize=1)
def _available_expiries() -> tuple[str, ...]:
    if not _OPTIONS_STORE or not _OPTIONS_STORE.is_dir():
        return ()
    out = [
        p.name.split("=", 1)[1]
        for p in _OPTIONS_STORE.iterdir()
        if p.is_dir() and p.name.startswith("expiry=")
    ]
    return tuple(sorted(out))


def nearest_expiry_on_or_after(date_str: str) -> str | None:
    """The weekly/monthly expiry a live trader would be positioned in on `date_str`."""
    for e in _available_expiries():
        if e >= date_str:
            return e
    return None


@lru_cache(maxsize=64)
def load_option_day(underlying: str, date_str: str) -> pd.DataFrame | None:
    """Load the full real 1m option chain (OHLCV + OI + IV) for one trading day.

    Uses the nearest weekly/monthly expiry on or after `date_str`, matching
    how a live trader would be positioned in the front-week contract.
    Timestamps are returned as naive IST (matching Candle.timestamp), sorted.
    """
    if not _OPTIONS_STORE:
        return None
    expiry = nearest_expiry_on_or_after(date_str)
    if not expiry:
        return None
    path = _OPTIONS_STORE / f"expiry={expiry}" / f"date={date_str}" / "bars.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception:
        log.warning(f"Failed to read IEA option parquet {path}")
        return None
    if df.empty:
        return None
    if "underlying" in df.columns and underlying:
        df = df[df["underlying"] == underlying.upper()]
    if df.empty:
        return None
    df = df.copy()
    if pd.api.types.is_datetime64tz_dtype(df["ts"]):
        df["ts"] = df["ts"].dt.tz_localize(None)
    return df.sort_values("ts").reset_index(drop=True)


class OptionDayIndex:
    """Vectorized per-(strike, right) and total-OI lookup index for one day's real option chain.

    Built once per day (~0.07s for a full ~15,800-row chain) and reused
    across every bar and every parameter-wiggle variant of that same day —
    the O(n)-per-call pandas scan this replaces was ~60% of total sim time.
    """

    __slots__ = ("_by_key", "_oi_ts", "_oi_vals")

    def __init__(self, df: pd.DataFrame) -> None:
        self._by_key: dict[tuple[float, bool], tuple[np.ndarray, np.ndarray]] = {}
        is_call = df["right"].astype(str).str.upper().str.startswith("C")
        for (strike, call), g in df.groupby([df["strike"].astype(float), is_call], sort=False):
            ts_arr = g["ts"].to_numpy(dtype="datetime64[ns]")
            vals = g[["open", "high", "low", "close", "oi"]].to_numpy(dtype="float64")
            self._by_key[(float(strike), bool(call))] = (ts_arr, vals)

        oi_by_ts = df.groupby("ts")["oi"].sum().sort_index()
        self._oi_ts = oi_by_ts.index.to_numpy(dtype="datetime64[ns]")
        self._oi_vals = oi_by_ts.to_numpy(dtype="float64")

    @staticmethod
    def _nearest_idx(ts_arr: np.ndarray, at: datetime) -> int:
        target = np.datetime64(at, "ns")
        i = int(np.searchsorted(ts_arr, target))
        i = min(max(i, 0), len(ts_arr) - 1)
        if i > 0 and abs(ts_arr[i - 1] - target) < abs(ts_arr[i] - target):
            i -= 1
        return i

    def nearest_bar(self, strike: float, right: str, at: datetime) -> dict[str, float] | None:
        """Real OHLC+OI for one strike/right nearest to `at`, or None if that contract isn't in the chain."""
        key = (float(strike), str(right).upper().startswith("C"))
        entry = self._by_key.get(key)
        if entry is None:
            return None
        ts_arr, vals = entry
        if len(ts_arr) == 0:
            return None
        row = vals[self._nearest_idx(ts_arr, at)]
        return {"open": row[0], "high": row[1], "low": row[2], "close": row[3], "oi": row[4]}

    def nearest_total_oi(self, at: datetime) -> float | None:
        """Total (CE+PE) open interest across the whole chain nearest to `at`."""
        if len(self._oi_ts) == 0:
            return None
        return float(self._oi_vals[self._nearest_idx(self._oi_ts, at)])


@lru_cache(maxsize=300)
def load_option_day_index(underlying: str, date_str: str) -> OptionDayIndex | None:
    """Cached `OptionDayIndex` for one day — shared across every wiggle-test variant of that day.

    Sized above `MAX_BACKTEST_DAYS` (250, see routes.py) so a single run never
    evicts its own earlier days mid-pass. Each index is small (numpy arrays
    only, no DataFrame), so holding the max window's worth costs low-hundreds
    of MB, not the multi-GB a raw-DataFrame cache of this size would.
    """
    df = load_option_day(underlying, date_str)
    if df is None:
        return None
    return OptionDayIndex(df)


def oi_snapshot(df: pd.DataFrame, at: datetime) -> dict[float, dict[str, float]]:
    """Per-strike CE/PE open interest at the timestamp nearest to `at`.

    Real intraday OI moves throughout the day — this pins the wall
    calculation to one honest snapshot (mirrors how the live engine reads a
    single option-chain poll), rather than peeking at the whole day's OI.
    Called once per session (not per bar), so a plain pandas snapshot is fine.
    """
    if df.empty:
        return {}
    ts_arr = df["ts"].to_numpy(dtype="datetime64[ns]")
    idx = OptionDayIndex._nearest_idx(ts_arr, at)
    nearest_ts = df["ts"].iloc[idx]
    snap = df[df["ts"] == nearest_ts]
    is_call = snap["right"].astype(str).str.upper().str.startswith("C")
    ce = snap[is_call].set_index("strike")["oi"].to_dict()
    pe = snap[~is_call].set_index("strike")["oi"].to_dict()
    strikes = set(ce) | set(pe)
    return {
        float(k): {"ce_oi": float(ce.get(k, 0.0) or 0.0), "pe_oi": float(pe.get(k, 0.0) or 0.0)}
        for k in strikes
    }

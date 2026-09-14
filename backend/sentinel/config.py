"""Config loader: params.yaml + .env -> frozen dataclasses.

Single source of truth is `config/params.yaml` (build plan §3). Secrets come only
from the environment (§0.8). Nothing here is mutable at runtime except through
`reload()`, which re-reads the YAML from disk — that is how the PWA System screen
applies edits without a code change.

The risk engine's own `RiskConfig` is built from this file, never hand-edited.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field, fields
from datetime import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from sentinel.risk_engine import RiskConfig

# repo root = .../sentinel/  (contains config/, backend/, pwa/)
ROOT = Path(__file__).resolve().parents[2]
PARAMS_PATH = Path(os.getenv("SENTINEL_PARAMS", ROOT / "config" / "params.yaml"))
# Factory defaults, never written to. "Reset all" restores params.yaml from here.
DEFAULTS_PATH = Path(
    os.getenv("SENTINEL_PARAMS_DEFAULT", PARAMS_PATH.with_name("params.default.yaml"))
)

# .env may live beside the app or one level up (the user's working layout).
for _candidate in (ROOT / ".env", ROOT.parent / ".env"):
    if _candidate.is_file():
        load_dotenv(_candidate, override=False)
        break


def _parse_time(s: str) -> time:
    hh, mm = str(s).split(":")[:2]
    return time(int(hh), int(mm))


def _env_first(*names: str, default: str = "") -> str:
    """Return the first non-empty env var among `names`.

    The user's existing .env predates this build and uses GROWW_TOTP_TOKEN /
    GROWW_TOTP_CODE. Canonical names are checked first, then those aliases, so
    neither file has to be rewritten.
    """
    for n in names:
        v = os.getenv(n)
        if v:
            return v.strip()
    return default


@dataclass(frozen=True)
class Secrets:
    groww_api_key: str = ""
    groww_totp_seed: str = ""
    dhan_client_id: str = ""
    dhan_access_token: str = ""
    groq_api_key: str = ""
    claude_bridge_url: str = ""
    vapid_public: str = ""
    vapid_private: str = ""
    vapid_subject: str = ""
    api_shared_secret: str = ""
    pwa_origin: str = ""
    backup_s3_bucket: str = ""
    aws_region: str = "ap-south-1"

    @staticmethod
    def from_env() -> Secrets:
        return Secrets(
            groww_api_key=_env_first("GROWW_API_KEY", "GROWW_TOTP_TOKEN"),
            groww_totp_seed=_env_first("GROWW_TOTP_SEED", "GROWW_TOTP_CODE"),
            dhan_client_id=_env_first("DHAN_CLIENT_ID"),
            dhan_access_token=_env_first("DHAN_ACCESS_TOKEN"),
            groq_api_key=_env_first("GROQ_API_KEY", "GROQ_KEY"),
            claude_bridge_url=_env_first("CLAUDE_BRIDGE_URL"),
            vapid_public=_env_first("VAPID_PUBLIC"),
            vapid_private=_env_first("VAPID_PRIVATE"),
            vapid_subject=_env_first("VAPID_SUBJECT", default="mailto:admin@example.com"),
            api_shared_secret=_env_first("API_SHARED_SECRET", default="dev-insecure"),
            pwa_origin=_env_first("PWA_ORIGIN", default="*"),
            backup_s3_bucket=_env_first("BACKUP_S3_BUCKET"),
            aws_region=_env_first("AWS_REGION", default="ap-south-1"),
        )

    def redacted(self) -> dict[str, str]:
        """Safe for logs and for the System screen. Never returns key material."""
        def mask(v: str) -> str:
            return f"set({len(v)} chars)" if v else "unset"

        return {
            "groww_api_key": mask(self.groww_api_key),
            "groww_totp_seed": mask(self.groww_totp_seed),
            "groq_api_key": mask(self.groq_api_key),
            "vapid_public": mask(self.vapid_public),
            "vapid_private": mask(self.vapid_private),
            "claude_bridge_url": self.claude_bridge_url or "unset",
            "vapid_subject": self.vapid_subject,
        }


@dataclass(frozen=True)
class SizingCfg:
    max_premium_pct_capital: float = 1.0
    min_lots: int = 1
    max_position_cost: float = 14_000.0
    max_lots: dict[str, int] = field(default_factory=dict)

    def lots_ceiling(self, instrument: str) -> int:
        """Per-instrument lot ceiling; 0 means unlimited."""
        return int(self.max_lots.get(instrument.upper(), 0))


@dataclass(frozen=True)
class DataCfg:
    tick_staleness_degraded_s: int = 10
    tick_staleness_critical_s: int = 60
    chain_snapshot_interval_s: int = 30
    oi_diff_lookback_min: int = 30
    recon_interval_s: int = 15
    pnl_tick_interval_s: int = 2


@dataclass(frozen=True)
class GuardianCfg:
    detection: str = "hybrid"
    poll_interval_s: int = 2
    sl_attach_deadline_s: int = 60
    squareoff_verify_s: int = 10
    yield_to_manual_sl: bool = True


@dataclass(frozen=True)
class LLMCfg:
    min_conf: int = 70
    suggest_cap_per_day: int = 10
    timeout_s: int = 10
    bridge_retries: int = 2
    bridge_url: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_fallback_model: str = "openai/gpt-oss-120b"


@dataclass(frozen=True)
class WeekCfg:
    loss_limit_mult: float = 2.5
    red_days_to_paper: int = 3


@dataclass(frozen=True)
class EventsCfg:
    debounce: dict[str, int] = field(default_factory=dict)
    thresholds: dict[str, float] = field(default_factory=dict)
    times: dict[str, time] = field(default_factory=dict)


@dataclass(frozen=True)
class InstrumentsCfg:
    primary: str = "SENSEX"
    secondary: str = "NIFTY"
    chain_depth: int = 5


@dataclass(frozen=True)
class AlgoCfg:
    enabled: bool = True
    active_strategy: str = "institutional_breakout"
    auto_execute: bool = False


@dataclass(frozen=True)
class Settings:
    raw: dict[str, Any]
    risk: RiskConfig
    sizing: SizingCfg
    data: DataCfg
    guardian: GuardianCfg
    llm: LLMCfg
    week: WeekCfg
    events: EventsCfg
    instruments: InstrumentsCfg
    secrets: Secrets
    algo: AlgoCfg = field(default_factory=AlgoCfg)
    broker_name: str = "dhan"
    mode: str = "LIVE"
    floor_warning_rupees: float = 200.0
    tz: str = "Asia/Kolkata"

    @property
    def is_live(self) -> bool:
        return self.mode.upper() == "LIVE"

    @property
    def capital(self) -> float:
        return self.risk.capital


def _build_data_cfg(raw_data: dict[str, Any]) -> DataCfg:
    """Only known keys, and unknown ones warn instead of crashing the process.

    Splatting the YAML sub-tree meant one stray key under `data:` raised TypeError
    at import time inside create_app() — the container crash-looped with no /health
    and no alert.
    """
    known = {f.name for f in fields(DataCfg)}
    kwargs: dict[str, int] = {}
    for k, v in raw_data.items():
        if k not in known:
            _log_unknown("data", k)
            continue
        try:
            kwargs[k] = int(round(float(v)))
        except (TypeError, ValueError):
            _log_unknown("data", f"{k} (non-numeric {v!r})")
    return DataCfg(**kwargs)


def _log_unknown(section: str, key: str) -> None:
    import logging
    logging.getLogger("config").warning(
        "ignoring unknown params.yaml key", extra={"section": section, "key": key})


def _build(raw: dict[str, Any], secrets: Secrets) -> Settings:
    daily = raw.get("daily", {})
    trades = raw.get("trades", {})
    fsm = raw.get("fsm", {})
    protect = fsm.get("protect", {})
    trail = fsm.get("trail", {})
    win = raw.get("windows", {})
    sizing = raw.get("sizing", {})
    llm = raw.get("llm", {})
    ev = raw.get("events", {})

    risk = RiskConfig(
        capital=float(raw.get("capital", 15_000)),
        target=float(daily.get("target", 2_500)),
        loss_limit=float(daily.get("loss_limit", 1_050)),
        risk_per_trade=float(daily.get("risk_per_trade", 1_200)),
        base_trades=int(trades.get("base", 3)),
        bonus_at_pct_target=float(trades.get("bonus_at_pct_target", 0.45)),
        consecutive_loss_stop=int(trades.get("consecutive_loss_stop", 2)),
        t1_keep_pct=float(fsm.get("t1_keep_pct", 0.70)),
        protect_at=float(protect.get("at", 1.0)),
        protect_floor_pct=float(protect.get("floor_pct_target", 0.5)),
        trail_at=float(trail.get("at", 1.3)),
        trail_giveback=float(trail.get("giveback", 0.25)),
        risk_scale_protected=float(protect.get("risk_scale", 0.5)),
        min_confidence=int(llm.get("min_conf", 70)),
        min_confidence_protected=int(protect.get("min_conf", 80)),
        entry_open=_parse_time(win.get("entry_open", "09:20")),
        entry_close=_parse_time(win.get("entry_close", "15:00")),
        squareoff_at=_parse_time(win.get("squareoff", "15:10")),
        expiry_entry_close=_parse_time(win.get("expiry_entry_close", "14:30")),
        expiry_risk_scale=float(win.get("expiry_risk_scale", 0.5)),
        default_sl_premium_pts={
            str(k).upper(): float(v)
            for k, v in (raw.get("default_sl_premium_pts") or {"NIFTY": 12, "SENSEX": 35}).items()
        },
    )

    # r > L means the per-trade budget can never be spent: the daily lock fires on the
    # tick before a single full-risk trade reaches its stop. Not fatal — the tighter
    # number simply wins — but it is always a config mistake, so say so out loud.
    if risk.risk_per_trade > risk.loss_limit:
        import logging
        logging.getLogger("config").warning(
            "risk_per_trade exceeds loss_limit — the daily lock binds first, so the "
            "per-trade risk budget is unreachable",
            extra={"risk_per_trade": risk.risk_per_trade, "loss_limit": risk.loss_limit},
        )

    bridge_url = str(llm.get("bridge_url", "") or "")
    if bridge_url.startswith("${") and bridge_url.endswith("}"):
        bridge_url = os.getenv(bridge_url[2:-1], "") or secrets.claude_bridge_url
    bridge_url = bridge_url or secrets.claude_bridge_url

    return Settings(
        raw=raw,
        risk=risk,
        sizing=SizingCfg(
            max_premium_pct_capital=float(sizing.get("max_premium_pct_capital", 1.0)),
            min_lots=int(sizing.get("min_lots", 1)),
            max_position_cost=float(sizing.get("max_position_cost", 14_000)),
            max_lots={str(k).upper(): int(v)
                      for k, v in (sizing.get("max_lots") or {}).items()},
        ),
        data=_build_data_cfg(raw.get("data") or {}),
        guardian=GuardianCfg(
            detection=str((raw.get("guardian") or {}).get("detection", "rest_poll")),
            poll_interval_s=int((raw.get("guardian") or {}).get("poll_interval_s", 5)),
            sl_attach_deadline_s=int((raw.get("guardian") or {}).get("sl_attach_deadline_s", 60)),
            squareoff_verify_s=int((raw.get("guardian") or {}).get("squareoff_verify_s", 10)),
            yield_to_manual_sl=bool((raw.get("guardian") or {}).get("yield_to_manual_sl", True)),
        ),
        llm=LLMCfg(
            min_conf=int(llm.get("min_conf", 70)),
            suggest_cap_per_day=int(llm.get("suggest_cap_per_day", 10)),
            timeout_s=int(llm.get("timeout_s", 10)),
            bridge_retries=int(llm.get("bridge_retries", 2)),
            bridge_url=bridge_url,
            groq_model=str(llm.get("groq_model", "llama-3.3-70b-versatile")),
            groq_fallback_model=str(llm.get("groq_fallback_model", "openai/gpt-oss-120b")),
        ),
        week=WeekCfg(
            loss_limit_mult=float((raw.get("week") or {}).get("loss_limit_mult", 2.5)),
            red_days_to_paper=int((raw.get("week") or {}).get("red_days_to_paper", 3)),
        ),
        events=EventsCfg(
            debounce={k: int(v) for k, v in (ev.get("debounce") or {}).items()},
            thresholds={k: float(v) for k, v in (ev.get("thresholds") or {}).items()},
            times={k: _parse_time(v) for k, v in (ev.get("times") or {}).items()},
        ),
        instruments=InstrumentsCfg(
            primary=str((raw.get("instruments") or {}).get("primary", "SENSEX")).upper(),
            secondary=str((raw.get("instruments") or {}).get("secondary", "NIFTY")).upper(),
            chain_depth=int((raw.get("instruments") or {}).get("chain_depth", 5)),
        ),
        secrets=secrets,
        algo=AlgoCfg(
            enabled=bool((raw.get("algo") or {}).get("enabled", True)),
            active_strategy=str((raw.get("algo") or {}).get("active_strategy", "institutional_breakout")),
            auto_execute=bool((raw.get("algo") or {}).get("auto_execute", False)),
        ),
        broker_name=str(os.getenv("BROKER_NAME") or raw.get("broker") or "dhan").lower(),
        mode=str(raw.get("mode", "LIVE")).upper(),
        floor_warning_rupees=float((raw.get("push") or {}).get("floor_warning_rupees", 200)),
        tz=os.getenv("TZ", "Asia/Kolkata"),
    )


_lock = threading.Lock()
_cached: Settings | None = None


def load(path: Path | None = None) -> Settings:
    raw = yaml.safe_load((path or PARAMS_PATH).read_text()) or {}
    return _build(raw, Secrets.from_env())


def get() -> Settings:
    """Process-wide settings. Cheap after the first call."""
    global _cached
    if _cached is None:
        with _lock:
            if _cached is None:
                _cached = load()
    return _cached


def reload() -> Settings:
    """Re-read params.yaml from disk. Used by PUT /config after a validated write."""
    global _cached
    with _lock:
        _cached = load()
    return _cached


RISK_PRESETS: dict[str, dict[str, float]] = {
    "CONSERVATIVE": {"target": 1500.0, "loss_limit": 1000.0, "risk_per_trade": 500.0},
    "MODERATE": {"target": 2500.0, "loss_limit": 1500.0, "risk_per_trade": 1200.0},
    "AGGRESSIVE": {"target": 5000.0, "loss_limit": 3000.0, "risk_per_trade": 2500.0},
}


def apply_preset(name: str, path: Path | None = None) -> Settings:
    """Apply a named risk profile preset (CONSERVATIVE | MODERATE | AGGRESSIVE)."""
    name_upper = name.strip().upper()
    if name_upper not in RISK_PRESETS:
        raise ValueError(f"Unknown risk preset '{name}'. Valid presets: {list(RISK_PRESETS.keys())}")

    p = path or PARAMS_PATH
    raw = yaml.safe_load(p.read_text()) if p.is_file() else {}
    raw.setdefault("daily", {})
    preset_vals = RISK_PRESETS[name_upper]
    raw["daily"]["preset"] = name_upper
    raw["daily"]["target"] = preset_vals["target"]
    raw["daily"]["loss_limit"] = preset_vals["loss_limit"]
    raw["daily"]["risk_per_trade"] = preset_vals["risk_per_trade"]
    return write_params(raw, p)


def default_params(path: Path | None = None) -> dict[str, Any]:
    """The factory defaults as a plain dict. Falls back to the live file if the
    defaults snapshot is missing, so "reset" degrades to "no change" rather than
    wiping the config."""
    src = path or DEFAULTS_PATH
    if not src.is_file():
        src = PARAMS_PATH
    return yaml.safe_load(src.read_text()) or {}


def write_params(new_raw: dict[str, Any], path: Path | None = None) -> Settings:
    """Atomically persist params.yaml, then reload.

    Validation happens by construction: we build a Settings from the candidate
    dict BEFORE touching disk, so a bad edit cannot leave an unbootable file.

    yaml.safe_dump drops the comments in params.yaml, so the first runtime write
    preserves the annotated original beside it once. The factory defaults live in
    params.default.yaml and are never touched.
    """
    _build(new_raw, Secrets.from_env())  # raises on invalid input
    target = path or PARAMS_PATH
    backup = target.with_name(target.stem + ".annotated.yaml")
    if target.is_file() and not backup.exists():
        backup.write_text(target.read_text())
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(new_raw, sort_keys=False, default_flow_style=False))
    tmp.replace(target)
    return reload()


def reset_params(path: Path | None = None) -> Settings:
    """Restore params.yaml from the factory defaults."""
    return write_params(default_params(), path)

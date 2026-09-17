"""Runtime config editing: the PWA writes params.yaml, and "reset all" must be able
to get back to a known-good state. Both paths validate BEFORE touching disk."""
import shutil

import pytest
import yaml
from vectra_quant import config as cfg


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A throwaway params.yaml + params.default.yaml pair."""
    live = tmp_path / "params.yaml"
    defaults = tmp_path / "params.default.yaml"
    shutil.copy(cfg.PARAMS_PATH, live)
    shutil.copy(cfg.PARAMS_PATH, defaults)
    monkeypatch.setattr(cfg, "PARAMS_PATH", live)
    monkeypatch.setattr(cfg, "DEFAULTS_PATH", defaults)
    return live, defaults


def test_write_then_read_round_trips(sandbox):
    live, _ = sandbox
    raw = yaml.safe_load(live.read_text())
    # target is a computed property (capital x target_pct), not a raw key --
    # the schema only ever reads target_pct (see config/params.yaml).
    raw["daily"]["target_pct"] = 3000 / raw["capital"]
    raw["sizing"]["max_lots"]["SENSEX"] = 3

    s = cfg.write_params(raw)
    assert s.risk.target == 3000
    assert s.sizing.lots_ceiling("SENSEX") == 3
    assert yaml.safe_load(live.read_text())["daily"]["target_pct"] == 3000 / raw["capital"]


def test_invalid_edit_never_reaches_disk(sandbox):
    live, _ = sandbox
    before = live.read_text()
    with pytest.raises((ValueError, TypeError)):
        cfg.write_params({"daily": {"target": "not-a-number"}, "windows": {"entry_open": "banana"}})
    assert live.read_text() == before          # unbootable file was refused


def test_first_write_preserves_the_annotated_original(sandbox):
    live, _ = sandbox
    assert "# VECTRA_QUANT" in live.read_text()    # comments present to begin with
    raw = yaml.safe_load(live.read_text())
    cfg.write_params(raw)
    backup = live.with_name("params.annotated.yaml")
    assert backup.is_file() and "# VECTRA_QUANT" in backup.read_text()
    assert "# VECTRA_QUANT" not in live.read_text()  # safe_dump drops them, as documented


def test_reset_restores_factory_defaults(sandbox):
    live, defaults = sandbox
    raw = yaml.safe_load(live.read_text())
    raw["daily"]["target_pct"] = 9999 / raw["capital"]
    raw["mode"] = "PAPER"
    cfg.write_params(raw)
    assert cfg.load(live).risk.target == 9999

    s = cfg.reset_params()
    factory = yaml.safe_load(defaults.read_text())
    assert s.risk.target == factory["capital"] * factory["daily"]["target_pct"]
    assert s.mode == factory["mode"].upper()


def test_reset_with_missing_defaults_is_a_no_op_not_a_wipe(sandbox, monkeypatch):
    live, defaults = sandbox
    defaults.unlink()
    raw = yaml.safe_load(live.read_text())
    raw["daily"]["target_pct"] = 4321 / raw["capital"]
    cfg.write_params(raw)

    s = cfg.reset_params()                     # falls back to the live file
    assert s.risk.target == 4321
    assert yaml.safe_load(live.read_text())["daily"]["target_pct"] == 4321 / raw["capital"]

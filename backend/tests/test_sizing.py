"""Sizing gate: Rs.14,000 hard cap (D-003) and the Rs.7-12k preference (D-010)."""
from sentinel.core.sizing import (
    Candidate,
    atm_offset,
    nearest_atm,
    select_strike,
    size_with_cap,
    strike_step,
)

CAP = 14_000.0


def test_nifty_one_lot_fits_under_cap():
    s = size_with_cap(max_risk=1200, sl_points_premium=12, lot_size=65,
                      premium=141, capital=15_000, max_position_cost=CAP)
    assert s.allowed
    assert s.lots == 1 and s.qty == 65
    assert s.cost == 141 * 65 == 9165
    assert s.risk == 12 * 65 == 780
    assert not s.over_risk


def test_one_lot_over_cap_is_rejected_not_shrunk():
    """Cap wins over 'always allow 1 lot'. NIFTY premium 220 x 65 = 14,300."""
    s = size_with_cap(max_risk=1200, sl_points_premium=12, lot_size=65,
                      premium=220, capital=15_000, max_position_cost=CAP)
    assert not s.allowed
    assert s.lots == 0
    assert "cap" in s.reason


def test_sensex_one_lot_at_boundary():
    # 700 x 20 = 14,000 exactly -> allowed (cap is inclusive)
    ok = size_with_cap(max_risk=1200, sl_points_premium=35, lot_size=20,
                       premium=700, capital=15_000, max_position_cost=CAP)
    assert ok.allowed and ok.cost == 14_000.0
    # 700.05 x 20 = 14,001 -> rejected
    no = size_with_cap(max_risk=1200, sl_points_premium=35, lot_size=20,
                       premium=700.05, capital=15_000, max_position_cost=CAP)
    assert not no.allowed


def test_over_risk_flagged_but_allowed_when_affordable():
    """User rule: 1 lot always offered even if its risk exceeds r, flagged."""
    s = size_with_cap(max_risk=600, sl_points_premium=12, lot_size=65,
                      premium=141, capital=15_000, max_position_cost=CAP)
    assert s.allowed and s.lots == 1
    assert s.over_risk                     # 780 risk > 600 max_risk


def test_multi_lot_trimmed_to_cap_not_to_capital():
    """Bigger capital: risk allows 7 lots, cost cap allows fewer."""
    s = size_with_cap(max_risk=6000, sl_points_premium=12, lot_size=65,
                      premium=141, capital=500_000, max_position_cost=CAP)
    assert s.allowed
    assert s.lots == 1                     # 2 lots = 18,330 > 14,000
    assert s.cost <= CAP


def test_reported_risk_and_cost_match_final_lots():
    """Blocker B5: engine reports pre-reduction figures; wrapper must not."""
    s = size_with_cap(max_risk=6000, sl_points_premium=12, lot_size=65,
                      premium=141, capital=500_000, max_position_cost=CAP)
    assert s.cost == s.lots * 141 * 65
    assert s.risk == s.lots * 12 * 65


def test_unknown_lot_size_refuses():
    s = size_with_cap(max_risk=1200, sl_points_premium=12, lot_size=0,
                      premium=141, capital=15_000, max_position_cost=CAP)
    assert not s.allowed and "lot size" in s.reason


def test_missing_stop_refuses():
    s = size_with_cap(max_risk=1200, sl_points_premium=0, lot_size=65,
                      premium=141, capital=15_000, max_position_cost=CAP)
    assert not s.allowed and "stop" in s.reason


# ---------------------------------------------------------------- strike selection

def _c(sym, strike, premium, lot, offset, oi=1000, vol=500, bid=0.0, ask=0.0):
    return Candidate(trading_symbol=sym, strike=strike, side="CE", premium=premium,
                     lot_size=lot, atm_offset=offset, open_interest=oi, volume=vol,
                     bid=bid, ask=ask)


def test_prefers_cost_band_over_cheapest():
    cheap = _c("CHEAP", 25000, 20, 65, 2)      # 1,300 — far too cheap, deep OTM
    band = _c("BAND", 24300, 150, 65, 1)       # 9,750 — inside 7-12k
    picked = select_strike([cheap, band])
    assert picked is not None and picked.trading_symbol == "BAND"


def test_rejects_above_hard_cap_even_if_atm():
    over = _c("OVER", 24250, 220, 65, 0)       # 14,300 > cap
    ok = _c("OK", 24300, 150, 65, 1)           # 9,750
    picked = select_strike([over, ok])
    assert picked is not None and picked.trading_symbol == "OK"


def test_returns_none_when_nothing_tradable():
    assert select_strike([]) is None
    assert select_strike([_c("OVER", 1, 500, 65, 0)]) is None       # 32,500 > cap


def test_allows_outside_band_when_band_empty():
    """Lower/higher is fine when the band has nothing (user instruction)."""
    only = _c("LOW", 24500, 30, 65, 1)         # 1,950 — outside band but under cap
    picked = select_strike([only])
    assert picked is not None and picked.trading_symbol == "LOW"


def test_atm_preferred_when_cost_equal():
    atm = _c("ATM", 24250, 150, 65, 0)
    far = _c("FAR", 24450, 150, 65, 2)
    assert select_strike([atm, far]).trading_symbol == "ATM"


def test_wide_spread_penalised():
    tight = _c("TIGHT", 24300, 150, 65, 1, bid=149.0, ask=151.0)
    wide = _c("WIDE", 24250, 150, 65, 0, bid=135.0, ask=165.0)   # 20% spread
    assert select_strike([tight, wide]).trading_symbol == "TIGHT"


def test_offset_beyond_max_excluded():
    far = _c("FAR", 26000, 150, 65, 5)
    assert select_strike([far], max_atm_offset=2) is None


def test_atm_helpers():
    strikes = [24000.0, 24050.0, 24100.0, 24150.0, 24200.0]
    assert strike_step(strikes) == 50.0
    assert nearest_atm(strikes, 24127.0) == 24150.0
    assert atm_offset(24050.0, 24150.0, 50.0) == 2
    assert nearest_atm([], 100.0) is None

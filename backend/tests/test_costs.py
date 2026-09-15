"""Cost model, verified against a hand-computed round trip (design §9 acceptance)."""
from vectra_quant.brokers.costs import CostRates, leg_cost, net_pnl, round_trip_cost

R = CostRates()


def test_buy_leg_hand_computed():
    # NIFTY 1 lot: premium 141, lot 65 -> turnover 9165
    leg = leg_cost(141.0, 65, "BUY", exchange="NSE")
    assert leg.turnover == 9165.0
    assert leg.brokerage == 20.0
    assert leg.stt == 0.0                                  # buy side: no STT
    assert round(leg.exchange_txn, 4) == round(9165 * R.exchange_txn_pct_nse, 4)
    assert round(leg.stamp, 4) == round(9165 * R.stamp_duty_buy_pct, 4)
    expected_gst = (20.0 + 9165 * R.exchange_txn_pct_nse + 9165 * R.sebi_turnover_pct) * 0.18
    assert round(leg.gst, 4) == round(expected_gst, 4)


def test_sell_leg_charges_stt_not_stamp():
    leg = leg_cost(160.0, 65, "SELL", exchange="NSE")
    assert leg.stt == round(160.0 * 65 * R.stt_sell_pct, 2)  # 0.1% of 10400 = 10.40
    assert leg.stt == 10.40
    assert leg.stamp == 0.0


def test_bse_uses_bse_txn_rate():
    nse = leg_cost(400.0, 20, "BUY", exchange="NSE")
    bse = leg_cost(400.0, 20, "BUY", exchange="BSE")
    assert bse.exchange_txn < nse.exchange_txn          # BSE rate is lower
    assert bse.turnover == nse.turnover == 8000.0


def test_round_trip_is_material_against_risk_budget():
    """Design §9: ~Rs.50-60 round trip eats 3-4% of an r=750 budget. Confirm the scale."""
    total = round_trip_cost(141.0, 160.0, 65, exchange="NSE")
    assert 40.0 < total < 80.0, total
    assert total / 1200.0 < 0.08                        # under 8% of r=1200


def test_net_pnl_subtracts_costs():
    gross, costs, net = net_pnl(141.0, 160.0, 65, exchange="NSE")
    assert gross == round((160.0 - 141.0) * 65, 2) == 1235.0
    assert costs > 0
    assert net == round(gross - costs, 2)
    assert net < gross


def test_losing_trade_costs_add_to_loss():
    gross, costs, net = net_pnl(141.0, 129.0, 65, exchange="NSE")
    assert gross < 0
    assert net < gross                                   # costs deepen the loss


def test_zero_quantity_is_free():
    leg = leg_cost(141.0, 0, "BUY")
    assert leg.total == 0.0


# ================================================================ review regression

def test_level_break_requires_a_cross_not_proximity(tmp_path, monkeypatch):
    """#12 — firing on distance alone meant spot resting 0.06% under PDL triggered on
    every tick and re-fired each debounce window, burning the daily LLM budget."""
    from vectra_quant import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "lb.db")
    monkeypatch.setattr(db_mod, "_engine", None, raising=False)
    monkeypatch.setattr(db_mod, "_SessionLocal", None, raising=False)
    db_mod.init_db()

    from vectra_quant.events.engine import EventEngine
    e = EventEngine(thresholds={"level_break_pct": 0.05})
    levels = {"PDL": 24000.0}

    # sitting just below the level, never crossed -> no event
    assert e.check_level_break("NIFTY", 23985.0, levels, prev_close=23984.0) == []
    # a genuine downward cross -> fires
    evs = e.check_level_break("NIFTY", 23985.0, levels, prev_close=24010.0)
    assert len(evs) == 1 and evs[0].detail["direction"] == "below"

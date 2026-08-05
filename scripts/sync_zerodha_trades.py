"""Sync today's executed Zerodha trades into SENTINEL SQLite DB."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from sentinel import config, db
from sentinel.brokers.costs import net_pnl
from sentinel.brokers.zerodha import ZerodhaAdapter
from sentinel.core.session_clock import session_date

def sync_trades() -> None:
    db.init_db()
    settings = config.get()

    if not settings.secrets.zerodha_api_key or not settings.secrets.zerodha_access_token:
        print("ZERODHA_API_KEY / ZERODHA_ACCESS_TOKEN missing in .env")
        return

    adapter = ZerodhaAdapter(settings.secrets.zerodha_api_key, settings.secrets.zerodha_access_token)
    orders = adapter.get_orders()

    completed = [o for o in orders if o.status in ("COMPLETE", "FILLED")]
    if not completed:
        print("No completed orders found in Zerodha today.")
        return

    # Group by trading symbol
    by_symbol: dict[str, list] = {}
    for o in completed:
        by_symbol.setdefault(o.trading_symbol, []).append(o)

    today = session_date()

    with db.session() as s:
        saved_count = 0
        total_realized = 0.0

        for sym, symbol_orders in by_symbol.items():
            buys = [o for o in symbol_orders if o.side.upper() == "BUY"]
            sells = [o for o in symbol_orders if o.side.upper() == "SELL"]

            # Match buys and sells
            for buy in buys:
                for sell in sells:
                    if buy.quantity > 0 and sell.quantity > 0 and buy.quantity == sell.quantity:
                        qty = buy.quantity
                        entry_px = buy.average_price or buy.price
                        exit_px = sell.average_price or sell.price
                        exch = buy.exchange or "NSE"

                        gross, costs, net = net_pnl(entry_px, exit_px, qty, exchange=exch)

                        # Instrument name derivation
                        inst_name = "NIFTY" if "NIFTY" in sym else ("SENSEX" if "SENSEX" in sym else "BANKNIFTY")
                        direction = "PE" if "PE" in sym else "CE"

                        trade = db.Trade(
                            id=str(uuid.uuid4()),
                            opened_at=datetime.now(UTC),
                            closed_at=datetime.now(UTC),
                            session_date=today,
                            origin="manual",
                            instrument=inst_name,
                            trading_symbol=sym,
                            exchange=exch,
                            segment="FNO",
                            direction=direction,
                            lots=1,
                            lot_size=qty,
                            qty=qty,
                            entry_price=entry_px,
                            exit_price=exit_px,
                            entry_order_id=buy.order_id,
                            sl_order_id="",
                            broker_order_ids=f"[\"{buy.order_id}\", \"{sell.order_id}\"]",
                            gross_pnl=gross,
                            costs=costs,
                            pnl=net,
                            exit_reason="manual",
                            status="CLOSED",
                        )
                        s.add(trade)
                        saved_count += 1
                        total_realized += net
                        # Mark used
                        buy.quantity = 0
                        sell.quantity = 0

    print(f"\n==========================================")
    print(f"Successfully synced {saved_count} completed trade(s) from Zerodha into SENTINEL DB!")
    print(f"Total Net P&L recorded today: ₹{total_realized:+,.2f}")
    print(f"==========================================\n")

if __name__ == "__main__":
    sync_trades()

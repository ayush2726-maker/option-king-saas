from bot.live_daily_history_response_patch import _repair_live_trade


def test_paper_history_row_is_not_relabelled_or_recalculated_as_live():
    trade = {
        "id": 1,
        "user_id": 1,
        "symbol": "NIFTY01SEP2625000CE",
        "status": "CLOSED",
        "trading_mode": "paper",
        "entry_price": 100,
        "exit_price": 120,
        "qty": 65,
        "net_pnl": 1000,
        "pnl": 1000,
        "pnl_basis": "PAPER_LTP_WITH_ESTIMATED_SLIPPAGE_AND_CHARGES",
    }

    repaired = _repair_live_trade(trade)

    assert repaired == trade
    assert repaired["trading_mode"] == "paper"
    assert repaired["net_pnl"] == 1000


def test_broker_order_keeps_legacy_live_row_eligible_for_live_repair(monkeypatch):
    trade = {
        "id": 2,
        "user_id": 1,
        "symbol": "SENSEX01SEP2680000PE",
        "status": "OPEN",
        "trading_mode": "paper",
        "entry_order_id": "ANGEL-2",
        "entry_price": 100,
        "qty": 20,
        "total_charges": 75,
    }

    repaired = _repair_live_trade(trade)

    assert repaired["qty"] == 20
    assert repaired["quantity"] == 20
    assert repaired["execution_cost"] == 75

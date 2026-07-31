from bot import authoritative_profit_lock_runtime_patch as patch


class FakeConn:
    def __init__(self, sl_price):
        self.sl_price = sl_price

    def execute(self, _sql, _params):
        return self

    def fetchone(self):
        return {"sl_price": self.sl_price}


def _trade(**overrides):
    data = {
        "id": 7,
        "entry_price": 100.0,
        "sl_price": 95.0,
        "initial_risk": 5.0,
        "peak_price": 100.0,
        "qty": 50,
        "broker_name": "upstox",
        "trading_mode": "paper",
        "underlying": "NIFTY",
        "symbol": "NIFTY TEST CE",
    }
    data.update(overrides)
    return data


def test_trail_waits_for_exact_charges_plus_4pct(monkeypatch):
    monkeypatch.setattr(
        patch.live_cost,
        "calculate_exact_breakeven_price",
        lambda *_args, **_kwargs: {
            "price": 110.0,
            "target_net_profit": 400.0,
            "net_pnl_at_price": 400.0,
        },
    )

    waiting = patch._authoritative_trail(
        _trade(peak_price=109.0),
        109.0,
    )
    assert waiting["breakeven_triggered"] is False
    assert waiting["sl_price"] == 95.0
    assert waiting["breakeven_net_profit_percent"] == 4.0

    locked = patch._authoritative_trail(
        _trade(peak_price=111.0),
        111.0,
    )
    assert locked["breakeven_triggered"] is True
    assert locked["sl_price"] >= 110.05
    assert "4PCT" in locked["breakeven_rule"]


def test_paper_stop_fill_is_capped_to_one_tick_below_saved_stop():
    trade = _trade(sl_price=95.0)
    fill = patch._paper_stop_fill_price(
        FakeConn(110.05),
        trade,
        102.0,
        "PROFIT LOCK TRAIL HIT | CHARGES_PLUS_4PCT_LOCK",
    )
    assert fill == 110.0


def test_live_and_non_stop_exits_are_not_repriced():
    live_trade = _trade(trading_mode="live")
    assert patch._paper_stop_fill_price(
        FakeConn(110.05),
        live_trade,
        102.0,
        "PROFIT LOCK TRAIL HIT",
    ) == 102.0

    paper_trade = _trade()
    assert patch._paper_stop_fill_price(
        FakeConn(110.05),
        paper_trade,
        102.0,
        "MANUAL EXIT BY USER",
    ) == 102.0

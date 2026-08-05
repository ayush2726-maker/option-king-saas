import sqlite3

from bot import auto_portfolio_runtime as runtime


def _row(**values):
    class Row(dict):
        def keys(self):
            return super().keys()
    return Row(values)


def test_small_capital_can_take_one_complete_lot_with_reserve():
    result = runtime._size(10000, 1, 200, 30, rows=[])
    assert result["one_lot_cost"] == 6000
    assert result["target_slot_budget"] == 5000
    assert result["reserve_floor"] == 1000
    assert result["lots"] == 1
    assert result["qty"] == 30
    assert result["flex_used"] is True
    assert result["sizing_mode"] == "LOT_AWARE_FLEX_ONE_LOT"


def test_reserve_is_never_breached():
    result = runtime._size(10000, 1, 310, 30, rows=[])
    assert result["one_lot_cost"] == 9300
    assert result["available_after_reserve"] == 9000
    assert result["lots"] == 0
    assert result["flex_used"] is False


def test_second_slot_uses_only_remaining_capital_above_reserve():
    existing = _row(capital_used=6000, entry_price=200, qty=30)
    result = runtime._size(10000, 2, 200, 30, rows=[existing])
    assert result["committed_capital"] == 6000
    assert result["available_after_reserve"] == 3000
    assert result["lots"] == 0


def test_large_capital_keeps_normal_target_slot_sizing():
    result = runtime._size(100000, 1, 200, 30, rows=[])
    assert result["target_slot_budget"] == 50000
    assert result["flex_used"] is False
    assert result["lots"] == 8
    assert result["capital_used"] == 48000


def test_route_exposes_sizing_diagnostics():
    source = open("bot/routes.py", encoding="utf-8").read()
    assert '"entry_sizing": (' in source
    assert '"position_size_block": (' in source
    assert '"entry_candidate_attempts": (' in source

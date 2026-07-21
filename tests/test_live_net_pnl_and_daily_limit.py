from backtest.daily_trade_limit_patch import _limit_day_result
from bot.live_net_pnl_breakeven_patch import (
    calculate_execution_costs,
    calculate_exact_breakeven_price,
)


def test_live_actual_fills_deduct_charges_without_double_slippage():
    costs = calculate_execution_costs(
        "angelone",
        "NIFTY",
        100.0,
        105.0,
        65,
        include_slippage=False,
    )
    assert costs["market_gross_pnl"] == 325.0
    assert costs["slippage_cost"] == 0.0
    assert costs["total_charges"] > 0
    assert costs["net_pnl"] < costs["market_gross_pnl"]
    assert costs["execution_basis"] == "LIVE_ACTUAL_FILLS_MINUS_ESTIMATED_CHARGES"


def test_paper_pnl_includes_estimated_slippage_and_all_charges():
    costs = calculate_execution_costs(
        "angelone",
        "SENSEX",
        100.0,
        105.0,
        20,
        include_slippage=True,
    )
    assert costs["market_gross_pnl"] == 100.0
    assert costs["slippage_cost"] > 0
    assert costs["total_charges"] > 0
    assert costs["net_pnl"] < costs["market_gross_pnl"]


def test_exact_breakeven_covers_trade_costs_plus_two_percent():
    solved = calculate_exact_breakeven_price(
        "angelone",
        "BANKNIFTY",
        150.0,
        60,
        "paper",
        2.0,
    )
    assert solved["price"] > 150.0
    assert solved["quantity_basis"] == 60
    assert solved["instrument_basis"] == "BANKNIFTY"
    assert solved["net_pnl_at_price"] >= solved["target_net_profit"]
    assert solved["target_net_profit"] == 180.0


def test_backtest_keeps_only_first_five_trades_per_day():
    trades = []
    for index in range(8):
        trades.append(
            {
                "trade_no": index + 1,
                "entry_time": f"2026-07-10T10:{index:02d}:00",
                "entry_price": 100.0,
                "exit_price": 101.0,
                "qty": 10,
                "pnl": 10.0,
                "gross_pnl": 12.0,
                "total_charges": 2.0,
            }
        )

    limited = _limit_day_result(
        {
            "success": True,
            "capital": 100000.0,
            "trades": trades,
            "summary": {"capital": 100000.0},
        }
    )

    assert limited["total_trades"] == 5
    assert limited["total_pnl"] == 50.0
    assert limited["ending_capital"] == 100050.0
    assert limited["trades_before_daily_limit"] == 8
    assert limited["trades_dropped_by_daily_limit"] == 3
    assert limited["daily_trade_limit"] == 5
    assert [trade["trade_no"] for trade in limited["trades"]] == [1, 2, 3, 4, 5]

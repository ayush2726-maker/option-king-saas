from datetime import date

from telegram.daily_report_format import build_daily_trade_report


def test_daily_report_separates_paper_and_live_and_totals_closed_pnl():
    paper = [
        {
            "status": "CLOSED",
            "trading_mode": "paper",
            "underlying": "NIFTY",
            "net_pnl": 125.50,
        },
        {
            "status": "OPEN",
            "trading_mode": "paper",
            "underlying": "SENSEX",
            "pnl": 0,
        },
    ]
    live = [
        {
            "status": "closed",
            "trading_mode": "live",
            "underlying": "SENSEX",
            "pnl": -25.25,
        }
    ]

    message = build_daily_trade_report(paper, live, date(2026, 8, 31))

    assert "Trades: 2 | Closed: 1 | Open: 1" in message
    assert "Trades: 1 | Closed: 1 | Open: 0" in message
    assert "NIFTY: ₹+125.50" in message
    assert "SENSEX: ₹-25.25" in message
    assert "Total closed P&amp;L: <b>₹+100.25</b>" in message


def test_no_trade_day_has_clear_message():
    message = build_daily_trade_report([], [], date(2026, 8, 31))

    assert "Trades: 0 | Closed: 0 | Open: 0" in message
    assert "आज कोई trade execute नहीं हुआ।" in message


if __name__ == "__main__":
    test_daily_report_separates_paper_and_live_and_totals_closed_pnl()
    test_no_trade_day_has_clear_message()
    print("Telegram daily trade report verified")

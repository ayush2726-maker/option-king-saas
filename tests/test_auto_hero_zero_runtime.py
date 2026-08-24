import sqlite3
from datetime import datetime

from bot import auto_hero_zero_runtime as hero
from bot import routes
from bot import auto_portfolio_runtime as runtime


def _scan(score=90, side="CE", bullish=True, mtf_side="CE", underlying="NIFTY"):
    return {
        "underlying": underlying,
        "status": "OK",
        "signal_data": {
            # After 14:45 the normal expiry guard changes signal to WAIT but
            # preserves candidate_signal for the separate Hero Zero window.
            "signal": "WAIT",
            "candidate_signal": side,
            "score": score,
            "real_mtf_5m": {"available": True, "side": mtf_side},
        },
        "market_data": {
            "price": 25000.0,
            "adx": 35.0,
            "c1_bullish": bullish,
            "c2_bullish": bullish,
        },
    }


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    routes.ensure_tables(conn)
    runtime._ensure_schema(conn)
    return conn


def test_auto_hero_window_is_1430_to_1500():
    assert hero._window_open(datetime(2026, 8, 25, 14, 29)) is False
    assert hero._window_open(datetime(2026, 8, 25, 14, 30)) is True
    assert hero._window_open(datetime(2026, 8, 25, 14, 59)) is True
    assert hero._window_open(datetime(2026, 8, 25, 15, 0)) is False


def test_candidate_needs_score_90_two_candles_and_real_5m():
    assert hero._eligible_candidate(_scan()) is True
    assert hero._eligible_candidate(_scan(score=89)) is False
    assert hero._eligible_candidate(_scan(bullish=False)) is False
    assert hero._eligible_candidate(_scan(mtf_side="PE")) is False

    pe = _scan(side="PE", bullish=False, mtf_side="PE")
    assert hero._eligible_candidate(pe) is True


def test_best_candidate_uses_highest_qualified_score():
    selected = hero._best_candidate([
        _scan(score=91, underlying="NIFTY"),
        _scan(score=95, underlying="SENSEX"),
        _scan(score=89, underlying="BANKNIFTY"),
    ])
    assert selected["underlying"] == "SENSEX"


def test_picker_uses_nearest_affordable_exact_expiry_otm():
    requested = []

    def resolve(strike):
        requested.append(strike)
        return {
            "success": True,
            "symbol": f"NIFTY{strike}CE",
            "token": str(strike),
            "expiry": "2026-08-25",
            "strike": strike,
            "lot_size": 65,
        }

    premiums = {25050: 45.0, 25100: 35.0, 25150: 30.0}

    def quote(contract):
        return {"success": True, "ltp": premiums[contract["strike"]]}

    result = hero._pick_affordable_contract(
        "NIFTY",
        "CE",
        25000.0,
        datetime(2026, 8, 25).date(),
        resolve,
        quote,
        65,
    )
    assert requested == [25050, 25100, 25150]
    assert result["strike"] == 25150
    assert result["hero_premium_capital"] == 1950.0
    assert result["hero_otm_offset"] == 3


def test_insert_is_one_lot_paper_with_50pct_sl_and_2x_target():
    conn = _conn()
    scan = _scan(score=94)
    contract = {
        "symbol": "NIFTY25150CE",
        "token": "abc",
        "exchange": "NSE_FO",
        "expiry": "2026-08-25",
        "strike": 25150,
        "quote_price": 30.0,
        "lot_size": 65,
        "hero_otm_offset": 3,
        "hero_premium_capital": 1950.0,
    }
    trade_id = hero._insert_trade(
        conn, 7, "upstox", scan, contract, 120000.0
    )
    row = conn.execute(
        "SELECT * FROM paper_trades WHERE id=?", (trade_id,)
    ).fetchone()
    assert row["qty"] == 65
    assert row["lots"] == 1
    assert row["trading_mode"] == "paper"
    assert row["sl_price"] == 15.0
    assert row["target_price"] == 60.0
    assert row["capital_used"] == 1950.0
    assert row["initial_risk"] == 15.0
    assert row["reason"].startswith("AUTO HERO ZERO PAPER")
    assert hero._today_hero_count(conn, 7, hero._now_ist()) == 1

    # Closing overwrites paper_trades.reason, but the daily-attempt ledger must
    # still block another automatic Hero Zero on the same day.
    conn.execute(
        "UPDATE paper_trades SET status='CLOSED', reason='TARGET HIT' WHERE id=?",
        (trade_id,),
    )
    conn.commit()
    assert hero._today_hero_count(conn, 7, hero._now_ist()) == 1


def test_auto_hero_target_and_1515_force_exit():
    trade = {
        "reason": "AUTO HERO ZERO PAPER | score=92",
        "entry_price": 30.0,
    }
    assert hero.auto_hero_zero_exit_reason(
        trade, 59.95, datetime(2026, 8, 25, 15, 14)
    ) is None
    assert hero.auto_hero_zero_exit_reason(
        trade, 60.0, datetime(2026, 8, 25, 14, 45)
    ) == "AUTO HERO ZERO TARGET 2X HIT"
    assert hero.auto_hero_zero_exit_reason(
        trade, 40.0, datetime(2026, 8, 25, 15, 15)
    ) == "AUTO HERO ZERO FORCE EXIT 15:15 IST"

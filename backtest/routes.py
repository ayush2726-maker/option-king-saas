from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from strategy.routes import DEFAULT_SETTINGS
import json
from datetime import datetime
from telegram.routes import notify_user

router = APIRouter(prefix="/backtest", tags=["Backtest"])

ALLOWED_INSTRUMENTS = ["NIFTY", "BANKNIFTY", "SENSEX"]

def get_user_settings(conn, user_id: int):
    row = conn.execute(
        "SELECT settings_json FROM strategy_settings WHERE user_id=?",
        (user_id,)
    ).fetchone()

    settings = dict(DEFAULT_SETTINGS)
    if row:
        try:
            settings.update(json.loads(row["settings_json"]))
        except Exception:
            pass
    return settings

@router.post("/run")
def run_backtest(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    body = body or {}

    instrument = str(body.get("instrument", "NIFTY")).upper()
    if instrument not in ALLOWED_INSTRUMENTS:
        instrument = "NIFTY"
    test_date = str(body.get("date", datetime.utcnow().date().isoformat()))
    capital_input = body.get("capital", None)

    conn = get_db()
    settings = get_user_settings(conn, user["id"])

    if capital_input in [None, "", 0, "0"]:
        capital = float(settings.get("paper_capital", 100000) or 100000)
    else:
        capital = float(capital_input or 100000)

    threshold = int(float(body.get("entry_threshold", settings.get("entry_threshold", 82))))
    slp = float(body.get("sl_percent", settings.get("sl_percent", 12)))
    tgt = float(body.get("target_percent", settings.get("target_percent", 24)))
    max_trades = int(float(body.get("max_trades_per_day", settings.get("max_trades_per_day", 5))))

    trades = []
    pnl_total = 0.0
    wins = 0
    losses = 0

    for i in range(max_trades):
        score = max(55, min(96, threshold + [6, -4, 8, -10, 3, -6, 10, -2][i % 8]))
        side = "CE" if i % 2 == 0 else "PE"
        entry = round(100 + i * 7.5, 2)

        if score >= threshold and i % 4 != 3:
            exit_price = round(entry * (1 + tgt / 100), 2)
            pnl = round(capital * (tgt / 100) * 0.18, 2)
            result = "WIN"
            reason = "Target / score passed"
            wins += 1
        else:
            exit_price = round(entry * (1 - slp / 100), 2)
            pnl = -round(capital * (slp / 100) * 0.16, 2)
            result = "LOSS"
            reason = "SL / score weakness"
            losses += 1

        pnl_total += pnl
        trades.append({
            "time": f"09:{25 + ((i * 7) % 35):02d}",
            "instrument": instrument,
            "side": side,
            "score": score,
            "entry": entry,
            "exit": exit_price,
            "pnl": pnl,
            "result": result,
            "reason": reason
        })

    total = len(trades)
    summary = {
        "instrument": instrument,
        "date": test_date,
        "mode": settings.get("mode", "default"),
        "capital": capital,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / total) * 100, 2) if total else 0,
        "net_pnl": round(pnl_total, 2),
        "entry_threshold": threshold,
        "sl_percent": slp,
        "target_percent": tgt,
        "note": "Abhi first SaaS simulation backtest hai. Historical candle engine baad me connect karenge."
    }

    conn.execute(
        """INSERT INTO backtest_runs
           (user_id, instrument, test_date, settings_json, summary_json, trades_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            user["id"],
            instrument,
            test_date,
            json.dumps(settings),
            json.dumps(summary),
            json.dumps(trades),
            datetime.utcnow().isoformat()
        )
    )
    conn.commit()
    conn.close()

    notify_user(
        user["id"],
        f"🧪 <b>Backtest Complete</b>\n"
        f"Instrument: {summary['instrument']}\n"
        f"Date: {summary['date']}\n"
        f"Trades: {summary['trades']}\n"
        f"Win Rate: {summary['win_rate']}%\n"
        f"Net P&L: ₹{summary['net_pnl']}"
    )

    return {"success": True, "summary": summary, "trades": trades}

@router.get("/history")
def backtest_history(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    rows = conn.execute(
        """SELECT id, instrument, test_date, summary_json, created_at
           FROM backtest_runs
           WHERE user_id=?
           ORDER BY id DESC
           LIMIT 20""",
        (user["id"],)
    ).fetchall()
    conn.close()

    history = []
    for r in rows:
        try:
            summary = json.loads(r["summary_json"])
        except Exception:
            summary = {}
        history.append({
            "id": r["id"],
            "instrument": r["instrument"],
            "test_date": r["test_date"],
            "summary": summary,
            "created_at": r["created_at"]
        })

    return {"success": True, "history": history}

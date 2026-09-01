import json
import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from auth.routes import get_current_user
from auth.utils import decrypt_credential
from bot.brokers.factory import create_broker
from database import get_db
from broker.selection import get_selected_broker
from bot.trade_mode_truth import paper_truth_sql


IST = timezone(timedelta(hours=5, minutes=30))
_FUNDS_CACHE = {}
_FUNDS_LOCK = threading.Lock()
_FUNDS_TTL_SECONDS = 30


def _number(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _load_settings(conn, user_id):
    settings = {}
    try:
        row = conn.execute(
            "SELECT settings_json FROM strategy_settings WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        if row and row["settings_json"]:
            settings = json.loads(row["settings_json"])
    except Exception:
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    settings.setdefault("trading_mode", "paper")
    settings.setdefault("paper_capital", 100000)
    return settings


def _trade_dict(row, default_mode):
    trade = dict(row)
    qty = int(trade.get("quantity") or trade.get("qty") or 0)
    status = str(trade.get("status") or "").upper()
    entry = _number(trade.get("entry_price"))
    last = trade.get("last_ltp")
    current = _number(last, entry) if last is not None else entry
    pnl = trade.get("net_pnl")
    if pnl is None:
        pnl = trade.get("pnl")
    if pnl is None and status in {"OPEN", "PENDING", "EXIT_PENDING"}:
        pnl = (current - entry) * qty
    trade.update(
        {
            "qty": qty,
            "quantity": qty,
            "trading_mode": str(trade.get("trading_mode") or default_mode).lower(),
            "current_price": round(current, 2) if current else None,
            "unrealized_pnl": round(_number(pnl), 2) if status in {"OPEN", "PENDING", "EXIT_PENDING"} else None,
            "pnl": round(_number(pnl), 2),
        }
    )
    return trade


def _live_rows(conn, user_id, active_only=False, limit=250):
    where = "AND UPPER(COALESCE(status,'')) IN ('PENDING','OPEN','EXIT_PENDING')" if active_only else ""
    try:
        rows = conn.execute(
            f"SELECT * FROM trades WHERE user_id=? {where} ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
    except Exception:
        return []
    return [_trade_dict(row, "live") for row in rows]


def _paper_rows(conn, user_id, active_only=False, limit=250):
    active = "AND UPPER(COALESCE(paper_trades.status,''))='OPEN'" if active_only else ""
    try:
        paper_filter = paper_truth_sql(conn, "paper_trades")
        rows = conn.execute(
            f"SELECT * FROM paper_trades WHERE user_id=? AND {paper_filter} {active} ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
    except Exception:
        return []
    output = [_trade_dict(row, "paper") for row in rows]
    for trade in output:
        trade["trading_mode"] = "paper"
    return output


def _realized_pnl(trades):
    total = 0.0
    for trade in trades:
        if str(trade.get("status") or "").upper() == "CLOSED":
            total += _number(trade.get("net_pnl") if trade.get("net_pnl") is not None else trade.get("pnl"))
    return round(total, 2)


def _daily_history(trades):
    buckets = {}
    for trade in trades:
        raw = trade.get("created_at") or trade.get("entry_time")
        if not raw:
            continue
        try:
            text = str(raw).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            day = dt.astimezone(IST).date().isoformat()
        except Exception:
            day = str(raw)[:10]
        item = buckets.setdefault(day, {"date": day, "trade_count": 0, "closed_count": 0, "open_count": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        item["trade_count"] += 1
        status = str(trade.get("status") or "").upper()
        pnl = _number(trade.get("net_pnl") if trade.get("net_pnl") is not None else trade.get("pnl"))
        if status == "CLOSED":
            item["closed_count"] += 1
            item["pnl"] += pnl
            if pnl > 0:
                item["wins"] += 1
            elif pnl < 0:
                item["losses"] += 1
        elif status in {"OPEN", "PENDING", "EXIT_PENDING"}:
            item["open_count"] += 1
    output = []
    for day in sorted(buckets.keys(), reverse=True):
        item = buckets[day]
        item["pnl"] = round(item["pnl"], 2)
        item["net_pnl"] = item["pnl"]
        item["display_date"] = day
        item["pnl_basis"] = "NET_AFTER_EXECUTION_COSTS"
        output.append(item)
    return output


def _broker_funds(user_id):
    now = time.monotonic()
    with _FUNDS_LOCK:
        cached = _FUNDS_CACHE.get(int(user_id))
        if cached and cached["expires_at"] > now:
            return dict(cached["value"])

    conn = get_db()
    try:
        selected = get_selected_broker(conn, int(user_id))
        if selected is None:
            return {"success": False, "message": "Broker not connected"}
        broker_name = str(selected["broker_name"] or "angelone").lower()
        broker = create_broker(
            broker_name,
            selected["client_id"],
            decrypt_credential(selected["api_key"]),
            decrypt_credential(selected["api_secret"]),
            decrypt_credential(selected["totp_secret"]) if selected["totp_secret"] else None,
        )
    finally:
        conn.close()

    login = broker.login()
    if not isinstance(login, dict) or not login.get("success"):
        value = {"success": False, "message": (login or {}).get("message", "Broker login failed") if isinstance(login, dict) else "Broker login failed"}
    else:
        value = broker.get_funds()
        if not isinstance(value, dict):
            value = {"success": False, "message": "Broker funds unavailable"}
        value = {**value, "broker": broker_name}

    with _FUNDS_LOCK:
        _FUNDS_CACHE[int(user_id)] = {"expires_at": now + _FUNDS_TTL_SECONDS, "value": dict(value)}
    return value


def _apply_signal_mode(data, user_id, mode, settings, conn):
    active = _live_rows(conn, user_id, True, 20) if mode == "live" else _paper_rows(conn, user_id, True, 20)
    history = _live_rows(conn, user_id, False, 250) if mode == "live" else _paper_rows(conn, user_id, False, 250)
    data["trading_mode"] = mode
    data["active_trades"] = active
    data["active_trade"] = active[0] if active else None
    data["open_trade_count"] = len(active)
    data["latest_trade"] = history[0] if history else None
    data["total_trades"] = len(history)
    data["total_pnl"] = _realized_pnl(history)
    data["active_trades_label"] = "Active Live Trades" if mode == "live" else "Active Paper Trades"
    data["trade_history_label"] = "Live Trade History" if mode == "live" else "Paper Trade History"
    data["history_mode"] = mode

    if mode == "paper":
        paper_capital = _number(settings.get("paper_capital"), 100000)
        open_pnl = round(sum(_number(t.get("unrealized_pnl")) for t in active), 2)
        current_capital = round(paper_capital + data["total_pnl"] + open_pnl, 2)
        data.update(
            {
                "starting_capital": paper_capital,
                "current_capital": current_capital,
                "current_equity": current_capital,
                "open_pnl": open_pnl,
                "capital_source": "PAPER_MODE_EQUITY",
            }
        )
        return data

    funds = _broker_funds(user_id)
    data["broker_funds"] = funds
    if funds.get("success"):
        available_cash = round(_number(funds.get("available_cash")), 2)
        used_margin = round(_number(funds.get("used_margin")), 2)
        data.update(
            {
                "available_cash": available_cash,
                "used_margin": used_margin,
                "live_capital": available_cash,
                "starting_capital": round(available_cash + used_margin, 2),
                "current_capital": available_cash,
                "current_equity": round(available_cash + used_margin, 2),
                "capital_source": "LIVE_BROKER_AUTO_SYNC",
                "capital_sync_ok": True,
            }
        )
    else:
        data["capital_sync_ok"] = False
        data["capital_sync_error"] = str(funds.get("message") or "Broker funds unavailable")[:160]
    return data


class ModeAwareDashboardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        watched = path in {"/bot/signal", "/history/paper", "/history/trades"}
        if request.method != "GET" or not watched:
            return await call_next(request)

        response = await call_next(request)
        if response.status_code != 200:
            return response

        try:
            raw = b"".join([chunk async for chunk in response.body_iterator])
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            return response

        try:
            user = get_current_user(request.headers.get("authorization"))
            conn = get_db()
            try:
                settings = _load_settings(conn, user["id"])
                mode = str(settings.get("trading_mode") or "paper").lower()
                mode = "live" if mode == "live" else "paper"

                if path == "/bot/signal":
                    data = _apply_signal_mode(data, user["id"], mode, settings, conn)
                elif path == "/history/paper":
                    trades = (
                        _live_rows(conn, user["id"], False, 250)
                        if mode == "live"
                        else _paper_rows(conn, user["id"], False, 250)
                    )
                    data = {
                        "success": True,
                        "paper_trades": trades,
                        "live_trades": trades if mode == "live" else [],
                        "trades": trades,
                        "count": len(trades),
                        "daily_history": _daily_history(trades),
                        "daily_count": len(_daily_history(trades)),
                        "history_view": "DAILY_DRILLDOWN_V1",
                        "history_mode": mode,
                        "trade_history_label": "Live Trade History" if mode == "live" else "Paper Trade History",
                        "pnl_basis": "NET_AFTER_EXECUTION_COSTS",
                        "timezone": "Asia/Kolkata",
                    }
                elif path == "/history/trades" and mode == "paper":
                    trades = _paper_rows(conn, user["id"], False, 250)
                    data = {
                        "success": True,
                        "trades": trades,
                        "paper_trades": trades,
                        "count": len(trades),
                        "history_mode": "paper",
                        "trade_history_label": "Paper Trade History",
                    }
            finally:
                conn.close()
        except Exception as exc:
            if isinstance(data, dict):
                data.setdefault("mode_sync_warning", str(exc)[:160])

        headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-length", "content-type"}}
        return JSONResponse(data, status_code=response.status_code, headers=headers)

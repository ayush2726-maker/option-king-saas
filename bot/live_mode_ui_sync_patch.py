import json
from datetime import datetime, timezone, timedelta

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse

from database import get_db


IST = timezone(timedelta(hours=5, minutes=30))
_PATCHED = False


def _num(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _ensure_table():
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_broker_funds (
                user_id INTEGER PRIMARY KEY,
                available_cash REAL,
                used_margin REAL,
                total_limit REAL,
                broker TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _settings(conn, user_id):
    try:
        row = conn.execute(
            "SELECT settings_json FROM strategy_settings WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        data = json.loads(row["settings_json"]) if row and row["settings_json"] else {}
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _mode(conn, user_id):
    return "live" if str(_settings(conn, user_id).get("trading_mode") or "paper").lower() == "live" else "paper"


def _snapshot(conn, user_id):
    try:
        row = conn.execute(
            "SELECT * FROM live_broker_funds WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return None
    item = dict(row)
    try:
        dt = datetime.fromisoformat(str(item.get("updated_at") or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        item["age_seconds"] = max(0, int((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()))
    except Exception:
        item["age_seconds"] = 999999
    item["fresh"] = item["age_seconds"] <= 120
    return item


def _live_trades(conn, user_id, limit=500):
    try:
        rows = conn.execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _daily_history(trades):
    buckets = {}
    for trade in trades:
        raw = trade.get("created_at") or trade.get("entry_time")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            day = dt.astimezone(IST).date().isoformat()
        except Exception:
            day = str(raw)[:10]
        item = buckets.setdefault(day, {
            "date": day,
            "display_date": day,
            "trade_count": 0,
            "closed_count": 0,
            "open_count": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "net_pnl": 0.0,
            "pnl_basis": "NET_AFTER_EXECUTION_COSTS",
        })
        item["trade_count"] += 1
        status = str(trade.get("status") or "").upper()
        pnl = _num(trade.get("net_pnl") if trade.get("net_pnl") is not None else trade.get("pnl"))
        if status == "CLOSED":
            item["closed_count"] += 1
            item["pnl"] += pnl
            if pnl > 0:
                item["wins"] += 1
            elif pnl < 0:
                item["losses"] += 1
        elif status in {"OPEN", "PENDING", "EXIT_PENDING"}:
            item["open_count"] += 1
    out = []
    for day in sorted(buckets, reverse=True):
        item = buckets[day]
        item["pnl"] = round(item["pnl"], 2)
        item["net_pnl"] = item["pnl"]
        out.append(item)
    return out


def _patch_gateway_route():
    import local_gateway.routes as gateway_routes

    for route in gateway_routes.router.routes:
        if getattr(route, "path", "") == "/local-gateway/funds-snapshot":
            return

    def gateway_funds_snapshot(body: dict, x_gateway_token: str = Header(None)):
        token = gateway_routes._gateway_token(x_gateway_token)
        gateway = gateway_routes.authenticate_gateway(token)
        available = _num((body or {}).get("available_cash"))
        used = _num((body or {}).get("used_margin"))
        total = _num((body or {}).get("total_limit"), available + used)
        if available < 0 or used < 0 or total < 0:
            raise HTTPException(status_code=400, detail="Invalid broker funds snapshot")
        now = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        try:
            _ensure_table()
            conn.execute(
                """
                INSERT INTO live_broker_funds(user_id, available_cash, used_margin, total_limit, broker, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    available_cash=excluded.available_cash,
                    used_margin=excluded.used_margin,
                    total_limit=excluded.total_limit,
                    broker=excluded.broker,
                    updated_at=excluded.updated_at
                """,
                (int(gateway["user_id"]), available, used, total, str((body or {}).get("broker") or "angelone"), now),
            )
            conn.commit()
        finally:
            conn.close()
        return {"success": True, "available_cash": available, "used_margin": used, "total_limit": total, "updated_at": now}

    gateway_routes.router.add_api_route(
        "/funds-snapshot",
        gateway_funds_snapshot,
        methods=["POST"],
        name="gateway_funds_snapshot",
    )


def _patch_dashboard_middleware():
    from auth.routes import get_current_user
    from bot.mode_aware_dashboard_middleware import ModeAwareDashboardMiddleware

    if getattr(ModeAwareDashboardMiddleware, "_live_snapshot_patch", False):
        return

    original_dispatch = ModeAwareDashboardMiddleware.dispatch

    async def dispatch(self, request, call_next):
        response = await original_dispatch(self, request, call_next)
        path = request.url.path.lower()
        if request.method != "GET" or not any(k in path for k in ("signal", "history", "report", "account")):
            return response
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
                mode = _mode(conn, user["id"])
                if mode == "live":
                    snap = _snapshot(conn, user["id"])
                    trades = _live_trades(conn, user["id"])
                    active = [t for t in trades if str(t.get("status") or "").upper() in {"OPEN", "PENDING", "EXIT_PENDING"}]
                    realized = round(sum(
                        _num(t.get("net_pnl") if t.get("net_pnl") is not None else t.get("pnl"))
                        for t in trades
                        if str(t.get("status") or "").upper() == "CLOSED"
                    ), 2)

                    if isinstance(data, dict):
                        data["trading_mode"] = "live"
                        data["history_mode"] = "live"
                        data["active_trades_label"] = "Active Live Trades"
                        data["trade_history_label"] = "Live Trade History"

                        if path.endswith("/bot/signal") or "signal" in path:
                            data["active_trades"] = active
                            data["active_trade"] = active[0] if active else None
                            data["open_trade_count"] = len(active)
                            data["total_trades"] = len(trades)
                            data["total_pnl"] = realized

                        if "history" in path:
                            daily = _daily_history(trades)
                            data["trades"] = trades
                            data["live_trades"] = trades
                            data["paper_trades"] = trades
                            data["count"] = len(trades)
                            data["daily_history"] = daily
                            data["daily_count"] = len(daily)

                        if snap and snap.get("fresh"):
                            available = round(_num(snap.get("available_cash")), 2)
                            used = round(_num(snap.get("used_margin")), 2)
                            total = round(_num(snap.get("total_limit"), available + used), 2)
                            data.update({
                                "available_cash": available,
                                "used_margin": used,
                                "live_capital": available,
                                "starting_capital": total,
                                "current_capital": available,
                                "current_equity": total,
                                "capital_source": "LOCAL_GATEWAY_ANGEL_AUTO_SYNC",
                                "capital_sync_ok": True,
                                "capital_snapshot_age_seconds": snap.get("age_seconds"),
                            })
                            if isinstance(data.get("account"), dict):
                                data["account"].update({
                                    "trading_mode": "live",
                                    "current_capital": available,
                                    "live_capital": available,
                                    "available_cash": available,
                                    "used_margin": used,
                                    "equity": total,
                                    "capital_source": "LOCAL_GATEWAY_ANGEL_AUTO_SYNC",
                                })
                        else:
                            data["capital_sync_ok"] = False
                            data["capital_source"] = "WAITING_FOR_LOCAL_GATEWAY_FUNDS"
            finally:
                conn.close()
        except Exception as exc:
            if isinstance(data, dict):
                data.setdefault("live_mode_sync_warning", str(exc)[:160])

        headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-length", "content-type"}}
        return JSONResponse(data, status_code=response.status_code, headers=headers)

    ModeAwareDashboardMiddleware.dispatch = dispatch
    ModeAwareDashboardMiddleware._live_snapshot_patch = True


def apply_live_mode_ui_sync_patch():
    global _PATCHED
    if _PATCHED:
        return
    _ensure_table()
    _patch_gateway_route()
    _patch_dashboard_middleware()
    _PATCHED = True

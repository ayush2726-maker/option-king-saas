"""LIVE /bot/signal dashboard authority from Angel broker truth.

Paper mode is untouched. In LIVE mode the home dashboard must not aggregate
paper_trades/Upstox rows. Today P&L, total P&L, trade counts and open positions
come from the Angel local-gateway-backed trades table, using the same cost model
as LIVE history.

V3 also exposes the legacy field names consumed by the AUTO Portfolio card
(`active_positions`, `ltp`, `sl`, `pnl`) directly in this authoritative response.
It also reads Current Capital at this final response boundary from the fresh
local-gateway Angel funds snapshot, removing middleware-order dependence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from fastapi.responses import JSONResponse

from auth.routes import get_current_user
from database import get_db
from bot.angel_fetcher import get_user_bot_state
from bot.authoritative_ledger import _fresh_live_funds_snapshot
from bot.live_mode_broker_truth_middleware import (
    VERSION as BROKER_TRUTH_VERSION,
    _settings_mode,
    _history_payload,
    _live_payload,
)

VERSION = "LIVE_SIGNAL_BROKER_TRUTH_V3_CURRENT_CAPITAL_20260902"


def _num(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _settings(user_id):
    import json
    data = {}
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT settings_json FROM strategy_settings WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        if row:
            try:
                data = json.loads(row["settings_json"] or "{}")
            except Exception:
                data = {}
    finally:
        conn.close()
    return data


def _running(user_id):
    conn = get_db()
    try:
        for table in ("user_bot_state", "bot_status"):
            try:
                row = conn.execute(
                    f"SELECT is_running FROM {table} WHERE user_id=?",
                    (int(user_id),),
                ).fetchone()
                if row is not None:
                    return bool(row["is_running"])
            except Exception:
                continue
    finally:
        conn.close()
    return False


def _capital_from_live_rows(user_id, open_pnl):
    """Use only broker/live-row capital proof; never paper capital in LIVE."""
    conn = get_db()
    try:
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        except Exception:
            cols = set()
        if "capital_base" not in cols:
            return None, "LIVE_BROKER_CAPITAL_UNAVAILABLE"
        row = conn.execute(
            "SELECT capital_base FROM trades WHERE user_id=? AND capital_base>0 ORDER BY id DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()
        if not row or not row["capital_base"]:
            return None, "LIVE_BROKER_CAPITAL_UNAVAILABLE"
        base = float(row["capital_base"])
        return round(base + float(open_pnl or 0), 2), "LIVE_TRADE_CAPITAL_BASE_PLUS_OPEN_PNL"
    except Exception:
        return None, "LIVE_BROKER_CAPITAL_UNAVAILABLE"
    finally:
        conn.close()


def _live_capital_payload(user_id, open_pnl, now=None):
    """Prefer the current local-gateway broker balance for LIVE display.

    ``/bot/signal`` is intercepted by this module before the older dashboard
    middleware can enrich it.  Reading the fresh gateway snapshot here keeps a
    flat LIVE account (no recent trade/capital_base row) from returning a null
    Current Capital.  Paper capital is never used as a fallback.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    conn = get_db()
    try:
        snapshot = _fresh_live_funds_snapshot(conn, int(user_id), current)
    finally:
        conn.close()

    if snapshot is not None:
        available = round(_num(snapshot.get("available_cash"), 0.0), 2)
        used = round(_num(snapshot.get("used_margin"), 0.0), 2)
        total = round(
            _num(snapshot.get("total_limit"), available + used),
            2,
        )
        return {
            "starting_capital": total,
            "current_capital": available,
            "current_equity": total,
            "live_capital": available,
            "available_cash": available,
            "used_margin": used,
            "broker_total_limit": total,
            "capital_source": "LOCAL_GATEWAY_ANGEL_FRESH_SNAPSHOT",
            "capital_sync_ok": True,
            "broker_funds_updated_at": snapshot.get("updated_at"),
            "broker_funds_age_seconds": snapshot.get("age_seconds"),
        }

    current_capital, source = _capital_from_live_rows(user_id, open_pnl)
    return {
        "starting_capital": current_capital,
        "current_capital": current_capital,
        "current_equity": current_capital,
        "live_capital": current_capital,
        "available_cash": None,
        "used_margin": None,
        "broker_total_limit": None,
        "capital_source": source,
        "capital_sync_ok": False,
        "capital_sync_error": "Waiting for fresh local-gateway broker funds",
        "broker_funds_updated_at": None,
        "broker_funds_age_seconds": None,
    }


def _auto_compat_position(position):
    """Expose one canonical LIVE position under every legacy AUTO-card alias."""
    item = dict(position or {})
    ltp = _num(
        item.get("live_price")
        if item.get("live_price") is not None
        else item.get("current_price")
        if item.get("current_price") is not None
        else item.get("last_ltp"),
        0.0,
    )
    entry = _num(item.get("entry_price"), 0.0)
    qty = int(_num(item.get("qty") if item.get("qty") is not None else item.get("quantity"), 0))
    sl = _num(item.get("sl_price") if item.get("sl_price") is not None else item.get("sl"), 0.0)
    net = item.get("net_pnl")
    if net is None:
        net = item.get("unrealized_pnl")
    if net is None:
        net = item.get("pnl")
    if net is None and ltp > 0 and entry > 0 and qty > 0:
        net = (ltp - entry) * qty
    net = _num(net, 0.0)

    item["qty"] = qty
    item["quantity"] = qty
    if entry > 0:
        item["entry"] = round(entry, 2)
        item["entry_price"] = round(entry, 2)
    if ltp > 0:
        for key in ("ltp", "live_price", "current_price", "last_ltp", "last_price"):
            item[key] = round(ltp, 2)
    if sl > 0:
        for key in ("sl", "sl_price", "live_sl"):
            item[key] = round(sl, 2)
    for key in ("pnl", "net_pnl", "unrealized_pnl", "profit_loss", "live_pnl"):
        item[key] = round(net, 2)
    item["display_source"] = "ANGEL_LIVE_SIGNAL_NATIVE_AUTO_V2"
    return item


def _payload(user_id):
    settings = _settings(user_id)
    history = _history_payload(user_id)
    live = _live_payload(user_id)
    today = dict(history.get("today") or {})
    ledger = dict(history.get("ledger") or {})
    active = [
        _auto_compat_position(x)
        for x in list(live.get("trades") or live.get("open_positions") or [])
    ]
    running = _running(user_id)

    try:
        engine = dict(get_user_bot_state(user_id) or {})
    except Exception:
        engine = {}

    score = int(float(engine.get("score") or 0))
    signal_raw = str(engine.get("signal") or "WAIT")
    min_score = int(float(engine.get("min_score") or settings.get("entry_threshold") or 82))
    signal = ("READY_" + signal_raw) if signal_raw in {"CE", "PE"} and score >= min_score else "WAITING"
    if active:
        signal = "HOLD_" + str(active[0].get("side") or "")

    capital = _live_capital_payload(
        user_id, ledger.get("open_pnl", 0)
    )
    account = {
        "trading_mode": "live",
        "broker": "angelone",
        "current_capital": capital.get("current_capital"),
        "live_capital": capital.get("live_capital"),
        "available_cash": capital.get("available_cash"),
        "used_margin": capital.get("used_margin"),
        "current_equity": capital.get("current_equity"),
        "equity": capital.get("current_equity"),
        "capital_source": capital.get("capital_source"),
        "capital_sync_ok": capital.get("capital_sync_ok"),
    }

    first = active[0] if active else None
    return {
        "success": True,
        "running": running,
        "is_running": running,
        "status": "LIVE_RUNNING" if running else "LIVE_WAITING",
        "trading_mode": "live",
        "mode": "live",
        "broker": "angelone",
        "broker_name": "angelone",
        "signal": signal,
        "last_signal": signal,
        "score": score,
        "tqu_score": score,
        "min_score": min_score,
        "adx": float(engine.get("adx") or 0),
        "volume_ratio": float(engine.get("volume_ratio") or 0),
        "mtf": "OK" if engine.get("mtf_confirmed") else "WEAK",
        "mtf_status": "OK" if engine.get("mtf_confirmed") else "WEAK",
        "scan_results": engine.get("scan_results") or [],
        "engine_mode": engine.get("engine_mode"),
        "strategy_profile_name": engine.get("strategy_profile_name") or "OKAI Default 82",
        "entry_guard": engine.get("entry_guard"),
        "entry_attempt": engine.get("entry_attempt") or engine.get("last_entry_attempt"),
        "entry_block_reason": engine.get("entry_block_reason") or engine.get("last_entry_block_reason"),
        "entry_permission": engine.get("entry_permission"),
        "entry_sizing": engine.get("entry_sizing"),
        "position_size_block": engine.get("position_size_block"),
        "active_trade": first,
        "active_trades": active,
        "active_positions": active,
        "open_positions": active,
        "portfolio_positions": active,
        "open_trade_count": len(active),
        "trade_symbol": first.get("symbol") if first else None,
        "trade_side": first.get("side") if first else None,
        "trade_qty": first.get("qty") if first else None,
        "entry_price": first.get("entry_price") if first else None,
        "ltp": first.get("ltp") if first else None,
        "current_price": first.get("current_price") if first else None,
        "live_price": first.get("live_price") if first else None,
        "sl": first.get("sl") if first else None,
        "sl_price": first.get("sl_price") if first else None,
        "target_price": first.get("target_price") if first else None,
        "trade_pnl": first.get("net_pnl") if first else None,
        "pnl": first.get("net_pnl") if first else None,
        "unrealized_pnl": first.get("net_pnl") if first else None,
        "today": today,
        "today_pnl": round(float(today.get("total_pnl") or 0), 2),
        "today_net_pnl": round(float(today.get("total_pnl") or 0), 2),
        "today_trades": int(today.get("trades") or 0),
        "today_closed_pnl": round(float(today.get("closed_pnl") or 0), 2),
        "today_open_pnl": round(float(today.get("open_pnl") or 0), 2),
        "execution_cost": round(float(today.get("execution_cost") or 0), 2),
        "total_trades": int(ledger.get("total_trades") or history.get("count") or 0),
        "total_pnl": round(float(ledger.get("total_pnl") or 0), 2),
        "realized_pnl": round(float(ledger.get("realized_pnl") or 0), 2),
        "open_pnl": round(float(ledger.get("open_pnl") or 0), 2),
        "ledger": ledger,
        **capital,
        "capital": capital.get("current_capital"),
        "account": account,
        "source": "ANGEL_LIVE_SIGNAL_BROKER_TRUTH_NATIVE_AUTO_V3",
        "broker_truth_version": BROKER_TRUTH_VERSION,
        "version": VERSION,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


class LiveSignalBrokerTruthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "GET" or (scope.get("path") or "") != "/bot/signal":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin1").lower(): v.decode("latin1")
            for k, v in scope.get("headers", [])
        }
        try:
            user = get_current_user(headers.get("authorization"))
        except Exception:
            await self.app(scope, receive, send)
            return

        if _settings_mode(user["id"]) != "live":
            await self.app(scope, receive, send)
            return

        try:
            response = JSONResponse(
                _payload(user["id"]),
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "X-OKAI-Live-Signal-Authority": VERSION,
                    "X-OKAI-Live-Broker": "angelone",
                },
            )
        except Exception as exc:
            response = JSONResponse(
                {
                    "success": False,
                    "message": "Angel LIVE signal dashboard unavailable: " + str(exc)[:180],
                    "version": VERSION,
                },
                status_code=500,
            )
        await response(scope, receive, send)


def install(app):
    app.add_middleware(LiveSignalBrokerTruthMiddleware)
    print(f"LIVE SIGNAL BROKER TRUTH INSTALLED | {VERSION}")

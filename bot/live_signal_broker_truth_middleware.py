"""LIVE /bot/signal dashboard authority from Angel broker truth.

Paper mode is untouched. In LIVE mode the home dashboard must not aggregate
paper_trades/Upstox rows. Today P&L, total P&L, trade counts and open positions
come from the Angel local-gateway-backed trades table, using the same cost model
as LIVE history.
"""
from __future__ import annotations

from datetime import datetime, timezone
from fastapi.responses import JSONResponse

from auth.routes import get_current_user
from database import get_db
from bot.angel_fetcher import get_user_bot_state
from bot.live_mode_broker_truth_middleware import (
    VERSION as BROKER_TRUTH_VERSION,
    _settings_mode,
    _history_payload,
    _live_payload,
)

VERSION = "LIVE_SIGNAL_BROKER_TRUTH_V1_20260901"


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


def _payload(user_id):
    settings = _settings(user_id)
    history = _history_payload(user_id)
    live = _live_payload(user_id)
    today = dict(history.get("today") or {})
    ledger = dict(history.get("ledger") or {})
    active = list(live.get("trades") or live.get("open_positions") or [])
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

    current_capital, capital_source = _capital_from_live_rows(
        user_id, ledger.get("open_pnl", 0)
    )

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
        "open_positions": active,
        "open_trade_count": len(active),
        "trade_symbol": first.get("symbol") if first else None,
        "trade_side": first.get("side") if first else None,
        "trade_qty": first.get("qty") if first else None,
        "entry_price": first.get("entry_price") if first else None,
        "current_price": first.get("current_price") if first else None,
        "live_price": first.get("live_price") if first else None,
        "sl_price": first.get("sl_price") if first else None,
        "target_price": first.get("target_price") if first else None,
        "trade_pnl": first.get("net_pnl") if first else None,
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
        "current_capital": current_capital,
        "current_equity": current_capital,
        "capital_source": capital_source,
        "source": "ANGEL_LIVE_SIGNAL_BROKER_TRUTH",
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

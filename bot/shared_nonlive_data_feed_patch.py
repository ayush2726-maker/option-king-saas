"""Use the owner's broker only as a market-data source for non-live features.

PAPER runtime, display quotes/sector rotation, and backtests may read market data
through the active admin/owner broker when the user has no personal broker.
LIVE order execution is intentionally untouched and still requires the user's own
broker credentials.
"""

from __future__ import annotations

from datetime import datetime

from auth.utils import decrypt_credential
from database import get_db


def _row_value(row, key, default=None):
    try:
        value = row[key]
        return default if value is None else value
    except Exception:
        return default


def _owner_broker(conn, requester_user_id: int):
    # Reuse the same owner-selection rules as the PAPER shared-feed patch.
    from bot.shared_trial_paper_feed_patch import _selected_owner_broker

    return _selected_owner_broker(conn, int(requester_user_id))


def _personal_broker(conn, user_id: int):
    return conn.execute(
        """SELECT * FROM broker_credentials
           WHERE user_id=? AND is_active=1
           ORDER BY last_connected DESC, id DESC
           LIMIT 1""",
        (int(user_id),),
    ).fetchone()


def _credentials(row):
    return {
        "client_id": row["client_id"],
        "api_key": decrypt_credential(row["api_key"]),
        "password": decrypt_credential(row["api_secret"]),
        "totp_secret": (
            decrypt_credential(row["totp_secret"])
            if _row_value(row, "totp_secret")
            else None
        ),
    }


def _effective_data_broker(user_id: int, owner_first: bool = False):
    """Return (row, source) for market-data-only work.

    Backtests intentionally prefer the owner feed so every SaaS user sees the
    same historical data source. Display/PAPER views keep a user's own connected
    broker when present and otherwise fall back to the owner feed.
    """
    conn = get_db()
    try:
        personal = _personal_broker(conn, int(user_id))
        owner = _owner_broker(conn, int(user_id))
        if owner_first:
            if owner:
                return owner, "shared_owner"
            if personal:
                return personal, "personal_fallback"
        else:
            if personal:
                return personal, "personal"
            if owner:
                return owner, "shared_owner"
        return None, None
    finally:
        conn.close()


def _login_backtest_broker(routes, row):
    broker_name = str(_row_value(row, "broker_name", "angelone") or "angelone").lower()
    creds = _credentials(row)
    if broker_name == "angelone":
        obj = routes.angel_login(creds)
    else:
        obj = routes.create_broker(
            broker_name,
            creds["client_id"],
            creds["api_key"],
            creds["password"],
            creds.get("totp_secret"),
        )
        login_result = obj.login()
        if not login_result.get("success"):
            raise RuntimeError(
                "Broker login failed: " + str(login_result.get("message") or "")[:150]
            )
    return broker_name, obj


def _replace_route_endpoint(router, path: str, method: str, endpoint) -> None:
    wanted = method.upper()
    for route in router.routes:
        if getattr(route, "path", None) != path:
            continue
        if wanted not in getattr(route, "methods", set()):
            continue
        route.endpoint = endpoint
        if getattr(route, "dependant", None) is not None:
            route.dependant.call = endpoint


def _patch_market_display_feed() -> None:
    from bot import market_routes
    from bot import sector_rotation_routes

    if getattr(market_routes, "_okai_shared_nonlive_market_feed_v1", False):
        return

    original = market_routes._get_active_broker

    def shared_aware_get_active_broker(user_id):
        broker_name, creds = original(user_id)
        if creds:
            return broker_name, creds

        row, source = _effective_data_broker(int(user_id), owner_first=False)
        if not row or source != "shared_owner":
            return None, None

        return str(row["broker_name"] or "angelone").lower(), _credentials(row)

    market_routes._get_active_broker = shared_aware_get_active_broker
    # sector_rotation_routes imported the function directly, so update its local
    # binding too.
    sector_rotation_routes._get_active_broker = shared_aware_get_active_broker
    market_routes._okai_shared_nonlive_market_feed_v1 = True


def _patch_daily_backtest() -> None:
    from backtest import routes

    if getattr(routes, "_okai_shared_owner_backtest_feed_v1", False):
        return

    original = routes.run_backtest

    def shared_owner_run_backtest(body: dict, authorization: str = None):
        # If a personal broker exists, the original route is safe and preserves
        # every existing backtest wrapper. If it does not, use the owner's broker
        # by temporarily executing the same route logic through a minimal clone.
        user = routes.get_current_user(authorization)
        row, source = _effective_data_broker(int(user["id"]), owner_first=True)
        if not row:
            return {
                "success": False,
                "message": "Backtest market-data feed unavailable. Admin broker ko reconnect karein.",
                "personal_broker_required": False,
            }

        # When the chosen row belongs to the user, keep all existing wrappers.
        if int(_row_value(row, "user_id", -1) or -1) == int(user["id"]):
            return original(body, authorization)

        conn = routes.get_db()
        try:
            routes.ensure_backtest_table(conn)
            payload = body or {}
            instrument = payload.get("instrument") or payload.get("primary_instrument") or "NIFTY"
            run_date = payload.get("date") or payload.get("run_date") or datetime.utcnow().date().isoformat()
            capital = float(payload.get("capital") or payload.get("paper_capital") or 100000)
            entry_score = int(payload.get("entry_score") or payload.get("entry_threshold") or 82)
            sl_percent = float(payload.get("sl_percent") or 12)
            target_percent = float(payload.get("target_percent") or 24)
            strategy_mode = str(payload.get("strategy_mode") or "NORMAL").upper()

            if routes.is_weekend(run_date):
                return {"success": False, "message": "Market holiday / weekend. No backtest for this date."}
            if not routes.ENGINE_AVAILABLE:
                return {"success": False, "message": "Backtest engine unavailable on server."}

            try:
                broker_name, obj = _login_backtest_broker(routes, row)
            except Exception as exc:
                return {
                    "success": False,
                    "message": "Shared backtest feed login failed: " + str(exc)[:150],
                    "personal_broker_required": False,
                }

            raw_result = routes._okai_run_backtest_mode(
                broker_name=broker_name,
                obj=obj,
                instrument=instrument,
                date_str=run_date,
                capital=capital,
                entry_threshold=82,
                sl_percent=sl_percent,
                target_percent=target_percent,
                strategy_mode=strategy_mode,
            )
            json_stats = {"non_finite": 0}
            result = routes._json_safe(raw_result, json_stats)
            if isinstance(result, dict):
                result["debug_sanitized_non_finite"] = json_stats["non_finite"]
                result["market_data_source"] = "shared_owner_backtest_feed"
                result["data_broker"] = broker_name
                result["personal_broker_required"] = False
            routes.json.dumps(result, allow_nan=False)
            if not isinstance(result, dict) or not result.get("success"):
                return result

            conn.execute(
                """INSERT INTO backtest_runs
                   (user_id, instrument, run_date, capital, entry_score, sl_percent, target_percent, result_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    int(user["id"]), instrument, run_date, capital, entry_score,
                    sl_percent, target_percent,
                    routes.json.dumps(result, allow_nan=False),
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()

            try:
                routes.notify_user(
                    int(user["id"]),
                    "\n".join([
                        "📊 <b>Backtest Complete (Shared Market Data)</b>",
                        f"Instrument: {instrument}",
                        f"Strategy: {result.get('strategy_mode', strategy_mode)}",
                        f"Date: {run_date}",
                        f"Trades: {result.get('total_trades', 0)}",
                        f"Wins/Losses: {result.get('wins', 0)}/{result.get('losses', 0)}",
                        f"P&L: Rs {result.get('total_pnl', 0)}",
                    ]),
                )
            except Exception:
                pass
            return result
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "message": "Backtest run failed, but error is now visible.",
            }
        finally:
            conn.close()

    routes.run_backtest = shared_owner_run_backtest
    _replace_route_endpoint(routes.router, "/backtest/run", "POST", shared_owner_run_backtest)
    routes._okai_shared_owner_backtest_feed_v1 = True


def _patch_range_backtest() -> None:
    from backtest import range_routes
    from backtest import routes

    if getattr(range_routes, "_okai_shared_owner_range_feed_v1", False):
        return

    original = range_routes._connect_broker

    def shared_owner_connect_broker(user_id: int):
        row, _source = _effective_data_broker(int(user_id), owner_first=True)
        if not row:
            return original(user_id)
        return _login_backtest_broker(routes, row)

    range_routes._connect_broker = shared_owner_connect_broker
    range_routes._okai_shared_owner_range_feed_v1 = True


def apply_shared_nonlive_data_feed_patch() -> None:
    """Apply owner-feed fallbacks without changing any LIVE order path."""
    from bot.shared_trial_paper_feed_patch import apply_shared_trial_paper_feed_patch

    apply_shared_trial_paper_feed_patch()
    _patch_market_display_feed()
    _patch_daily_backtest()
    _patch_range_backtest()

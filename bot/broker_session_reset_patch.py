"""Clear every per-user broker runtime/session after broker selection changes."""

from __future__ import annotations

from fastapi import Header

from bot import angel_fetcher
from bot import shared_trial_paper_feed_patch as shared_paper_feed
from bot.completed_candle_direction_patch import (
    apply_completed_candle_direction_patch,
)
from bot.live_net_pnl_breakeven_patch import (
    apply_live_net_pnl_breakeven_patch,
)
from bot.net_pnl_history_patch import install_net_pnl_history_patch
from bot.shared_trial_paper_feed_patch import (
    apply_shared_trial_paper_feed_patch,
)
from bot.capital_continuity_patch import (
    apply_capital_continuity_patch,
)


def _install_shared_paper_owner_first_policy() -> None:
    """Never let a saved/expired user token shadow the owner's PAPER feed.

    Normal users run PAPER on the admin's selected market-data broker. Their own
    saved broker remains available for LIVE mode, but it must not be selected by
    the shared PAPER wrapper merely because an old row is still marked active.
    """
    if getattr(shared_paper_feed, "_okai_shared_paper_owner_first_v1", False):
        return

    original_selected_personal_broker = shared_paper_feed._selected_personal_broker

    def owner_first_selected_personal_broker(conn, user_id: int):
        try:
            if shared_paper_feed._eligible_shared_user(conn, int(user_id)):
                owner = shared_paper_feed._selected_owner_broker(
                    conn,
                    int(user_id),
                )
                if owner:
                    # Returning None makes the existing shared PAPER start path
                    # use the owner's feed. LIVE mode is handled before this path.
                    return None
        except Exception:
            # Preserve the previous behaviour if eligibility lookup itself fails.
            pass

        return original_selected_personal_broker(conn, int(user_id))

    shared_paper_feed._selected_personal_broker = (
        owner_first_selected_personal_broker
    )
    shared_paper_feed._okai_shared_paper_owner_first_v1 = True


def _install_shared_paper_owner_first_start_route() -> None:
    """Start normal users' PAPER engines directly from the owner broker.

    The older route first called the legacy PAPER start function. That legacy
    function queried the user's saved broker again, so an expired Upstox token
    could still be attempted before the shared owner feed. This route bypasses
    that personal-token attempt entirely for eligible PAPER users.
    """
    from database import get_db
    from bot import routes

    if getattr(routes, "_okai_shared_paper_owner_first_start_v1", False):
        return

    previous_start = routes.bot_start

    def owner_first_paper_start(authorization: str = Header(None)):
        user = routes.get_current_user(authorization)
        user_id = int(user["id"])
        owner = None
        settings = None
        trading_mode = "paper"

        conn = get_db()
        try:
            routes.ensure_tables(conn)
            settings = routes.get_strategy_settings(conn, user_id)
            trading_mode = str(
                settings.get("trading_mode", "paper") or "paper"
            ).lower()
            if (
                trading_mode == "paper"
                and shared_paper_feed._eligible_shared_user(conn, user_id)
            ):
                owner = shared_paper_feed._selected_owner_broker(conn, user_id)
        finally:
            conn.close()

        # Admin PAPER and every LIVE account keep their existing personal-broker
        # behaviour. Also preserve the old error when the owner feed is absent.
        if trading_mode == "live" or not owner:
            return previous_start(authorization)

        conn = get_db()
        try:
            routes.ensure_tables(conn)
            routes.save_bot_status(conn, user_id, 1, "PAPER_MODE")
        finally:
            conn.close()

        try:
            # Remove a stale runtime that may have been created with the user's
            # expired personal token by an older server build.
            try:
                angel_fetcher.stop_user_bot(user_id)
            except Exception:
                pass

            result, broker_name = shared_paper_feed._start_from_row(
                angel_fetcher,
                user_id,
                owner,
                "shared_owner_paper",
            )
            state = shared_paper_feed._mark_shared_state(
                angel_fetcher,
                user_id,
                broker_name,
            )
            started = bool(state.get("running")) or bool(
                isinstance(result, dict)
                and (
                    result.get("success")
                    or result.get("message") == "Bot already running"
                )
            )
            if not started:
                raise RuntimeError(
                    str(
                        (result or {}).get("message")
                        or "Shared engine start failed"
                    )
                )
        except Exception as exc:
            shared_paper_feed._set_persisted_stopped(routes, user_id)
            return {
                "success": False,
                "message": "Shared PAPER feed start failed: " + str(exc)[:160],
                "mode": "paper",
                "shared_paper_feed": False,
                "real_orders": False,
            }

        try:
            routes.notify_user(
                user_id,
                "\n".join(
                    [
                        "📝 <b>Paper Bot Started</b>",
                        "Mode: PAPER",
                        f"Market Data: Shared {broker_name.upper()} feed",
                        f"Paper Capital: ₹{settings.get('paper_capital', 100000)}",
                        "Real orders OFF.",
                    ]
                ),
            )
        except Exception:
            pass

        return {
            "success": True,
            "message": (
                f"PAPER bot started on shared {broker_name.upper()} market data. "
                "User ka personal token use nahi hua; real orders OFF."
            ),
            "mode": "paper",
            "paper_capital": settings.get("paper_capital", 100000),
            "primary_instrument": settings.get("primary_instrument", "NIFTY"),
            "enabled_instruments": settings.get(
                "enabled_instruments",
                ["NIFTY"],
            ),
            "shared_paper_feed": True,
            "market_data_source": "shared_owner_paper_feed",
            "data_broker": broker_name,
            "real_orders": False,
            "personal_broker_required": False,
        }

    routes.bot_start = owner_first_paper_start

    # FastAPI's decorator has already created the APIRoute object. Replace both
    # call references before main.py includes the router in the application.
    for route in routes.router.routes:
        if (
            getattr(route, "path", None) == "/bot/start"
            and "POST" in getattr(route, "methods", set())
        ):
            route.endpoint = owner_first_paper_start
            if getattr(route, "dependant", None) is not None:
                route.dependant.call = owner_first_paper_start

    routes._okai_shared_paper_owner_first_start_v1 = True


def _install_shared_paper_owner_first_recovery() -> None:
    """Recover persisted PAPER bots on the owner feed after a server restart."""
    from database import get_db
    from bot import routes

    if getattr(routes, "_okai_shared_paper_owner_first_recovery_v1", False):
        return

    previous_recovery = routes._start_saved_runtime_engine

    def owner_first_recovery(user_id: int):
        uid = int(user_id)
        owner = None

        conn = get_db()
        try:
            routes.ensure_tables(conn)
            settings = routes.get_strategy_settings(conn, uid)
            trading_mode = str(
                settings.get("trading_mode", "paper") or "paper"
            ).lower()

            should_recover = routes._persisted_bot_should_run(conn, uid)
            if (
                trading_mode == "paper"
                and should_recover
                and shared_paper_feed._eligible_shared_user(conn, uid)
            ):
                owner = shared_paper_feed._selected_owner_broker(conn, uid)
        except Exception:
            owner = None
        finally:
            conn.close()

        if not owner:
            return previous_recovery(uid)

        current = angel_fetcher.get_user_bot_state(uid)
        if current.get("running") and current.get("shared_paper_feed"):
            return {
                "state": current,
                "started": False,
                "reason": None,
                "shared_paper_feed": True,
            }

        try:
            # Remove any stale personal-broker runtime left from an older build.
            try:
                angel_fetcher.stop_user_bot(uid)
            except Exception:
                pass

            result, broker_name = shared_paper_feed._start_from_row(
                angel_fetcher,
                uid,
                owner,
                "shared_owner_paper",
            )
            state = shared_paper_feed._mark_shared_state(
                angel_fetcher,
                uid,
                broker_name,
            )
            started = bool(state.get("running")) or bool(
                isinstance(result, dict)
                and (
                    result.get("success")
                    or result.get("message") == "Bot already running"
                )
            )
            return {
                "state": state,
                "started": started,
                "reason": (
                    None
                    if started
                    else str(
                        (result or {}).get("message")
                        or "SHARED_ENGINE_START_FAILED"
                    )[:160]
                ),
                "shared_paper_feed": started,
            }
        except Exception as exc:
            return {
                "state": angel_fetcher.get_user_bot_state(uid),
                "started": False,
                "reason": "SHARED_PAPER_FEED_FAILED: " + str(exc)[:120],
                "shared_paper_feed": False,
            }

    routes._start_saved_runtime_engine = owner_first_recovery
    routes._okai_shared_paper_owner_first_recovery_v1 = True


def apply_broker_session_reset_patch() -> None:
    # These accounting/direction patches were previously present but could be
    # skipped after later startup refactors. Install them before every runtime
    # wrapper so Paper, Live and UI history use one consistent source of truth.
    apply_live_net_pnl_breakeven_patch()
    apply_completed_candle_direction_patch()
    install_net_pnl_history_patch()
    apply_capital_continuity_patch()

    # Install owner-first PAPER selection before the existing shared-feed route
    # wrapper is created. The function lookup is dynamic, so this also repairs an
    # already-created wrapper during hot reloads.
    _install_shared_paper_owner_first_policy()
    apply_shared_trial_paper_feed_patch()
    _install_shared_paper_owner_first_start_route()
    _install_shared_paper_owner_first_recovery()

    if getattr(angel_fetcher, "_okai_broker_session_reset_v2", False):
        apply_capital_continuity_patch()
        return

    def reset_user_broker_runtime(user_id: int):
        uid = int(user_id)

        # Stop PAPER/LIVE strategy loop bound to the old credentials.
        try:
            angel_fetcher.stop_user_bot(uid)
        except Exception:
            pass

        # Clear Angel lightweight quote session.
        try:
            with angel_fetcher._ltp_lock:
                angel_fetcher._ltp_sessions.pop(uid, None)
        except Exception:
            pass

        try:
            angel_fetcher._entry_guard_state.pop(uid, None)
        except Exception:
            pass

        # Clear chart/status quote sessions for all brokers. This is essential
        # when the same broker's daily token/credentials are refreshed because
        # the cache key otherwise remains unchanged.
        try:
            from bot import market_routes

            with market_routes._multi_sessions_lock:
                stale_keys = [
                    key
                    for key in market_routes._multi_sessions
                    if int(key[0]) == uid
                ]
                for key in stale_keys:
                    market_routes._multi_sessions.pop(key, None)

            with market_routes._quote_cache_lock:
                stale_quote_keys = [
                    key
                    for key in market_routes._quote_cache
                    if int(key[0]) == uid
                ]
                for key in stale_quote_keys:
                    market_routes._quote_cache.pop(key, None)
        except Exception:
            pass

        return {
            "success": True,
            "message": "Broker runtime, chart and quote sessions reset",
        }

    angel_fetcher.reset_user_broker_runtime = reset_user_broker_runtime
    angel_fetcher._okai_broker_session_reset_v1 = True
    angel_fetcher._okai_broker_session_reset_v2 = True

    # New trial/testing users can start isolated PAPER bots using the owner's
    # selected broker only as a shared market-data source. LIVE remains personal.
    apply_capital_continuity_patch()

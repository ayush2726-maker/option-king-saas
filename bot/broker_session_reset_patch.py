"""Clear every per-user broker runtime/session after broker selection changes."""

from __future__ import annotations

from bot import angel_fetcher


def apply_broker_session_reset_patch() -> None:
    if getattr(angel_fetcher, "_okai_broker_session_reset_v2", False):
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

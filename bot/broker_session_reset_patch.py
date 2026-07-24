"""Clear all per-user broker runtime/session caches after broker selection changes."""

from __future__ import annotations

from bot import angel_fetcher


def apply_broker_session_reset_patch() -> None:
    if getattr(angel_fetcher, "_okai_broker_session_reset_v1", False):
        return

    def reset_user_broker_runtime(user_id: int):
        uid = int(user_id)
        try:
            angel_fetcher.stop_user_bot(uid)
        except Exception:
            pass

        try:
            with angel_fetcher._ltp_lock:
                angel_fetcher._ltp_sessions.pop(uid, None)
        except Exception:
            pass

        try:
            angel_fetcher._entry_guard_state.pop(uid, None)
        except Exception:
            pass

        return {
            "success": True,
            "message": "Broker runtime and quote session reset",
        }

    angel_fetcher.reset_user_broker_runtime = reset_user_broker_runtime
    angel_fetcher._okai_broker_session_reset_v1 = True

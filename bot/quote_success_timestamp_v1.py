"""Make quote freshness follow the actual successful broker quote read.

The UI's STALE badge is based on paper_trades.quote_updated_at. Some later exit
wrappers can replace _update_open, so relying on _update_open to stamp quote time
can leave a genuinely fresh LTP marked STALE. This patch stamps quote freshness
inside _manage_rows immediately after quote_fetcher succeeds, before exit logic.
It changes no entry, SL, sizing, or order rule.
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone

_PATCHED = False
_LOCK = threading.Lock()


def _stamp(conn, trade, broker_name):
    try:
        conn.execute(
            """
            UPDATE paper_trades
            SET quote_updated_at=?, quote_source=?,
                quote_failed_at=NULL, quote_error=NULL,
                quote_failure_count=0
            WHERE id=? AND status='OPEN'
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                f"{str(broker_name or 'broker').upper()}_QUOTE_SUCCESS",
                trade["id"],
            ),
        )
        conn.commit()
    except Exception:
        pass


def _patch(runtime):
    global _PATCHED
    with _LOCK:
        if _PATCHED:
            return
        original = getattr(runtime, "_manage_rows", None)
        if not callable(original):
            return

        def manage_rows_with_quote_timestamp(
            conn, user_id, rows, scans, quote_fetcher, live_order, state
        ):
            def stamped_fetch(trade):
                result = quote_fetcher(trade)
                try:
                    ok = isinstance(result, dict) and result.get("success")
                    ltp = float((result or {}).get("ltp") or 0)
                except Exception:
                    ok = False
                    ltp = 0.0
                if ok and ltp > 0:
                    _stamp(conn, trade, getattr(state, "get", lambda *_: None)("broker") or runtime._v(trade, "broker_name", "broker"))
                return result

            return original(
                conn, user_id, rows, scans, stamped_fetch, live_order, state
            )

        runtime._manage_rows = manage_rows_with_quote_timestamp
        runtime.QUOTE_SUCCESS_TIMESTAMP_PATCH = "V1"
        _PATCHED = True


def _wait():
    for _ in range(240):
        runtime = sys.modules.get("bot.auto_portfolio_runtime")
        if runtime is not None:
            try:
                _patch(runtime)
                if _PATCHED:
                    return
            except Exception:
                pass
        time.sleep(0.25)


def schedule_quote_success_timestamp_patch():
    threading.Thread(
        target=_wait,
        name="okai-quote-success-timestamp",
        daemon=True,
    ).start()

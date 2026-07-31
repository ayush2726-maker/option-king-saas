"""Run the one-time far-expiry PAPER cleanup after application startup."""

from __future__ import annotations

import threading
import time


_started = False
_lock = threading.Lock()


def schedule_far_expiry_cleanup() -> bool:
    global _started
    with _lock:
        if _started:
            return False
        _started = True

    def worker() -> None:
        # bot package imports before FastAPI startup initializes every table.
        # Retry briefly, then run idempotently; archived trade IDs prevent repeats.
        last_error = ""
        for attempt in range(1, 13):
            try:
                if attempt == 1:
                    time.sleep(3)
                from bot.far_expiry_trade_cleanup import (
                    cleanup_invalid_far_expiry_paper_trades,
                )

                result = cleanup_invalid_far_expiry_paper_trades()
                print(
                    "Far-expiry PAPER cleanup | "
                    f"removed={result['removed']} | "
                    f"users={result['affected_users']} | "
                    f"pnl={result['removed_recorded_pnl']} | "
                    f"by_index={result['by_underlying']} | "
                    f"version={result['cleanup_version']}"
                )
                return
            except Exception as exc:
                last_error = str(exc)[:240]
                time.sleep(5)

        print(f"Far-expiry PAPER cleanup failed after retries: {last_error}")

    threading.Thread(
        target=worker,
        name="okai-far-expiry-paper-cleanup",
        daemon=True,
    ).start()
    return True

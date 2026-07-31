"""Run one-time PAPER cleanup jobs after application startup."""

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
        # Retry briefly, then run both idempotent archival cleanups.
        last_error = ""
        for attempt in range(1, 13):
            try:
                if attempt == 1:
                    time.sleep(3)

                from bot.far_expiry_trade_cleanup import (
                    cleanup_invalid_far_expiry_paper_trades,
                )
                from bot.proven_invalid_trade_cleanup import (
                    cleanup_proven_invalid_paper_trades,
                )

                far = cleanup_invalid_far_expiry_paper_trades()
                invalid = cleanup_proven_invalid_paper_trades()

                print(
                    "Far-expiry PAPER cleanup | "
                    f"removed={far['removed']} | "
                    f"users={far['affected_users']} | "
                    f"pnl={far['removed_recorded_pnl']} | "
                    f"by_index={far['by_underlying']} | "
                    f"version={far['cleanup_version']}"
                )
                print(
                    "Proven-invalid PAPER cleanup | "
                    f"removed={invalid['removed']} | "
                    f"users={invalid['affected_users']} | "
                    f"pnl={invalid['removed_recorded_pnl']} | "
                    f"by_reason={invalid['by_reason']} | "
                    f"version={invalid['cleanup_version']}"
                )
                return
            except Exception as exc:
                last_error = str(exc)[:240]
                time.sleep(5)

        print(f"PAPER cleanup failed after retries: {last_error}")

    threading.Thread(
        target=worker,
        name="okai-paper-trade-cleanup",
        daemon=True,
    ).start()
    return True

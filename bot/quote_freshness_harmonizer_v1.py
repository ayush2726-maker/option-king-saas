"""Harmonize open-trade quote freshness windows.

Normal Upstox open-position quotes are batched, while last-resort recovery runs
less frequently to avoid API throttling. A 15-second UI threshold and 10-second
runtime restart threshold therefore produced false STALE badges and needless
runtime churn even when a newer price was arriving.

This patch changes freshness/recovery timing only. It does not change entry,
exit, SL, sizing, strategy, P&L, or broker order rules.
"""
from __future__ import annotations

import sys
import threading
import time

DISPLAY_STALE_SECONDS = 45
RUNTIME_STALE_SECONDS = 45


def _apply_once() -> bool:
    applied = False

    live_routes = sys.modules.get("bot.trade_live_routes")
    if live_routes is not None:
        try:
            live_routes.LIVE_QUOTE_STALE_SECONDS = DISPLAY_STALE_SECONDS
            live_routes.QUOTE_FRESHNESS_HARMONIZER = "V1"
            applied = True
        except Exception:
            pass

    recovery = sys.modules.get("bot.live_quote_runtime_recovery")
    if recovery is not None:
        try:
            recovery.STALE_RUNTIME_SECONDS = RUNTIME_STALE_SECONDS
            recovery.QUOTE_FRESHNESS_HARMONIZER = "V1"
            applied = True
        except Exception:
            pass

    return applied


def _loop() -> None:
    # Routes and recovery middleware are imported at different points during
    # application startup. Keep applying for a short startup window so module
    # import order cannot leave one side on the old threshold.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        _apply_once()
        live_ready = sys.modules.get("bot.trade_live_routes") is not None
        recovery_ready = sys.modules.get("bot.live_quote_runtime_recovery") is not None
        if live_ready and recovery_ready:
            return
        time.sleep(0.25)


def schedule_quote_freshness_harmonizer() -> None:
    _apply_once()
    threading.Thread(
        target=_loop,
        name="okai-quote-freshness-harmonizer",
        daemon=True,
    ).start()

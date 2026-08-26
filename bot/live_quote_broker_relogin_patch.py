"""Recover stale open-position quotes by re-authenticating the broker session.

This patch is intentionally limited to quote reads for already-open positions.
It does not alter signals, entries, exits, SL logic, sizing, or order placement.
"""

from __future__ import annotations

import sys
import threading
import time

_PATCHED = False
_PATCH_LOCK = threading.Lock()


def _safe_ltp(result):
    try:
        if not isinstance(result, dict) or not result.get("success"):
            return None
        value = float(result.get("ltp") or 0)
        return value if value > 0 else None
    except Exception:
        return None


def _patch_runtime_module(runtime):
    global _PATCHED
    with _PATCH_LOCK:
        if _PATCHED:
            return

        original = getattr(runtime, "_ltp_multi", None)
        if not callable(original):
            return

        def resilient_ltp_multi(broker_name, obj, trade):
            errors = []

            # First use the normal code path exactly as before.
            try:
                result = original(broker_name, obj, trade)
            except Exception as exc:
                result = {"success": False, "message": str(exc)}
            if _safe_ltp(result) is not None:
                return result
            errors.append(str((result or {}).get("message") or "OPTION_LTP_FAILED")[:180])

            # A stale authenticated session is the common cause of repeated
            # quote failures after Railway/runtime reconnects. Re-login the
            # same broker object and then retry immediately.
            try:
                login = obj.login()
                if isinstance(login, dict) and login.get("success") is False:
                    errors.append(str(login.get("message") or "BROKER_RELOGIN_FAILED")[:180])
            except Exception as exc:
                errors.append(f"RELOGIN:{type(exc).__name__}:{str(exc)[:140]}")

            # Retry normal resolution first, then Upstox token/symbol variants.
            for attempt in range(3):
                try:
                    result = original(broker_name, obj, trade)
                except Exception as exc:
                    result = {"success": False, "message": str(exc)}
                if _safe_ltp(result) is not None:
                    result = dict(result)
                    result["recovered_after_relogin"] = True
                    result["recovery_attempt"] = attempt + 1
                    return result
                errors.append(str((result or {}).get("message") or "OPTION_LTP_FAILED")[:180])

                if str(broker_name or "").lower() == "upstox":
                    symbol = runtime._v(trade, "symbol", "") or ""
                    token = runtime._v(trade, "token", "") or ""
                    exchange = runtime._v(trade, "exch_seg", "NSE_FO") or "NSE_FO"
                    for ref in (token, symbol):
                        if not ref:
                            continue
                        try:
                            alt = obj.get_ltp(ref, exchange=exchange)
                        except Exception as exc:
                            alt = {"success": False, "message": str(exc)}
                        if _safe_ltp(alt) is not None:
                            alt = dict(alt)
                            alt["recovered_after_relogin"] = True
                            alt["recovery_ref"] = "token" if ref == token else "symbol"
                            return alt
                        errors.append(str((alt or {}).get("message") or "OPTION_LTP_FAILED")[:180])

                time.sleep(0.35)

            return {
                "success": False,
                "message": "BROKER_QUOTE_RECOVERY_FAILED | " + " | ".join(errors[-6:]),
                "session_relogin_attempted": True,
            }

        runtime._ltp_multi = resilient_ltp_multi
        runtime.LIVE_QUOTE_BROKER_RELOGIN_PATCH = "V1"
        _PATCHED = True


def _wait_and_patch():
    # bot.__init__ runs before auto_portfolio_runtime is necessarily imported.
    # Wait briefly and patch the module as soon as it exists, without forcing
    # an early circular import during package initialization.
    for _ in range(240):
        runtime = sys.modules.get("bot.auto_portfolio_runtime")
        if runtime is not None:
            try:
                _patch_runtime_module(runtime)
                if _PATCHED:
                    return
            except Exception:
                pass
        time.sleep(0.25)


def schedule_live_quote_broker_relogin_patch():
    thread = threading.Thread(
        target=_wait_and_patch,
        name="okai-quote-broker-relogin-patch",
        daemon=True,
    )
    thread.start()

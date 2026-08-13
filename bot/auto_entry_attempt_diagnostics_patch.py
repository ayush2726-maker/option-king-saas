"""Visible diagnostics for AUTO entry attempts.

A live index scan can be QUALIFIED while the actual option entry is still blocked
by the later execution guards: option contract resolution, option LTP fetch,
real premium direction confirmation, sizing, mode lock, max positions, or live
order failure.  This patch is display/diagnostic only.  It does not loosen or
tighten entry rules and it never places, modifies or closes orders by itself.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

VERSION = "OKAI-AUTO-ENTRY-DIAGNOSTICS-V1"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _attempt(state: dict, *, allowed: bool, reason: str, stage: str, selected=None, **extra) -> None:
    signal = dict((selected or {}).get("signal_data") or {})
    payload = {
        "allowed": bool(allowed),
        "reason": str(reason or ""),
        "stage": str(stage or ""),
        "version": VERSION,
        "underlying": (selected or {}).get("underlying"),
        "side": signal.get("signal") or signal.get("candidate_signal"),
        "score": signal.get("score"),
        "min_score": signal.get("min_score", 82),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    state["entry_guard"] = dict(payload)
    state["last_entry_attempt"] = dict(payload)
    if not allowed:
        state["last_entry_block_reason"] = payload["reason"]


def _reason_from_state(state: dict) -> str:
    if state.get("mode_change_blocked"):
        return "MODE_CHANGE_WAITING_FOR_EXISTING_POSITION_CLOSE"
    if state.get("position_size_block"):
        return "POSITION_SIZE_BLOCK:" + str((state.get("position_size_block") or {}).get("reason") or "LOT_BUDGET_TOO_SMALL")
    if state.get("live_order_error"):
        return "LIVE_ORDER_ERROR:" + str(state.get("live_order_error"))[:140]
    if state.get("live_order_lock"):
        return "LIVE_ORDER_LOCK_ACTIVE"
    guard = dict(state.get("entry_guard") or {})
    if guard.get("reason"):
        return str(guard.get("reason"))
    return "ENTRY_NOT_OPENED_BY_EXECUTION_GUARD"


def _resolve_multi_contract(
    obj,
    broker_name: str,
    underlying: str,
    strike: float,
    side: str,
    *,
    sleeper=time.sleep,
) -> tuple[dict, list[str]]:
    # The Upstox resolver performs its own exact-expiry retries and unfiltered
    # strict recovery. Repeating that whole sequence here would delay entry.
    attempts = 1 if str(broker_name).lower() == "upstox" else 3
    resolved: dict = {}
    errors: list[str] = []
    for attempt in range(attempts):
        resolved = obj.search_option(
            underlying,
            "current_week",
            strike,
            side,
        )
        if resolved.get("success"):
            break
        errors.append(
            str(
                resolved.get("message")
                or resolved.get("error")
                or "OPTION_CONTRACT_NOT_RESOLVED"
            )[:180]
        )
        if attempt + 1 < attempts:
            sleeper(0.5 * (attempt + 1))
    return resolved, errors


def _fetch_multi_ltp(
    obj,
    broker_name: str,
    resolved: dict,
    *,
    sleeper=time.sleep,
) -> tuple[dict, list[str]]:
    quote: dict = {}
    errors: list[str] = []
    for attempt in range(3):
        if str(broker_name).lower() == "upstox":
            quote = obj.get_ltp(
                resolved.get("token") or resolved["symbol"],
                exchange=resolved.get("exchange", "NSE_FO"),
            )
        else:
            quote = obj.get_ltp(
                resolved["symbol"],
                exchange=resolved.get("exchange", "NFO"),
            )
        if quote.get("success") and _f(quote.get("ltp"), 0) > 0:
            break
        errors.append(
            str(
                quote.get("message")
                or quote.get("error")
                or "OPTION_LTP_FAILED"
            )[:180]
        )
        if attempt < 2:
            sleeper(0.5 * (attempt + 1))
    return quote, errors


def apply_auto_entry_attempt_diagnostics_patch() -> None:
    try:
        from bot import auto_portfolio_runtime as runtime

        if getattr(runtime, "_okai_auto_entry_attempt_diagnostics_v1", False):
            return

        original_open_common = runtime._open_common
        original_state_update = runtime._state_update

        def open_common_with_diagnostics(
            conn,
            user_id,
            broker_name,
            selected,
            settings,
            resolved,
            quote_price,
            quality,
            lot_size,
            live_order,
            live_cash,
            state,
        ):
            quality = dict(quality or {})
            if quality.get("allowed") is False:
                _attempt(
                    state,
                    allowed=False,
                    reason=quality.get("reason") or "OPTION_ENTRY_QUALITY_BLOCKED",
                    stage="OPTION_ENTRY_QUALITY",
                    selected=selected,
                    broker=broker_name,
                    symbol=(resolved or {}).get("symbol"),
                    option_ltp=round(_f(quote_price), 2),
                    quality=quality,
                )
                return original_open_common(
                    conn, user_id, broker_name, selected, settings, resolved,
                    quote_price, quality, lot_size, live_order, live_cash, state,
                )

            _attempt(
                state,
                allowed=True,
                reason="ENTRY_OPEN_ATTEMPT_STARTED",
                stage="OPEN_COMMON",
                selected=selected,
                broker=broker_name,
                symbol=(resolved or {}).get("symbol"),
                option_ltp=round(_f(quote_price), 2),
            )
            opened = bool(original_open_common(
                conn, user_id, broker_name, selected, settings, resolved,
                quote_price, quality, lot_size, live_order, live_cash, state,
            ))
            if opened:
                _attempt(
                    state,
                    allowed=True,
                    reason="ENTRY_OPENED",
                    stage="OPEN_COMMON",
                    selected=selected,
                    broker=broker_name,
                    symbol=(resolved or {}).get("symbol"),
                    option_ltp=round(_f(quote_price), 2),
                    trade=state.get("last_opened_trade"),
                )
            else:
                _attempt(
                    state,
                    allowed=False,
                    reason=_reason_from_state(state),
                    stage="OPEN_COMMON",
                    selected=selected,
                    broker=broker_name,
                    symbol=(resolved or {}).get("symbol"),
                    option_ltp=round(_f(quote_price), 2),
                )
            return opened

        def open_angel_with_diagnostics(conn, user_id, obj, selected, settings, state):
            underlying = selected["underlying"]
            signal = selected["signal_data"]
            market = selected["market_data"]
            resolved = runtime._legacy().resolve_option(underlying, market["price"], signal["signal"])
            if not resolved:
                _attempt(state, allowed=False, reason="OPTION_CONTRACT_NOT_RESOLVED", stage="RESOLVE_CONTRACT", selected=selected, broker="angelone")
                return False
            resolved = dict(resolved)
            resolved["exchange"] = resolved.get("exch_seg") or resolved.get("exchange")
            try:
                q = obj.ltpData(resolved["exch_seg"], resolved["symbol"], resolved["token"])
                quote_price = float(q["data"]["ltp"])
            except Exception as exc:
                _attempt(state, allowed=False, reason="OPTION_LTP_FAILED:" + str(exc)[:120], stage="OPTION_LTP", selected=selected, broker="angelone", symbol=resolved.get("symbol"))
                return False
            quality = runtime._legacy()._option_entry_quality_angel(obj, resolved, quote_price)
            return runtime._open_common(
                conn, user_id, "angelone", selected, settings, resolved, quote_price,
                quality, runtime._legacy().LOT_SIZES.get(underlying, 1),
                lambda r, a, q, p: runtime._place_angel(obj, r, a, q, p),
                lambda: runtime._angel_cash(obj), state,
            )

        def open_multi_with_diagnostics(conn, user_id, broker_name, obj, selected, settings, state):
            underlying = selected["underlying"]
            signal = selected["signal_data"]
            market = selected["market_data"]
            resolved, resolve_errors = _resolve_multi_contract(
                obj,
                broker_name,
                underlying,
                runtime._legacy()._dynamic_atm_strike(
                    underlying,
                    market["price"],
                ),
                signal["signal"],
            )
            if not resolved.get("success"):
                _attempt(
                    state,
                    allowed=False,
                    reason="OPTION_CONTRACT_NOT_RESOLVED:" + str(resolved.get("message") or resolved.get("error") or "")[:180],
                    stage="RESOLVE_CONTRACT",
                    selected=selected,
                    broker=broker_name,
                    resolve_attempts=(1 if broker_name == "upstox" else 3),
                    errors=resolve_errors,
                    expected_expiry=resolved.get("expected_expiry"),
                )
                return False
            quote, quote_errors = _fetch_multi_ltp(
                obj,
                broker_name,
                resolved,
            )
            if not quote.get("success") or _f(quote.get("ltp"), 0) <= 0:
                _attempt(
                    state,
                    allowed=False,
                    reason="OPTION_LTP_FAILED:" + str(quote.get("message") or quote.get("error") or "INVALID_OR_EMPTY_LTP")[:180],
                    stage="OPTION_LTP",
                    selected=selected,
                    broker=broker_name,
                    symbol=resolved.get("symbol"),
                    token=resolved.get("token"),
                    exchange=resolved.get("exchange"),
                    quote_attempts=3,
                    errors=quote_errors,
                )
                return False
            quote_price = _f(quote.get("ltp"), 0)
            if quote_price <= 0:
                _attempt(state, allowed=False, reason="INVALID_OPTION_LTP", stage="OPTION_LTP", selected=selected, broker=broker_name, symbol=resolved.get("symbol"))
                return False
            quality = runtime._legacy()._option_entry_quality_multi(broker_name, obj, resolved, quote_price)
            return runtime._open_common(
                conn, user_id, broker_name, selected, settings, resolved, quote_price,
                quality, resolved.get("lot_size") or runtime._legacy().LOT_SIZES.get(underlying, 1),
                lambda r, a, q, p: runtime._place_multi(obj, r, a, q, p),
                lambda: runtime._multi_cash(obj), state,
            )

        def state_update_with_entry_diagnostics(state, scans, selected, settings, rows):
            original_state_update(state, scans, selected, settings, rows)
            attempt = dict(state.get("last_entry_attempt") or state.get("entry_guard") or {})
            if not attempt:
                return
            state["entry_attempt"] = attempt
            if attempt.get("reason"):
                state["entry_block_reason"] = attempt.get("reason")
            summary = dict(state.get("selected_for_entry") or {})
            if summary and attempt.get("allowed") is False:
                summary["status"] = "ENTRY_GUARD_BLOCKED"
                summary["entry_status"] = "ENTRY_GUARD_BLOCKED"
                summary["entry_block_reason"] = str(attempt.get("reason") or "")[:180]
                summary["entry_guard_stage"] = attempt.get("stage")
                summary["trade_allowed"] = False
                state["selected_for_entry"] = summary

        runtime._open_common = open_common_with_diagnostics
        runtime._open_angel = open_angel_with_diagnostics
        runtime._open_multi = open_multi_with_diagnostics
        runtime._state_update = state_update_with_entry_diagnostics
        runtime._okai_auto_entry_attempt_diagnostics_v1 = True
    except Exception:
        pass

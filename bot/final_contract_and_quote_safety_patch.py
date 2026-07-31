"""Final defence-in-depth safety for AUTO option contracts and PAPER quotes.

This layer deliberately does not impose an entry clock. Entry timing remains
controlled by the user's active settings and the existing runtime guards.

It only adds two protections:
* reject missing, expired, or abnormally distant option expiries immediately
  before the trade row is inserted;
* require confirmation for a single extreme PAPER quote jump through the SL.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from bot import auto_portfolio_runtime as runtime


VERSION = "OKAI-FINAL-CONTRACT-QUOTE-SAFETY-V2"
IST = ZoneInfo("Asia/Kolkata")
MAX_DTE = {
    "NIFTY": 8,
    "SENSEX": 8,
    "BANKNIFTY": 40,
}

# PAPER-only bad-tick confirmation. Normal SL touches are not delayed.
EXTREME_JUMP_PERCENT = 12.0
EXTREME_JUMP_R_MULTIPLE = 5.0
CONFIRM_WINDOW_SECONDS = 20.0
CONFIRM_PRICE_TOLERANCE_PERCENT = 3.0
_pending_quote_confirmation: dict[tuple[int, int], dict[str, float]] = {}


def _today_ist() -> date:
    return datetime.now(timezone.utc).astimezone(IST).date()


def _normal_underlying(value: Any) -> str:
    raw = str(value or "").upper().replace(" ", "").replace("-", "")
    if raw in {"NIFTY", "NIFTY50"}:
        return "NIFTY"
    if raw in {"BANKNIFTY", "NIFTYBANK"}:
        return "BANKNIFTY"
    if "SENSEX" in raw:
        return "SENSEX"
    return raw


def _parse_expiry(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    raw = str(value or "").strip()
    if not raw:
        return None

    for candidate in (raw[:10], raw):
        for fmt in (
            "%Y-%m-%d",
            "%d-%m-%Y",
            "%d%b%Y",
            "%d-%b-%Y",
            "%d %b %Y",
            "%d %b %y",
        ):
            try:
                return datetime.strptime(candidate.upper(), fmt).date()
            except (TypeError, ValueError):
                continue

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _contract_check(resolved: Any, underlying: Any, today: date) -> dict:
    name = _normal_underlying(underlying)
    if not isinstance(resolved, dict):
        return {
            "allowed": False,
            "reason": "OPTION_CONTRACT_NOT_RESOLVED",
            "underlying": name,
            "safety_version": VERSION,
        }

    expiry_day = _parse_expiry(resolved.get("expiry"))
    if expiry_day is None:
        return {
            "allowed": False,
            "reason": "OPTION_EXPIRY_MISSING_OR_INVALID",
            "underlying": name,
            "symbol": str(resolved.get("symbol") or ""),
            "safety_version": VERSION,
        }

    dte = (expiry_day - today).days
    max_dte = int(MAX_DTE.get(name, 8))
    if dte < 0:
        return {
            "allowed": False,
            "reason": "OPTION_EXPIRY_ALREADY_EXPIRED",
            "underlying": name,
            "expiry": expiry_day.isoformat(),
            "expiry_dte": dte,
            "max_expiry_dte": max_dte,
            "safety_version": VERSION,
        }
    if dte > max_dte:
        return {
            "allowed": False,
            "reason": "OPTION_EXPIRY_TOO_FAR_FINAL_GUARD",
            "underlying": name,
            "symbol": str(resolved.get("symbol") or ""),
            "expiry": expiry_day.isoformat(),
            "expiry_dte": dte,
            "max_expiry_dte": max_dte,
            "safety_version": VERSION,
        }

    return {
        "allowed": True,
        "reason": "OPTION_EXPIRY_VALIDATED_FINAL_GUARD",
        "underlying": name,
        "symbol": str(resolved.get("symbol") or ""),
        "expiry": expiry_day.isoformat(),
        "expiry_dte": dte,
        "max_expiry_dte": max_dte,
        "safety_version": VERSION,
    }


def _mark_contract_block(state: Any, check: dict) -> None:
    if not isinstance(state, dict):
        return
    state.update(
        {
            "contract_safety_blocked": True,
            "contract_safety": check,
            "entry_block_reason": check.get("reason"),
            "selected_for_entry": None,
            "final_contract_quote_safety_version": VERSION,
        }
    )


def _clear_contract_block(state: Any, check: dict) -> None:
    if not isinstance(state, dict):
        return
    state["contract_safety_blocked"] = False
    state["contract_safety"] = check
    state["final_contract_quote_safety_version"] = VERSION


def _extreme_paper_sl_jump(trade: Any, ltp: float) -> bool:
    try:
        if runtime._mode(trade) != "paper":
            return False
        entry = runtime._f(runtime._v(trade, "entry_price", 0), 0)
        sl = runtime._f(runtime._v(trade, "sl_price", 0), 0)
        last_ltp = runtime._f(runtime._v(trade, "last_ltp", entry), entry)
        risk = runtime._f(
            runtime._v(trade, "initial_risk", max(0.05, entry - sl)),
            max(0.05, entry - sl),
        )
        if ltp <= 0 or sl <= 0 or last_ltp <= 0:
            return False
        if ltp > sl or last_ltp <= sl:
            return False

        drop = last_ltp - ltp
        drop_percent = drop / last_ltp * 100.0
        return (
            drop_percent >= EXTREME_JUMP_PERCENT
            and drop >= max(1.0, risk * EXTREME_JUMP_R_MULTIPLE)
        )
    except Exception:
        return False


def _confirmed_or_defer_quote(user_id: int, trade: Any, quote: Any, state: Any) -> Any:
    if not isinstance(quote, dict) or not quote.get("success"):
        return quote

    ltp = runtime._f(quote.get("ltp"), 0)
    try:
        trade_id = int(runtime._v(trade, "id", 0) or 0)
    except Exception:
        trade_id = 0
    key = (int(user_id), trade_id)

    if not _extreme_paper_sl_jump(trade, ltp):
        _pending_quote_confirmation.pop(key, None)
        return quote

    now = time.monotonic()
    previous = _pending_quote_confirmation.get(key)
    if previous:
        age = now - previous["seen_at"]
        difference = (
            abs(ltp - previous["ltp"])
            / max(ltp, previous["ltp"], 0.05)
            * 100.0
        )
        if age <= CONFIRM_WINDOW_SECONDS and difference <= CONFIRM_PRICE_TOLERANCE_PERCENT:
            _pending_quote_confirmation.pop(key, None)
            if isinstance(state, dict):
                state["paper_quote_jump_confirmed"] = {
                    "trade_id": trade_id,
                    "ltp": round(ltp, 2),
                    "confirmation_age_seconds": round(age, 2),
                    "safety_version": VERSION,
                }
                state.pop("paper_quote_jump_pending", None)
            return quote

    _pending_quote_confirmation[key] = {"ltp": ltp, "seen_at": now}
    if isinstance(state, dict):
        state["paper_quote_jump_pending"] = {
            "trade_id": trade_id,
            "symbol": str(runtime._v(trade, "symbol", "") or ""),
            "suspect_ltp": round(ltp, 2),
            "message": "Extreme one-tick PAPER SL gap awaiting confirmation",
            "safety_version": VERSION,
        }
    return {
        "success": False,
        "message": "PAPER_EXTREME_QUOTE_JUMP_AWAITING_CONFIRMATION",
    }


def apply_final_contract_and_quote_safety_patch() -> None:
    if getattr(runtime, "_okai_final_contract_quote_safety_v2", False):
        return

    original_open_common = runtime._open_common
    original_manage_rows = runtime._manage_rows

    def open_common_with_final_contract_guard(
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
        underlying = (
            selected.get("underlying")
            if isinstance(selected, dict)
            else ""
        )
        check = _contract_check(resolved, underlying, _today_ist())
        if not check.get("allowed"):
            _mark_contract_block(state, check)
            return False

        _clear_contract_block(state, check)
        return original_open_common(
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
        )

    def manage_rows_with_paper_quote_confirmation(
        conn,
        user_id,
        rows,
        scans,
        quote_fetcher,
        live_order,
        state,
    ):
        def guarded_quote_fetcher(trade):
            quote = quote_fetcher(trade)
            return _confirmed_or_defer_quote(user_id, trade, quote, state)

        return original_manage_rows(
            conn,
            user_id,
            rows,
            scans,
            guarded_quote_fetcher,
            live_order,
            state,
        )

    runtime._open_common = open_common_with_final_contract_guard
    runtime._manage_rows = manage_rows_with_paper_quote_confirmation
    runtime._okai_final_contract_quote_safety_v1 = True
    runtime._okai_final_contract_quote_safety_v2 = True
    runtime._okai_final_contract_quote_safety_version = VERSION

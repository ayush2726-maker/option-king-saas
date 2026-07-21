"""Same-day expiry and AUTO entry diagnostics patch.

Fixes:
- Angel resolver compared an expiry midnight datetime with the current clock time,
  so on expiry day the current contract was incorrectly treated as expired.
- Upstox `current_week` lookup can return no option for an index whose nearest
  listed contract is monthly. Resolve exact today first, then week/month/all.
- AUTO entry failures were silent. Surface score-safety and execution block
  reasons in the existing scan row without weakening the 82/fresh-entry guards.
"""

from datetime import date, datetime

import requests

from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime
from bot import option_chain
from bot.brokers.upstox import UpstoxBroker


UPSTOX_UNDERLYING_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    return None


def _resolve_angel_same_day(underlying, spot_price, option_type):
    underlying = str(underlying or "").upper().strip()
    option_type = str(option_type or "").upper().strip()
    strike_target = option_chain.get_atm_strike(underlying, float(spot_price or 0))
    today = date.today()

    def choose(options):
        candidates = [
            row for row in options or []
            if str(row.get("name") or "").upper() == underlying
            and str(row.get("symbol") or "").upper().endswith(option_type)
        ]
        active_expiries = sorted({
            parsed
            for row in candidates
            for parsed in [_parse_date(row.get("expiry"))]
            if parsed is not None and parsed >= today
        })
        if not active_expiries:
            return None

        nearest = active_expiries[0]
        same_expiry = [
            row for row in candidates
            if _parse_date(row.get("expiry")) == nearest
        ]

        def strike_of(row):
            try:
                return float(row.get("strike")) / 100.0
            except Exception:
                return None

        ranked = []
        for row in same_expiry:
            strike = strike_of(row)
            if strike is None:
                continue
            ranked.append((abs(strike - strike_target), strike, row))
        if not ranked:
            return None

        _, strike, best = min(ranked, key=lambda item: item[0])
        return {
            "token": best.get("token"),
            "symbol": best.get("symbol"),
            "exch_seg": best.get("exch_seg") or option_chain.EXCHANGE_FOR.get(underlying),
            "exchange": best.get("exch_seg") or option_chain.EXCHANGE_FOR.get(underlying),
            "strike": strike,
            "expiry": str(best.get("expiry") or ""),
            "expiry_date": nearest.isoformat(),
            "same_day_expiry": nearest == today,
        }

    result = choose(option_chain._load_cache())
    if result:
        return result
    return choose(option_chain._load_cache(force_refresh=True))


def _upstox_option_contracts(self, underlying, expiry_value):
    key = UPSTOX_UNDERLYING_KEYS[underlying]
    params = {"instrument_key": key}
    if expiry_value:
        params["expiry_date"] = expiry_value
    response = requests.get(
        f"{self.BASE_URL}/option/contract",
        params=params,
        headers=self._h(),
        timeout=18,
    )
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if response.status_code != 200 or payload.get("status") != "success":
        return [], str(payload.get("errors") or payload.get("message") or response.text)[:220]
    return payload.get("data") or [], None


def _search_upstox_nearest(self, underlying, expiry, strike, option_type):
    try:
        u = str(underlying or "").upper().strip()
        ot = str(option_type or "").upper().strip()
        if u not in UPSTOX_UNDERLYING_KEYS or ot not in ("CE", "PE"):
            return {"success": False, "message": "Unsupported option request"}

        today = date.today()
        requested = str(expiry or "current_week").strip()
        attempts = [today.isoformat()]
        if requested and requested not in attempts:
            attempts.append(requested)
        for fallback in ("current_week", "current_month", None):
            if fallback not in attempts:
                attempts.append(fallback)

        all_errors = []
        for expiry_value in attempts:
            rows, error = _upstox_option_contracts(self, u, expiry_value)
            if error:
                all_errors.append(f"{expiry_value or 'nearest'}:{error}")
                continue

            ranked = []
            for row in rows:
                if str(row.get("instrument_type") or "").upper() != ot:
                    continue
                symbol_underlying = str(
                    row.get("underlying_symbol") or row.get("name") or ""
                ).upper().replace(" ", "")
                if symbol_underlying not in {u, "NIFTYBANK" if u == "BANKNIFTY" else u}:
                    continue
                expiry_date = _parse_date(row.get("expiry"))
                if expiry_date is None or expiry_date < today:
                    continue
                try:
                    row_strike = float(row.get("strike_price") or 0)
                except Exception:
                    continue
                ranked.append((expiry_date, abs(row_strike - float(strike)), row_strike, row))

            if not ranked:
                all_errors.append(f"{expiry_value or 'nearest'}:no matching {u} {ot}")
                continue

            expiry_date, _, row_strike, best = min(
                ranked,
                key=lambda item: (item[0], item[1]),
            )
            return {
                "success": True,
                "symbol": best.get("trading_symbol"),
                "token": best.get("instrument_key"),
                "exchange": best.get("segment") or ("BSE_FO" if u == "SENSEX" else "NSE_FO"),
                "expiry": str(best.get("expiry") or expiry_date.isoformat()),
                "strike": row_strike,
                "lot_size": int(best.get("lot_size") or best.get("minimum_lot") or 0),
                "expiry_source": expiry_value or "nearest_active",
                "same_day_expiry": expiry_date == today,
            }

        return {
            "success": False,
            "message": "Upstox option resolve failed | " + " | ".join(all_errors)[:500],
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)[:500]}


def _signal_block_reason(signal):
    if not isinstance(signal, dict):
        return "SIGNAL_NOT_READY"
    reasons = signal.get("fresh_entry_block_reasons") or []
    if reasons:
        return str(reasons[0])
    if signal.get("sideways_blocked"):
        return "SIDEWAYS_BLOCKED"
    if signal.get("ema_chase_blocked"):
        return "EMA_ANTI_CHASE"
    if signal.get("vwap_chase_blocked"):
        return "VWAP_ANTI_CHASE"
    warnings = signal.get("warnings") or []
    for warning in warnings:
        text = str(warning)
        if "BLOCK" in text or "CHASE" in text or "REVERS" in text or "EXTENSION" in text:
            return text
    return "SCORE_PASSED_BUT_SAFETY_NOT_QUALIFIED"


def _execution_reason(state):
    guard = state.get("entry_guard") or {}
    if guard and not guard.get("allowed", True):
        return str(guard.get("reason") or "OPTION_PREMIUM_GUARD")
    size = state.get("position_size_block") or {}
    if size:
        return str(size.get("reason") or "POSITION_SIZE_BLOCK")
    for key in ("mode_change_blocked", "live_order_error"):
        if state.get(key):
            return str(state.get(key))
    if state.get("live_order_lock"):
        return "LIVE_ORDER_LOCK"
    return "OPTION_RESOLVE_LTP_OR_ATR_FAILED"


def apply_expiry_entry_diagnostics_patch():
    if getattr(runtime, "_okai_expiry_entry_diag_v1", False):
        return

    option_chain.resolve_option = _resolve_angel_same_day
    angel_fetcher.resolve_option = _resolve_angel_same_day
    UpstoxBroker.search_option = _search_upstox_nearest

    original_summary = runtime._summary
    original_open_angel = runtime._open_angel
    original_open_multi = runtime._open_multi

    def summary_with_reason(scan):
        summary = original_summary(scan)
        signal = scan.get("signal_data") or {}
        score = int(summary.get("score") or 0)
        minimum = int(summary.get("min_score") or 82)

        attempt_error = scan.get("entry_attempt_error")
        if attempt_error:
            summary["trade_allowed"] = False
            summary["status"] = "ENTRY_BLOCKED"
            summary["candidate_signal"] = str(attempt_error)[:90]
            summary["entry_block_reason"] = str(attempt_error)[:180]
            return summary

        if score >= minimum and not summary.get("trade_allowed"):
            reason = _signal_block_reason(signal)
            summary["status"] = "SAFETY_BLOCKED"
            summary["candidate_signal"] = reason[:90]
            summary["entry_block_reason"] = reason[:180]
        elif summary.get("trade_allowed"):
            summary["entry_block_reason"] = None
            summary["entry_status"] = "QUALIFIED"

        summary["fresh_entry_ok"] = signal.get("fresh_entry_ok")
        summary["core_confirmations"] = signal.get("core_confirmations")
        summary["score_before_volume_normalize"] = signal.get("score_before_volume_normalize")
        return summary

    def open_angel_with_reason(conn, user_id, obj, selected, settings, state):
        selected.pop("entry_attempt_error", None)
        opened = original_open_angel(conn, user_id, obj, selected, settings, state)
        if opened:
            selected["entry_attempt_status"] = "OPENED"
            state["entry_attempt"] = {
                "underlying": selected.get("underlying"),
                "status": "OPENED",
            }
        else:
            reason = _execution_reason(state)
            selected["entry_attempt_error"] = reason
            state["entry_attempt"] = {
                "underlying": selected.get("underlying"),
                "status": "BLOCKED",
                "reason": reason,
            }
        return opened

    def open_multi_with_reason(conn, user_id, broker_name, obj, selected, settings, state):
        selected.pop("entry_attempt_error", None)
        opened = original_open_multi(
            conn, user_id, broker_name, obj, selected, settings, state
        )
        if opened:
            selected["entry_attempt_status"] = "OPENED"
            state["entry_attempt"] = {
                "underlying": selected.get("underlying"),
                "status": "OPENED",
            }
        else:
            reason = _execution_reason(state)
            selected["entry_attempt_error"] = reason
            state["entry_attempt"] = {
                "underlying": selected.get("underlying"),
                "status": "BLOCKED",
                "reason": reason,
            }
        return opened

    runtime._summary = summary_with_reason
    runtime._open_angel = open_angel_with_reason
    runtime._open_multi = open_multi_with_reason
    runtime._okai_expiry_entry_diag_v1 = True

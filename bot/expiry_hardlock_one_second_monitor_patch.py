"""Expiry hard-lock and one-second open-position monitoring.

Safety rules:
- On a NIFTY weekly-expiry Tuesday, only a contract expiring today may be
  opened. If today's contract cannot be resolved, skip the entry rather than
  silently selecting the following week.
- While a position is open, refresh option LTP and evaluate SL/profit-lock once
  per second. Strategy/index candle scans remain on their existing ~1 minute
  cycle, so candle-based reversal confirmation is not fabricated from ticks.
"""

import time
from datetime import date, datetime, timezone

from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime
from bot import option_chain
from bot.brokers.upstox import UpstoxBroker


def _parse_expiry(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip().upper()
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    return None


def _today_ist():
    return runtime._now_ist().date()


def _requires_today_expiry(underlying):
    # NIFTY weekly expiry is Tuesday in this project.  Do not impose this rule
    # on BANKNIFTY/SENSEX because their listed-expiry schedules can differ.
    return str(underlying or "").upper() == "NIFTY" and runtime._now_ist().weekday() == 1


def _expiry_is_today(resolved):
    if not isinstance(resolved, dict):
        return False
    parsed = _parse_expiry(resolved.get("expiry_date") or resolved.get("expiry"))
    return parsed == _today_ist()


def _expiry_block_reason():
    return "TODAY_NIFTY_EXPIRY_NOT_AVAILABLE_NEXT_WEEK_BLOCKED"


def _sync_open_state(state, rows):
    state["open_trade_monitor_seconds"] = 1
    state["open_trade_count"] = len(rows)
    state["open_positions"] = [
        {
            "id": row["id"],
            "underlying": runtime._underlying(row),
            "side": row["side"],
            "symbol": row["symbol"],
            "qty": row["qty"],
            "entry_price": runtime._v(row, "entry_price", 0),
            "last_ltp": runtime._v(row, "last_ltp", 0),
            "sl_price": runtime._v(row, "sl_price", 0),
            "capital_slot": runtime._v(row, "capital_slot"),
            "allocation_pct": runtime._v(row, "allocation_pct"),
            "trading_mode": runtime._mode(row),
        }
        for row in rows
    ]
    state["updated_at"] = datetime.now(timezone.utc).isoformat()


def _monitor_angel_for_one_minute(user_id, obj, state, scans):
    for _ in range(60):
        time.sleep(1)
        if not state.get("running"):
            break
        conn = runtime.get_db()
        try:
            runtime._ensure_schema(conn)
            rows = runtime._open_rows(conn, user_id)
            if rows:
                runtime._manage_rows(
                    conn,
                    user_id,
                    rows,
                    scans,
                    lambda trade: runtime._ltp_angel(obj, trade),
                    lambda r, a, q, p: runtime._place_angel(obj, r, a, q, p),
                    state,
                )
                rows = runtime._open_rows(conn, user_id)
            _sync_open_state(state, rows)
        finally:
            conn.close()


def _monitor_multi_for_one_minute(user_id, broker_name, obj, state, scans):
    for _ in range(60):
        time.sleep(1)
        if not state.get("running"):
            break
        conn = runtime.get_db()
        try:
            runtime._ensure_schema(conn)
            rows = runtime._open_rows(conn, user_id)
            if rows:
                runtime._manage_rows(
                    conn,
                    user_id,
                    rows,
                    scans,
                    lambda trade: runtime._ltp_multi(broker_name, obj, trade),
                    lambda r, a, q, p: runtime._place_multi(obj, r, a, q, p),
                    state,
                )
                rows = runtime._open_rows(conn, user_id)
            _sync_open_state(state, rows)
        finally:
            conn.close()


def _run_angel_one_second(user_id, creds, state):
    obj = None
    while state.get("running"):
        try:
            if obj is None:
                obj = runtime._legacy().angel_login(creds)
                state["status"] = "LOGGED_IN"

            settings = runtime._legacy()._read_settings(user_id)
            profile = runtime._legacy().get_active_profile_config(user_id)
            streak = runtime._legacy()._get_consecutive_losses_today(user_id)
            scans = runtime._scan_angel(user_id, obj, settings, profile, streak)

            conn = runtime.get_db()
            try:
                runtime._ensure_schema(conn)
                rows = runtime._open_rows(conn, user_id)
                runtime._manage_rows(
                    conn,
                    user_id,
                    rows,
                    scans,
                    lambda trade: runtime._ltp_angel(obj, trade),
                    lambda r, a, q, p: runtime._place_angel(obj, r, a, q, p),
                    state,
                )

                rows = runtime._open_rows(conn, user_id)
                blocked = {runtime._underlying(row) for row in rows}
                selected = None
                if runtime._can_enter(conn, user_id, settings, rows, state):
                    selected = runtime._best_candidate(scans, blocked)
                    if selected:
                        runtime._open_angel(conn, user_id, obj, selected, settings, state)

                rows = runtime._open_rows(conn, user_id)
                runtime._state_update(state, scans, selected, settings, rows)
                _sync_open_state(state, rows)
            finally:
                conn.close()

            _monitor_angel_for_one_minute(user_id, obj, state, scans)
        except Exception as exc:
            obj = None
            state["status"] = "ERROR: " + str(exc)[:140]
            time.sleep(30)


def _run_multi_one_second(user_id, broker_name, creds, state):
    obj = None
    while state.get("running"):
        try:
            if obj is None:
                obj = runtime._legacy().create_broker(
                    broker_name,
                    creds["client_id"],
                    creds["api_key"],
                    creds["password"],
                    creds.get("totp_secret"),
                )
                login = obj.login()
                if not login.get("success"):
                    raise RuntimeError(login.get("message", "Login failed"))
                state["status"] = "LOGGED_IN"

            settings = runtime._legacy()._read_settings(user_id)
            profile = runtime._legacy().get_active_profile_config(user_id)
            streak = runtime._legacy()._get_consecutive_losses_today(user_id)
            scans = runtime._scan_multi(
                user_id, broker_name, obj, settings, profile, streak
            )

            conn = runtime.get_db()
            try:
                runtime._ensure_schema(conn)
                rows = runtime._open_rows(conn, user_id)
                runtime._manage_rows(
                    conn,
                    user_id,
                    rows,
                    scans,
                    lambda trade: runtime._ltp_multi(broker_name, obj, trade),
                    lambda r, a, q, p: runtime._place_multi(obj, r, a, q, p),
                    state,
                )

                rows = runtime._open_rows(conn, user_id)
                blocked = {runtime._underlying(row) for row in rows}
                selected = None
                if runtime._can_enter(conn, user_id, settings, rows, state):
                    selected = runtime._best_candidate(scans, blocked)
                    if selected:
                        runtime._open_multi(
                            conn,
                            user_id,
                            broker_name,
                            obj,
                            selected,
                            settings,
                            state,
                        )

                rows = runtime._open_rows(conn, user_id)
                runtime._state_update(state, scans, selected, settings, rows)
                _sync_open_state(state, rows)
            finally:
                conn.close()

            _monitor_multi_for_one_minute(user_id, broker_name, obj, state, scans)
        except Exception as exc:
            obj = None
            state["status"] = "ERROR: " + str(exc)[:140]
            time.sleep(30)


def apply_expiry_hardlock_one_second_monitor_patch():
    if getattr(runtime, "_okai_expiry_hardlock_1s_v1", False):
        return

    original_angel_resolver = angel_fetcher.resolve_option
    original_upstox_search = UpstoxBroker.search_option
    original_open_angel = runtime._open_angel

    def strict_angel_resolver(underlying, spot_price, option_type):
        resolved = original_angel_resolver(underlying, spot_price, option_type)
        if _requires_today_expiry(underlying):
            if not resolved or not _expiry_is_today(resolved):
                return None
        return resolved

    def strict_upstox_search(self, underlying, expiry, strike, option_type):
        result = original_upstox_search(self, underlying, expiry, strike, option_type)
        if (
            _requires_today_expiry(underlying)
            and result.get("success")
            and not _expiry_is_today(result)
        ):
            return {
                "success": False,
                "message": _expiry_block_reason(),
            }
        return result

    def open_angel_hardlocked(conn, user_id, obj, selected, settings, state):
        underlying = selected.get("underlying")
        if _requires_today_expiry(underlying):
            signal = selected.get("signal_data") or {}
            market = selected.get("market_data") or {}
            preview = strict_angel_resolver(
                underlying,
                market.get("price", 0),
                signal.get("signal") or signal.get("candidate_signal"),
            )
            if not preview:
                reason = _expiry_block_reason()
                selected["entry_attempt_error"] = reason
                state["expiry_contract_block"] = reason
                state["entry_attempt"] = {
                    "underlying": underlying,
                    "status": "BLOCKED",
                    "reason": reason,
                }
                return False
        return original_open_angel(conn, user_id, obj, selected, settings, state)

    option_chain.resolve_option = strict_angel_resolver
    angel_fetcher.resolve_option = strict_angel_resolver
    UpstoxBroker.search_option = strict_upstox_search
    runtime._open_angel = open_angel_hardlocked

    runtime.run_user_bot_auto = _run_angel_one_second
    runtime.run_user_bot_multi_auto = _run_multi_one_second
    # angel_fetcher imported these functions as aliases at module import time, so
    # update those aliases explicitly as well.
    angel_fetcher.run_user_bot = _run_angel_one_second
    angel_fetcher.run_user_bot_multi = _run_multi_one_second

    runtime._okai_expiry_hardlock_1s_v1 = True

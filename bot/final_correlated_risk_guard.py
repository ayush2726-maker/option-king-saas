"""Final-order correlated-position and post-loss cooldown enforcement.

This module is intentionally installed after every other runtime wrapper.  It
is the last authority before an order can reach ``_open_common`` and therefore
cannot be bypassed by a later scan/entry patch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from bot import auto_portfolio_runtime as runtime
from bot import post_loss_reentry_guard_patch as cooldown


CORRELATED_INDICES = {"NIFTY", "BANKNIFTY", "SENSEX"}
COOLDOWN_SECONDS = 15 * 60
COOLDOWN_REASON = "POST_ATR_SL_SAME_SIDE_COOLDOWN_15M"
SAME_DIRECTION_REASON = "CORRELATED_SAME_DIRECTION_POSITION_OPEN"
HEDGE_NOT_LOSING_REASON = "OPPOSITE_HEDGE_REQUIRES_EXISTING_LOSS"
HEDGE_SAME_INDEX_REASON = "OPPOSITE_HEDGE_REQUIRES_DIFFERENT_INDEX"
VERSION = "OKAI-FINAL-CORRELATED-RISK-GUARD-V2"


def _value(row: Any, key: str, default=None):
    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        pass
    if isinstance(row, dict):
        value = row.get(key)
        return default if value is None else value
    return default


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _parse(value: Any):
    try:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _candidate(selected: dict) -> tuple[str, str]:
    signal = dict((selected or {}).get("signal_data") or {})
    underlying = str((selected or {}).get("underlying") or "").upper()
    side = str(signal.get("signal") or signal.get("candidate_signal") or "").upper()
    return underlying, side


def _mode(row: Any) -> str:
    return str(_value(row, "trading_mode", "paper") or "paper").lower()


def _open_net_pnl(row: Any):
    entry = _f(_value(row, "entry_price"), 0.0)
    raw_current = _value(row, "last_ltp")
    if raw_current is None or _f(raw_current, 0.0) <= 0:
        return None
    current = _f(raw_current, entry)
    qty = max(1, int(_f(_value(row, "qty", 1), 1)))
    try:
        from bot.net_pnl_history_patch import calculate_row_net_costs

        costs = calculate_row_net_costs(row, exit_price=current) or {}
        return round(_f(costs.get("net_pnl"), (current - entry) * qty), 2)
    except Exception:
        return round((current - entry) * qty, 2)


def _set_block(state: dict, reason: str, underlying: str, side: str, **details) -> bool:
    payload = {
        "allowed": False,
        "reason": reason,
        "stage": "FINAL_CORRELATED_RISK_GUARD",
        "underlying": underlying,
        "side": side,
        "version": VERSION,
        "updated_at": _iso(datetime.now(timezone.utc)),
        **details,
    }
    if isinstance(state, dict):
        state["entry_guard"] = payload
        state["entry_permission"] = payload
        state["entry_attempt"] = payload
        state["last_entry_attempt"] = payload
        state["entry_block_reason"] = reason
        state["last_entry_block_reason"] = reason
    return False


def _active_exact_cooldown(conn, user_id: int, underlying: str, side: str):
    cooldown._ensure_guard_schema(conn)
    row = conn.execute(
        """
        SELECT blocked_until, source_trade_id, reason, previous_pnl,
               previous_symbol, previous_exit_reason
        FROM auto_reentry_blocks
        WHERE user_id=? AND UPPER(underlying)=? AND UPPER(side)=?
        """,
        (int(user_id), underlying, side),
    ).fetchone()
    if not row:
        return None
    until = _parse(_value(row, "blocked_until"))
    now = datetime.now(timezone.utc)
    if not until or until <= now:
        conn.execute(
            "DELETE FROM auto_reentry_blocks WHERE user_id=? AND UPPER(underlying)=? AND UPPER(side)=?",
            (int(user_id), underlying, side),
        )
        conn.commit()
        return None
    return {
        "blocked_until": _iso(until),
        "remaining_seconds": max(1, int((until - now).total_seconds())),
        "source_trade_id": _value(row, "source_trade_id"),
        "previous_pnl": _value(row, "previous_pnl"),
        "previous_symbol": _value(row, "previous_symbol"),
        "previous_exit_reason": _value(row, "previous_exit_reason"),
    }


def _register_exact_cooldown(conn, user_id: int, trade: Any, reason: Any, price: Any) -> None:
    trade_id = int(_f(_value(trade, "id"), 0))
    stored = None
    if trade_id:
        try:
            stored = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        except Exception:
            stored = None
    source = stored or trade
    net = _value(source, "net_pnl")
    if net is None:
        net = _value(source, "pnl")
    if net is None:
        qty = max(1, int(_f(_value(source, "qty", 1), 1)))
        net = (_f(price) - _f(_value(source, "entry_price"))) * qty
    net = round(_f(net), 2)
    reason_text = str(reason or _value(source, "reason", "") or "")
    if not cooldown._loss_or_sl(reason_text, net):
        return

    underlying = str(runtime._underlying(source) or "").upper()
    side = str(_value(source, "side", "") or "").upper()
    if underlying not in CORRELATED_INDICES or side not in {"CE", "PE"}:
        return

    cooldown._ensure_guard_schema(conn)
    now = datetime.now(timezone.utc)
    blocked_until = now + timedelta(seconds=COOLDOWN_SECONDS)
    conn.execute(
        """
        INSERT INTO auto_reentry_blocks (
            user_id, underlying, side, blocked_until, source_trade_id,
            reason, created_at, previous_symbol, previous_pnl,
            previous_exit_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, underlying, side) DO UPDATE SET
            blocked_until=excluded.blocked_until,
            source_trade_id=excluded.source_trade_id,
            reason=excluded.reason,
            created_at=excluded.created_at,
            previous_symbol=excluded.previous_symbol,
            previous_pnl=excluded.previous_pnl,
            previous_exit_reason=excluded.previous_exit_reason
        """,
        (
            int(user_id), underlying, side, _iso(blocked_until), trade_id or None,
            COOLDOWN_REASON, _iso(now), str(_value(source, "symbol", "") or ""),
            net, reason_text[:240],
        ),
    )
    conn.commit()


def apply_final_correlated_risk_guard() -> bool:
    if getattr(runtime, "_okai_final_correlated_risk_guard_v1", False):
        return True

    previous_open = runtime._open_common
    previous_close = runtime._close

    def open_guarded(
        conn, user_id, broker_name, selected, settings, resolved, quote_price,
        quality, lot_size, live_order, live_cash, state,
    ):
        underlying, side = _candidate(selected)
        if underlying in CORRELATED_INDICES and side in {"CE", "PE"}:
            block = _active_exact_cooldown(conn, user_id, underlying, side)
            if block:
                return _set_block(
                    state, COOLDOWN_REASON, underlying, side,
                    message="Loss ke baad same index aur same side 15 minute blocked hai.",
                    **block,
                )

            current_mode = "live" if str(settings.get("trading_mode", "paper")).lower() == "live" else "paper"
            rows = [
                row for row in runtime._open_rows(conn, user_id)
                if _mode(row) == current_mode and str(runtime._underlying(row) or "").upper() in CORRELATED_INDICES
            ]
            # User choice: correlated indices may hold more than one trade in
            # the same direction. The exact same-index/side post-loss cooldown
            # above remains a hard 15-minute safety rule.
            opposite = [
                row for row in rows
                if str(_value(row, "side", "")).upper() in {"CE", "PE"}
                and str(_value(row, "side", "")).upper() != side
            ]
            if opposite:
                row = opposite[0]
                existing_underlying = str(runtime._underlying(row) or "").upper()
                existing_pnl = _open_net_pnl(row)
                if existing_underlying == underlying:
                    return _set_block(
                        state, HEDGE_SAME_INDEX_REASON, underlying, side,
                        existing_trade_id=_value(row, "id"), existing_pnl=existing_pnl,
                    )
                if existing_pnl is None or existing_pnl >= 0:
                    return _set_block(
                        state, HEDGE_NOT_LOSING_REASON, underlying, side,
                        message="Opposite correlated hedge tabhi allowed hai jab existing position loss me ho.",
                        existing_trade_id=_value(row, "id"),
                        existing_underlying=existing_underlying,
                        existing_side=str(_value(row, "side", "")).upper(),
                        existing_pnl=existing_pnl,
                        live_loss_confirmed=existing_pnl is not None,
                    )

        return previous_open(
            conn, user_id, broker_name, selected, settings, resolved, quote_price,
            quality, lot_size, live_order, live_cash, state,
        )

    def close_guarded(conn, user_id, trade, price, reason, order_id=None):
        result = previous_close(conn, user_id, trade, price, reason, order_id)
        _register_exact_cooldown(conn, user_id, trade, reason, price)
        return result

    runtime._open_common = open_guarded
    runtime._close = close_guarded
    runtime._okai_final_correlated_risk_guard_v1 = True
    runtime._okai_final_correlated_risk_guard_version = VERSION
    return True


__all__ = [
    "COOLDOWN_REASON", "COOLDOWN_SECONDS", "CORRELATED_INDICES",
    "HEDGE_NOT_LOSING_REASON", "SAME_DIRECTION_REASON", VERSION,
    "apply_final_correlated_risk_guard",
]

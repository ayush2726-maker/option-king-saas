"""Persistent post-loss re-entry guard for AUTO Portfolio.

Fixes the live/paper failure mode where an option exits on ATR SL/loss and AUTO
opens the same index+side again in the same loop/minute because the previous
scan was already calculated before the exit.  The guard is persistent in SQLite
and also checks _open_common, so same-cycle re-entry is blocked immediately.

Risk-control only: scoring, SL calculation, quantity sizing, exit rules, and
order execution mechanics are unchanged.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Mapping, Optional

from bot import auto_portfolio_runtime as runtime
from bot import strategy


COOLDOWN_SECONDS = int(getattr(strategy, "LOSS_COOLDOWN_SECONDS", 15 * 60))
BLOCK_REASON = "POST_ATR_SL_SAME_SIDE_COOLDOWN_15M"
VERSION = "OKAI-POST-LOSS-REENTRY-GUARD-V2"


def _now_utc():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_dt(value: Any) -> Optional[datetime]:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _ensure_guard_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_reentry_blocks (
            user_id INTEGER NOT NULL,
            underlying TEXT NOT NULL,
            side TEXT NOT NULL,
            blocked_until TEXT NOT NULL,
            source_trade_id INTEGER,
            reason TEXT,
            created_at TEXT NOT NULL,
            previous_symbol TEXT,
            previous_pnl REAL,
            previous_exit_reason TEXT,
            PRIMARY KEY (user_id, underlying, side)
        )
        """
    )
    for name, kind in (
        ("previous_symbol", "TEXT"),
        ("previous_pnl", "REAL"),
        ("previous_exit_reason", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE auto_reentry_blocks ADD COLUMN {name} {kind}")
        except Exception:
            pass
    conn.commit()


def _loss_or_sl(reason: Any, pnl: Any) -> bool:
    text = str(reason or "").upper()
    return (
        _f(pnl, 0.0) < 0
        or "PURE ATR SL" in text
        or "SL HIT" in text
        or "STOP" in text
        or "LOSS" in text
    )


def _register_loss_block(conn, user_id, trade, reason, exit_price=None):
    qty = max(1, runtime._i(runtime._v(trade, "qty", 1), 1))
    pnl = round((_f(exit_price, runtime._v(trade, "last_ltp", 0)) - _f(runtime._v(trade, "entry_price", 0))) * qty, 2)
    if not _loss_or_sl(reason, pnl):
        return

    underlying = runtime._underlying(trade)
    side = str(runtime._v(trade, "side", "") or "").upper()
    if side not in ("CE", "PE"):
        return

    _ensure_guard_schema(conn)
    now = _now_utc()
    blocked_until = now + timedelta(seconds=max(60, COOLDOWN_SECONDS))
    conn.execute(
        """
        INSERT INTO auto_reentry_blocks (
            user_id, underlying, side, blocked_until,
            source_trade_id, reason, created_at,
            previous_symbol, previous_pnl, previous_exit_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, underlying, side)
        DO UPDATE SET
            blocked_until=excluded.blocked_until,
            source_trade_id=excluded.source_trade_id,
            reason=excluded.reason,
            created_at=excluded.created_at,
            previous_symbol=excluded.previous_symbol,
            previous_pnl=excluded.previous_pnl,
            previous_exit_reason=excluded.previous_exit_reason
        """,
        (
            int(user_id),
            underlying,
            side,
            _iso(blocked_until),
            runtime._i(runtime._v(trade, "id", 0), 0),
            BLOCK_REASON,
            _iso(now),
            str(runtime._v(trade, "symbol", "") or ""),
            pnl,
            str(reason or "")[:240],
        ),
    )
    conn.commit()


def _active_blocks(user_id, conn=None):
    close_after = False
    if conn is None:
        conn = runtime.get_db()
        close_after = True
    try:
        _ensure_guard_schema(conn)
        now_text = _iso(_now_utc())
        conn.execute(
            "DELETE FROM auto_reentry_blocks WHERE blocked_until <= ?",
            (now_text,),
        )
        conn.commit()
        rows = conn.execute(
            """
            SELECT underlying, side, blocked_until, source_trade_id, reason,
                   previous_symbol, previous_pnl, previous_exit_reason
            FROM auto_reentry_blocks
            WHERE user_id=? AND blocked_until > ?
            """,
            (int(user_id), now_text),
        ).fetchall()
        return {
            (
                str(row["underlying"] or "").upper(),
                str(row["side"] or "").upper(),
            ): {
                "version": VERSION,
                "blocked_until": row["blocked_until"],
                "source_trade_id": row["source_trade_id"],
                "reason": row["reason"] or BLOCK_REASON,
                "previous_symbol": row["previous_symbol"],
                "previous_pnl": row["previous_pnl"],
                "previous_exit_reason": row["previous_exit_reason"],
            }
            for row in rows
        }
    finally:
        if close_after:
            conn.close()


def _block_for_candidate(conn, user_id, underlying, side):
    if side not in ("CE", "PE"):
        return None
    return _active_blocks(user_id, conn).get((str(underlying or "").upper(), side))


def _block_scan(scan, block):
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return scan

    signal = dict(scan.get("signal_data") or {})
    candidate = str(
        signal.get("candidate_signal") or signal.get("signal") or "WAIT"
    ).upper()
    if candidate not in ("CE", "PE"):
        return scan

    reasons = list(signal.get("safety_gate_reasons") or [])
    fresh = list(signal.get("fresh_entry_block_reasons") or [])
    warnings = list(signal.get("warnings") or [])
    for collection in (reasons, fresh, warnings):
        if BLOCK_REASON not in collection:
            collection.append(BLOCK_REASON)

    signal.update({
        "signal": "WAIT",
        "trade_allowed": False,
        "safety_gate_passed": False,
        "fresh_entry_ok": False,
        "safety_gate_reasons": reasons,
        "fresh_entry_block_reasons": fresh,
        "warnings": warnings,
        "post_loss_reentry_blocked": True,
        "post_loss_reentry_reason": BLOCK_REASON,
        "post_loss_reentry_blocked_until": block["blocked_until"],
        "post_loss_source_trade_id": block.get("source_trade_id"),
        "post_loss_previous_symbol": block.get("previous_symbol"),
        "post_loss_previous_pnl": block.get("previous_pnl"),
    })
    scan["signal_data"] = signal
    market = scan.get("market_data") or {}
    market["signal"] = "WAIT"
    scan["market_data"] = market
    scan["entry_block_reason"] = BLOCK_REASON
    return scan


def _apply_blocks(user_id, scans):
    blocks = _active_blocks(user_id)
    output = []
    for scan in scans or []:
        if not isinstance(scan, dict):
            output.append(scan)
            continue
        signal = scan.get("signal_data") or {}
        candidate = str(
            signal.get("candidate_signal") or signal.get("signal") or "WAIT"
        ).upper()
        key = (str(scan.get("underlying") or "").upper(), candidate)
        block = blocks.get(key)
        output.append(_block_scan(scan, block) if block else scan)
    return output


def _state_guard_payload(block, underlying, side):
    until = _parse_dt(block.get("blocked_until"))
    remaining = max(1, int((until - _now_utc()).total_seconds())) if until else None
    return {
        "allowed": False,
        "version": VERSION,
        "reason": BLOCK_REASON,
        "message": "SL/loss ke turant baad same index+side re-entry blocked.",
        "underlying": underlying,
        "side": side,
        "blocked_until": block.get("blocked_until"),
        "remaining_seconds": remaining,
        "source_trade_id": block.get("source_trade_id"),
        "previous_symbol": block.get("previous_symbol"),
        "previous_pnl": block.get("previous_pnl"),
        "previous_exit_reason": block.get("previous_exit_reason"),
    }


def apply_post_loss_reentry_guard_patch():
    if getattr(runtime, "_okai_post_loss_reentry_guard_v2", False):
        return

    original_ensure_schema = runtime._ensure_schema
    original_close = runtime._close
    original_scan_angel = runtime._scan_angel
    original_scan_multi = runtime._scan_multi
    original_summary = runtime._summary
    original_open_common = runtime._open_common

    def ensure_schema(conn):
        original_ensure_schema(conn)
        _ensure_guard_schema(conn)

    def close_with_reentry_block(conn, user_id, trade, price, reason, order_id=None):
        result = original_close(
            conn,
            user_id,
            trade,
            price,
            reason,
            order_id,
        )
        _register_loss_block(conn, user_id, trade, reason, exit_price=price)
        return result

    def scan_angel_with_cooldown(user_id, *args, **kwargs):
        return _apply_blocks(
            user_id,
            original_scan_angel(user_id, *args, **kwargs),
        )

    def scan_multi_with_cooldown(user_id, *args, **kwargs):
        return _apply_blocks(
            user_id,
            original_scan_multi(user_id, *args, **kwargs),
        )

    def open_common_with_same_cycle_guard(
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
        try:
            signal = dict(selected.get("signal_data") or {})
            underlying = str(selected.get("underlying") or "").upper()
            side = str(signal.get("signal") or "").upper()
            block = _block_for_candidate(conn, user_id, underlying, side)
            if block:
                payload = _state_guard_payload(block, underlying, side)
                if isinstance(state, dict):
                    state["entry_guard"] = payload
                    state["post_loss_reentry_guard"] = payload
                    state["last_entry_block_reason"] = BLOCK_REASON
                return False
        except Exception as exc:
            if isinstance(state, dict):
                state["post_loss_reentry_guard_error"] = f"{type(exc).__name__}:{str(exc)[:160]}"

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

    def summary_with_cooldown(scan):
        summary = dict(original_summary(scan) or {})
        signal = scan.get("signal_data") or {}
        if signal.get("post_loss_reentry_blocked"):
            summary.update({
                "status": "SAFETY_BLOCKED",
                "signal": "WAIT",
                "candidate_signal": BLOCK_REASON,
                "trade_allowed": False,
                "entry_block_reason": BLOCK_REASON,
                "post_loss_reentry_blocked": True,
                "post_loss_reentry_blocked_until": signal.get(
                    "post_loss_reentry_blocked_until"
                ),
                "post_loss_previous_symbol": signal.get("post_loss_previous_symbol"),
                "post_loss_previous_pnl": signal.get("post_loss_previous_pnl"),
            })
        return summary

    runtime._ensure_schema = ensure_schema
    runtime._close = close_with_reentry_block
    runtime._scan_angel = scan_angel_with_cooldown
    runtime._scan_multi = scan_multi_with_cooldown
    runtime._summary = summary_with_cooldown
    runtime._open_common = open_common_with_same_cycle_guard
    runtime._okai_post_loss_reentry_guard_v1 = True
    runtime._okai_post_loss_reentry_guard_v2 = True

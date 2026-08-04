"""Final normal-AUTO opening and post-loss circuit breaker.

This patch closes two unsafe runtime gaps:

1. Normal AUTO could enter from 09:15 while the configured opening range is
   still being formed through 09:30.  Normal AUTO now waits for the completed
   opening range.  Hero Zero uses its separate route/window and is untouched.
2. A losing position could be reopened in the same index and direction before
   the intended cooldown was reliably visible on the final wrapped close path.
   The final close path now persists a 15-minute same-index+side block.  Two
   consecutive losing closes also activate a global 15-minute entry cooldown.

No score weights, threshold, quantity, ATR stop, profit lock, broker execution,
manual exit, EOD exit or Hero Zero logic is changed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bot import auto_portfolio_runtime as runtime


PATCH_VERSION = "OPENING_ORB_LOSS_CIRCUIT_V1"
NORMAL_AUTO_START_MINUTE = 9 * 60 + 30
COOLDOWN_SECONDS = 15 * 60
OPENING_BLOCK_REASON = "AUTO_ENTRY_BLOCKED_UNTIL_ORB_COMPLETE_0930_IST"
SAME_SIDE_BLOCK_REASON = "POST_LOSS_SAME_INDEX_SIDE_COOLDOWN_15M"
GLOBAL_BLOCK_REASON = "TWO_CONSECUTIVE_LOSSES_GLOBAL_COOLDOWN_15M"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_ist() -> datetime:
    return runtime._now_ist()


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse(value: Any) -> Optional[datetime]:
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


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_final_reentry_blocks (
            user_id INTEGER NOT NULL,
            underlying TEXT NOT NULL,
            side TEXT NOT NULL,
            blocked_until TEXT NOT NULL,
            source_trade_id INTEGER,
            previous_pnl REAL,
            previous_reason TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, underlying, side)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_final_loss_circuit (
            user_id INTEGER PRIMARY KEY,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            blocked_until TEXT,
            source_trade_id INTEGER,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _opening_ready(value: Optional[datetime] = None) -> bool:
    current = value or _now_ist()
    minute = current.hour * 60 + current.minute
    return bool(current.weekday() < 5 and minute >= NORMAL_AUTO_START_MINUTE)


def _trade_pnl(trade: Any, exit_price: float) -> float:
    qty = max(1, _i(runtime._v(trade, "qty", 1), 1))
    entry = _f(runtime._v(trade, "entry_price", 0), 0)
    return round((_f(exit_price, 0) - entry) * qty, 2)


def _record_close_outcome(
    conn,
    user_id: int,
    trade: Any,
    exit_price: float,
    reason: Any,
    now: Optional[datetime] = None,
) -> dict:
    _ensure_schema(conn)
    current = (now or _now_utc()).astimezone(timezone.utc)
    now_text = _iso(current)
    pnl = _trade_pnl(trade, exit_price)
    trade_id = _i(runtime._v(trade, "id", 0), 0)
    underlying = runtime._underlying(trade)
    side = str(runtime._v(trade, "side", "") or "").upper()
    loss = pnl < -1e-9

    if loss and side in {"CE", "PE"}:
        blocked_until = _iso(current + timedelta(seconds=COOLDOWN_SECONDS))
        conn.execute(
            """
            INSERT INTO auto_final_reentry_blocks (
                user_id, underlying, side, blocked_until,
                source_trade_id, previous_pnl, previous_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, underlying, side)
            DO UPDATE SET
                blocked_until=excluded.blocked_until,
                source_trade_id=excluded.source_trade_id,
                previous_pnl=excluded.previous_pnl,
                previous_reason=excluded.previous_reason,
                created_at=excluded.created_at
            """,
            (
                int(user_id),
                str(underlying).upper(),
                side,
                blocked_until,
                trade_id,
                pnl,
                str(reason or "")[:240],
                now_text,
            ),
        )

    row = conn.execute(
        """
        SELECT consecutive_losses, blocked_until
        FROM auto_final_loss_circuit
        WHERE user_id=?
        """,
        (int(user_id),),
    ).fetchone()
    previous_count = _i(row["consecutive_losses"] if row else 0, 0)

    if loss:
        count = previous_count + 1
        global_until = (
            _iso(current + timedelta(seconds=COOLDOWN_SECONDS))
            if count >= 2
            else None
        )
    else:
        count = 0
        global_until = None

    conn.execute(
        """
        INSERT INTO auto_final_loss_circuit (
            user_id, consecutive_losses, blocked_until,
            source_trade_id, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            consecutive_losses=excluded.consecutive_losses,
            blocked_until=excluded.blocked_until,
            source_trade_id=excluded.source_trade_id,
            updated_at=excluded.updated_at
        """,
        (int(user_id), count, global_until, trade_id, now_text),
    )
    conn.commit()
    return {
        "loss": loss,
        "pnl": pnl,
        "consecutive_losses": count,
        "same_side_blocked": bool(loss and side in {"CE", "PE"}),
        "global_blocked": bool(global_until),
        "global_blocked_until": global_until,
    }


def _same_side_block(conn, user_id: int, underlying: str, side: str) -> Optional[dict]:
    if str(side).upper() not in {"CE", "PE"}:
        return None
    _ensure_schema(conn)
    now = _now_utc()
    now_text = _iso(now)
    conn.execute(
        "DELETE FROM auto_final_reentry_blocks WHERE blocked_until <= ?",
        (now_text,),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT blocked_until, source_trade_id, previous_pnl, previous_reason
        FROM auto_final_reentry_blocks
        WHERE user_id=? AND underlying=? AND side=? AND blocked_until>?
        """,
        (
            int(user_id),
            str(underlying or "").upper(),
            str(side or "").upper(),
            now_text,
        ),
    ).fetchone()
    if row is None:
        return None
    until = _parse(row["blocked_until"])
    remaining = max(1, int((until - now).total_seconds())) if until else None
    return {
        "reason": SAME_SIDE_BLOCK_REASON,
        "blocked_until": row["blocked_until"],
        "remaining_seconds": remaining,
        "source_trade_id": row["source_trade_id"],
        "previous_pnl": row["previous_pnl"],
        "previous_reason": row["previous_reason"],
    }


def _global_block(conn, user_id: int) -> Optional[dict]:
    _ensure_schema(conn)
    now = _now_utc()
    now_text = _iso(now)
    row = conn.execute(
        """
        SELECT consecutive_losses, blocked_until, source_trade_id
        FROM auto_final_loss_circuit
        WHERE user_id=?
        """,
        (int(user_id),),
    ).fetchone()
    if row is None:
        return None
    until = _parse(row["blocked_until"])
    if not until or until <= now:
        if row["blocked_until"]:
            conn.execute(
                """
                UPDATE auto_final_loss_circuit
                SET consecutive_losses=0, blocked_until=NULL, updated_at=?
                WHERE user_id=?
                """,
                (now_text, int(user_id)),
            )
            conn.commit()
        return None
    return {
        "reason": GLOBAL_BLOCK_REASON,
        "blocked_until": row["blocked_until"],
        "remaining_seconds": max(1, int((until - now).total_seconds())),
        "consecutive_losses": _i(row["consecutive_losses"], 0),
        "source_trade_id": row["source_trade_id"],
    }


def _mark_block(state: Any, reason: str, payload: Optional[dict] = None) -> None:
    if not isinstance(state, dict):
        return
    details = dict(payload or {})
    details.update(
        {
            "allowed": False,
            "reason": reason,
            "version": PATCH_VERSION,
        }
    )
    state["entry_guard"] = details
    state["last_entry_block_reason"] = reason
    state["entry_block_reason"] = reason
    state["opening_orb_loss_circuit"] = details
    state["selected_for_entry"] = None


def apply_opening_orb_loss_circuit_patch() -> None:
    if getattr(runtime, "_okai_opening_orb_loss_circuit_v1", False):
        return

    previous_ensure_schema = runtime._ensure_schema
    previous_can_enter = runtime._can_enter
    previous_open_common = runtime._open_common
    previous_close = runtime._close

    def ensure_schema(conn):
        previous_ensure_schema(conn)
        _ensure_schema(conn)

    def can_enter_with_final_circuit(conn, user_id, settings, rows, state):
        current = _now_ist()
        if not _opening_ready(current):
            _mark_block(
                state,
                OPENING_BLOCK_REASON,
                {
                    "entry_start_ist": "09:30",
                    "current_time_ist": current.strftime("%H:%M:%S"),
                    "orb_window_ist": "09:15-09:30",
                },
            )
            return False
        global_block = _global_block(conn, user_id)
        if global_block:
            _mark_block(state, GLOBAL_BLOCK_REASON, global_block)
            return False
        return previous_can_enter(conn, user_id, settings, rows, state)

    def open_common_with_final_circuit(
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
        current = _now_ist()
        if not _opening_ready(current):
            _mark_block(
                state,
                OPENING_BLOCK_REASON,
                {
                    "entry_start_ist": "09:30",
                    "current_time_ist": current.strftime("%H:%M:%S"),
                    "orb_window_ist": "09:15-09:30",
                },
            )
            return False

        global_block = _global_block(conn, user_id)
        if global_block:
            _mark_block(state, GLOBAL_BLOCK_REASON, global_block)
            return False

        signal = dict((selected or {}).get("signal_data") or {})
        underlying = str((selected or {}).get("underlying") or "").upper()
        side = str(signal.get("signal") or signal.get("candidate_signal") or "").upper()
        same_side = _same_side_block(conn, user_id, underlying, side)
        if same_side:
            _mark_block(state, SAME_SIDE_BLOCK_REASON, same_side)
            return False

        return previous_open_common(
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

    def close_with_final_circuit(
        conn,
        user_id,
        trade,
        price,
        reason,
        order_id=None,
    ):
        result = previous_close(
            conn,
            user_id,
            trade,
            price,
            reason,
            order_id,
        )
        try:
            outcome = _record_close_outcome(
                conn,
                user_id,
                trade,
                price,
                reason,
            )
            if outcome.get("loss"):
                setattr(runtime, "_okai_last_loss_circuit_outcome", outcome)
        except Exception as exc:
            setattr(
                runtime,
                "_okai_last_loss_circuit_error",
                f"{type(exc).__name__}:{str(exc)[:180]}",
            )
        return result

    runtime._ensure_schema = ensure_schema
    runtime._can_enter = can_enter_with_final_circuit
    runtime._open_common = open_common_with_final_circuit
    runtime._close = close_with_final_circuit
    runtime._okai_opening_orb_loss_circuit_v1 = True
    runtime._okai_opening_orb_loss_circuit_version = PATCH_VERSION

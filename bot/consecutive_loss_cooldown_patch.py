"""User-level cooldown after two consecutive losing AUTO trades.

The older guard only reacted to PURE ATR SL on the same index and same side.
Losses closed by structural reversal, EOD, manual exit, or another side/index
could therefore be followed by a new position in the same monitor cycle.  This
patch is installed last and blocks every fresh AUTO entry for 15 minutes after
any two consecutive net losing trades during the same IST trading day.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot import auto_portfolio_runtime as runtime


IST = ZoneInfo("Asia/Kolkata")
COOLDOWN_MINUTES = 15
BLOCK_REASON = "TWO_CONSECUTIVE_LOSSES_GLOBAL_COOLDOWN_15M"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_user_cooldowns (
            user_id INTEGER PRIMARY KEY,
            blocked_until TEXT NOT NULL,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            source_trade_id INTEGER,
            reason TEXT NOT NULL,
            trading_day_ist TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _parse(value) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _today_bounds_utc(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    current = (now or _now_utc()).astimezone(IST)
    start_ist = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end_ist = start_ist + timedelta(days=1)
    return (
        start_ist.astimezone(timezone.utc),
        end_ist.astimezone(timezone.utc),
        start_ist.date().isoformat(),
    )


def _row_loss_value(row) -> float:
    try:
        keys = set(row.keys())
    except Exception:
        keys = set()
    for key in ("net_pnl", "pnl"):
        try:
            if key in keys and row[key] is not None:
                return float(row[key])
        except Exception:
            pass
    return 0.0


def _consecutive_losses_today(conn, user_id: int) -> tuple[int, int | None]:
    start_utc, end_utc, _ = _today_bounds_utc()
    rows = conn.execute(
        """
        SELECT * FROM paper_trades
        WHERE user_id=?
          AND UPPER(COALESCE(status, ''))='CLOSED'
          AND datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
        ORDER BY id DESC
        LIMIT 25
        """,
        (
            int(user_id),
            start_utc.strftime("%Y-%m-%d %H:%M:%S"),
            end_utc.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ).fetchall()

    count = 0
    latest_trade_id = None
    for row in rows:
        value = _row_loss_value(row)
        if value < 0:
            count += 1
            if latest_trade_id is None:
                try:
                    latest_trade_id = int(row["id"])
                except Exception:
                    latest_trade_id = None
            continue
        break
    return count, latest_trade_id


def _register_after_close(conn, user_id: int, trade) -> None:
    _ensure_schema(conn)
    count, latest_trade_id = _consecutive_losses_today(conn, user_id)
    _, _, trading_day = _today_bounds_utc()

    if count < 2:
        # A winning/breakeven trade resets an old expired streak marker.
        if count == 0:
            conn.execute(
                "DELETE FROM auto_user_cooldowns WHERE user_id=?",
                (int(user_id),),
            )
            conn.commit()
        return

    now = _now_utc()
    blocked_until = now + timedelta(minutes=COOLDOWN_MINUTES)
    source_id = latest_trade_id
    if source_id is None:
        try:
            source_id = int(runtime._v(trade, "id", 0) or 0)
        except Exception:
            source_id = None

    conn.execute(
        """
        INSERT INTO auto_user_cooldowns (
            user_id, blocked_until, consecutive_losses,
            source_trade_id, reason, trading_day_ist, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            blocked_until=excluded.blocked_until,
            consecutive_losses=excluded.consecutive_losses,
            source_trade_id=excluded.source_trade_id,
            reason=excluded.reason,
            trading_day_ist=excluded.trading_day_ist,
            updated_at=excluded.updated_at
        """,
        (
            int(user_id),
            _iso(blocked_until),
            int(count),
            source_id,
            BLOCK_REASON,
            trading_day,
            _iso(now),
        ),
    )
    conn.commit()


def _active_block(conn, user_id: int) -> dict | None:
    _ensure_schema(conn)
    row = conn.execute(
        """
        SELECT blocked_until, consecutive_losses, source_trade_id,
               reason, trading_day_ist
        FROM auto_user_cooldowns
        WHERE user_id=?
        """,
        (int(user_id),),
    ).fetchone()
    if not row:
        return None

    blocked_until = _parse(row["blocked_until"])
    _, _, today_key = _today_bounds_utc()
    if (
        blocked_until is None
        or blocked_until <= _now_utc()
        or str(row["trading_day_ist"] or "") != today_key
    ):
        conn.execute(
            "DELETE FROM auto_user_cooldowns WHERE user_id=?",
            (int(user_id),),
        )
        conn.commit()
        return None

    return {
        "blocked_until": _iso(blocked_until),
        "consecutive_losses": int(row["consecutive_losses"] or 0),
        "source_trade_id": row["source_trade_id"],
        "reason": str(row["reason"] or BLOCK_REASON),
    }


def _mark_state(state: dict | None, block: dict) -> None:
    if not isinstance(state, dict):
        return
    state.update(
        {
            "global_loss_cooldown": True,
            "global_loss_cooldown_reason": block["reason"],
            "global_loss_cooldown_until": block["blocked_until"],
            "consecutive_losses": block["consecutive_losses"],
            "selected_for_entry": None,
            "entry_block_reason": block["reason"],
        }
    )


def _clear_state(state: dict | None) -> None:
    if not isinstance(state, dict):
        return
    state["global_loss_cooldown"] = False
    state.pop("global_loss_cooldown_reason", None)
    state.pop("global_loss_cooldown_until", None)
    state.pop("consecutive_losses", None)


def apply_consecutive_loss_cooldown_patch() -> None:
    if getattr(runtime, "_okai_consecutive_loss_cooldown_v1", False):
        return

    original_ensure_schema = runtime._ensure_schema
    original_close = runtime._close
    original_can_enter = runtime._can_enter
    original_open_common = runtime._open_common

    def ensure_schema(conn):
        original_ensure_schema(conn)
        _ensure_schema(conn)

    def close_with_global_cooldown(
        conn,
        user_id,
        trade,
        price,
        reason,
        order_id=None,
    ):
        result = original_close(
            conn,
            user_id,
            trade,
            price,
            reason,
            order_id,
        )
        _register_after_close(conn, user_id, trade)
        return result

    def can_enter_with_global_cooldown(conn, user_id, settings, rows, state):
        block = _active_block(conn, user_id)
        if block:
            _mark_state(state, block)
            return False
        _clear_state(state)
        return original_can_enter(conn, user_id, settings, rows, state)

    def open_common_with_global_cooldown(
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
        block = _active_block(conn, user_id)
        if block:
            _mark_state(state, block)
            return False
        _clear_state(state)
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

    runtime._ensure_schema = ensure_schema
    runtime._close = close_with_global_cooldown
    runtime._can_enter = can_enter_with_global_cooldown
    runtime._open_common = open_common_with_global_cooldown
    runtime._okai_consecutive_loss_cooldown_v1 = True

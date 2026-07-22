"""Persistent post-loss re-entry guard for AUTO Portfolio.

The setup score measures indicator agreement; it is not a win probability. A
strong trend can therefore remain 100/82 for several candles even after an
option trade has hit its ATR stop. AUTO Portfolio previously allowed the same
index and same option side to be opened again on the very next scan because the
existing 15-minute loss-cooldown helper was not wired into the portfolio loop.

This patch keeps PAPER unlimited observation and LIVE daily limits unchanged,
but blocks only the stopped index+side for 15 minutes after PURE ATR SL. Other
indices and the opposite side remain eligible. The block is stored in SQLite so
it survives a bot/server restart.
"""

from datetime import datetime, timezone, timedelta

from bot import auto_portfolio_runtime as runtime
from bot import strategy


COOLDOWN_SECONDS = int(getattr(strategy, "LOSS_COOLDOWN_SECONDS", 15 * 60))
BLOCK_REASON = "POST_ATR_SL_SAME_SIDE_COOLDOWN_15M"


def _now_utc():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat()


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
            PRIMARY KEY (user_id, underlying, side)
        )
        """
    )
    conn.commit()


def _register_loss_block(conn, user_id, trade, reason):
    text = str(reason or "").upper()
    if "PURE ATR SL" not in text:
        return

    underlying = runtime._underlying(trade)
    side = str(runtime._v(trade, "side", "") or "").upper()
    if side not in ("CE", "PE"):
        return

    now = _now_utc()
    blocked_until = now + timedelta(seconds=COOLDOWN_SECONDS)
    conn.execute(
        """
        INSERT INTO auto_reentry_blocks (
            user_id, underlying, side, blocked_until,
            source_trade_id, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, underlying, side)
        DO UPDATE SET
            blocked_until=excluded.blocked_until,
            source_trade_id=excluded.source_trade_id,
            reason=excluded.reason,
            created_at=excluded.created_at
        """,
        (
            int(user_id),
            underlying,
            side,
            _iso(blocked_until),
            runtime._i(runtime._v(trade, "id", 0), 0),
            BLOCK_REASON,
            _iso(now),
        ),
    )
    conn.commit()


def _active_blocks(user_id):
    conn = runtime.get_db()
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
            SELECT underlying, side, blocked_until, source_trade_id, reason
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
                "blocked_until": row["blocked_until"],
                "source_trade_id": row["source_trade_id"],
                "reason": row["reason"] or BLOCK_REASON,
            }
            for row in rows
        }
    finally:
        conn.close()


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


def apply_post_loss_reentry_guard_patch():
    if getattr(runtime, "_okai_post_loss_reentry_guard_v1", False):
        return

    original_ensure_schema = runtime._ensure_schema
    original_close = runtime._close
    original_scan_angel = runtime._scan_angel
    original_scan_multi = runtime._scan_multi
    original_summary = runtime._summary

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
        _register_loss_block(conn, user_id, trade, reason)
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
            })
        return summary

    runtime._ensure_schema = ensure_schema
    runtime._close = close_with_reentry_block
    runtime._scan_angel = scan_angel_with_cooldown
    runtime._scan_multi = scan_multi_with_cooldown
    runtime._summary = summary_with_cooldown
    runtime._okai_post_loss_reentry_guard_v1 = True

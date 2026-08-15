"""Keep one canonical post-loss cooldown and disable overlapping blockers.

The live runtime accumulated several independently persisted cooldowns over
time. A single losing close could therefore create same-side 12/15 minute,
same-index 20 minute and global/rest-of-day blocks at once. That made a later
score-qualified setup look inexplicably blocked.

This final-order patch keeps exactly one policy:

``POST_ATR_SL_SAME_SIDE_COOLDOWN_15M``

It is scoped to the same user, index and CE/PE side. Opening-time, score,
contract/LTP, position sizing and broker/order safeguards are untouched.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any


VERSION = "OKAI-CANONICAL-COOLDOWN-DEDUP-V1"
CANONICAL_REASON = "POST_ATR_SL_SAME_SIDE_COOLDOWN_15M"
CANONICAL_SECONDS = 15 * 60

DUPLICATE_REASONS = {
    "CONSECUTIVE_SAME_SIDE_LOSS_BLOCK",
    "SAME_INDEX_SIDE_COOLDOWN_15M",
    "POST_LOSS_REENTRY_BLOCK",
    "POST_LOSS_SAME_INDEX_SIDE_COOLDOWN_15M",
    "TWO_CONSECUTIVE_LOSSES_GLOBAL_COOLDOWN_15M",
    "POST_RISK_EXIT_SAME_SIDE_COOLDOWN_12M",
    "POST_RISK_EXIT_SAME_INDEX_COOLDOWN_20M_AFTER_2",
    "POST_RISK_EXIT_INDEX_BLOCK_REST_OF_DAY_AFTER_3",
    "POST_ATR_SL_SAME_SIDE_COOLDOWN_30M",
    "POST_ATR_SL_SAME_SIDE_BLOCK_REST_OF_DAY_AFTER_2",
}


def _reason(value: Any) -> str:
    return str(value or "").strip().upper()


def _filter_active_blocks(blocks: Any) -> dict:
    """Ignore only known duplicate cooldown rows; preserve unrelated safety."""
    if not isinstance(blocks, dict):
        return {}
    return {
        key: value
        for key, value in blocks.items()
        if _reason((value or {}).get("reason")) not in DUPLICATE_REASONS
    }


def _canonical_register_loss_block(
    conn,
    user_id: int,
    trade: Any,
    reason: Any,
    exit_price: Any = None,
) -> None:
    """Persist one 15-minute block for the same index and option side only."""
    from bot import post_loss_reentry_guard_patch as guard

    runtime = guard.runtime
    qty = max(1, runtime._i(runtime._v(trade, "qty", 1), 1))
    exit_value = guard._f(
        exit_price,
        runtime._v(trade, "last_ltp", 0),
    )
    entry = guard._f(runtime._v(trade, "entry_price", 0), 0)
    pnl = round((exit_value - entry) * qty, 2)
    if not guard._loss_or_sl(reason, pnl):
        return

    underlying = runtime._underlying(trade)
    side = str(runtime._v(trade, "side", "") or "").upper()
    if side not in {"CE", "PE"}:
        return

    guard._ensure_guard_schema(conn)
    now = guard._now_utc()
    blocked_until = now + timedelta(seconds=CANONICAL_SECONDS)

    # Old rows may include both sides or a rest-of-day expiry. They are no
    # longer authoritative and must not survive as a hidden parallel block.
    placeholders = ",".join("?" for _ in DUPLICATE_REASONS)
    conn.execute(
        f"""
        DELETE FROM auto_reentry_blocks
        WHERE user_id=? AND underlying=?
          AND UPPER(COALESCE(reason, '')) IN ({placeholders})
        """,
        (
            int(user_id),
            str(underlying).upper(),
            *sorted(DUPLICATE_REASONS),
        ),
    )
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
            str(underlying).upper(),
            side,
            guard._iso(blocked_until),
            runtime._i(runtime._v(trade, "id", 0), 0),
            CANONICAL_REASON,
            guard._iso(now),
            str(runtime._v(trade, "symbol", "") or ""),
            pnl,
            str(reason or "")[:240],
        ),
    )
    conn.commit()


def _non_blocking_loss_observation(*args, **kwargs) -> dict:
    """Opening circuit may keep its timing role but not create loss blocks."""
    return {
        "loss": False,
        "same_side_blocked": False,
        "global_blocked": False,
        "cooldown_owner": CANONICAL_REASON,
        "version": VERSION,
    }


def _no_block(*args, **kwargs):
    return None


def apply_canonical_cooldown_dedup_patch() -> bool:
    from bot import auto_portfolio_runtime as runtime
    from bot import balanced_exit_cooldown_v4_patch as balanced
    from bot import consecutive_loss_cooldown_patch as consecutive
    from bot import opening_orb_loss_circuit_patch as opening
    from bot import post_loss_reentry_guard_patch as guard

    if getattr(runtime, "_okai_canonical_cooldown_dedup_v1", False):
        return True

    original_active_blocks = guard._active_blocks

    def canonical_active_blocks(user_id, conn=None):
        return _filter_active_blocks(original_active_blocks(user_id, conn))

    guard.COOLDOWN_SECONDS = CANONICAL_SECONDS
    guard.BLOCK_REASON = CANONICAL_REASON
    guard.VERSION = VERSION
    guard._register_loss_block = _canonical_register_loss_block
    guard._active_blocks = canonical_active_blocks

    # Balanced V4 still owns exits/profit locks. Only its overlapping cooldown
    # registration is redirected to the one canonical post-loss owner.
    balanced.FIRST_LOSS_COOLDOWN_MINUTES = 15
    balanced.FIRST_LOSS_REASON = CANONICAL_REASON
    balanced._register_balanced_loss_block = _canonical_register_loss_block

    # The opening circuit continues to enforce ORB completion at 09:30 only.
    opening._same_side_block = _no_block
    opening._global_block = _no_block
    opening._record_close_outcome = _non_blocking_loss_observation
    opening.POST_LOSS_BLOCKS_DISABLED = True

    # Disable the separate all-index cooldown after two losses, including when
    # an older wrapper was already installed earlier in the startup chain.
    consecutive._active_block = _no_block
    consecutive._register_after_close = _no_block
    consecutive.DUPLICATE_COOLDOWN_DISABLED = True

    try:
        from backtest import post_loss_reentry_cooldown_patch as backtest_guard

        backtest_guard.COOLDOWN_MINUTES = 15
        backtest_guard.COOLDOWN_REASON = CANONICAL_REASON
    except Exception:
        pass

    runtime._okai_canonical_cooldown_dedup_v1 = True
    runtime._okai_canonical_cooldown_reason = CANONICAL_REASON
    runtime._okai_canonical_cooldown_seconds = CANONICAL_SECONDS
    return True


__all__ = [
    "CANONICAL_REASON",
    "CANONICAL_SECONDS",
    "DUPLICATE_REASONS",
    "VERSION",
    "_filter_active_blocks",
    "apply_canonical_cooldown_dedup_patch",
]

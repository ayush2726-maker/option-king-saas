"""Paper-only unlimited trade observation mode.

Purpose: keep collecting qualified PAPER entries after trade 5 so we can compare
whether trades 6+ add profit or loss. LIVE remains hard-capped at five entries
per day. Existing two-open-position, different-index, score, ATR SL, charges and
profit-lock rules are unchanged.
"""

from datetime import timezone

from bot import auto_portfolio_runtime as runtime


LIVE_MAX_TRADES_PER_DAY = 5
FIRST_BUCKET_SIZE = 5


def _mode_from_settings(settings):
    return (
        "live"
        if str((settings or {}).get("trading_mode", "paper")).lower() == "live"
        else "paper"
    )


def _day_start_utc_text():
    return runtime._now_ist().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _today_rows(conn, user_id, mode):
    return conn.execute(
        """
        SELECT id, status, pnl, created_at
        FROM paper_trades
        WHERE user_id=?
          AND COALESCE(trading_mode, 'paper')=?
          AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) ASC, id ASC
        """,
        (int(user_id), str(mode), _day_start_utc_text()),
    ).fetchall()


def _today_count(conn, user_id, mode):
    return len(_today_rows(conn, user_id, mode))


def _bucket_stats(rows):
    closed = [row for row in rows if str(row["status"] or "").upper() == "CLOSED"]
    first = closed[:FIRST_BUCKET_SIZE]
    later = closed[FIRST_BUCKET_SIZE:]

    def summarize(group):
        pnls = [float(row["pnl"] or 0.0) for row in group]
        return {
            "closed_trades": len(group),
            "wins": sum(1 for pnl in pnls if pnl > 0),
            "losses": sum(1 for pnl in pnls if pnl < 0),
            "flat": sum(1 for pnl in pnls if pnl == 0),
            "net_pnl": round(sum(pnls), 2),
        }

    return {
        "first_5": summarize(first),
        "after_5": summarize(later),
    }


def _ensure_observation_schema(conn):
    for name, kind in (
        ("daily_trade_number", "INTEGER"),
        ("observation_bucket", "TEXT"),
    ):
        try:
            conn.execute(
                f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}"
            )
        except Exception:
            pass
    conn.commit()


def _can_enter_observation(conn, user_id, settings, rows, state):
    if len(rows) >= runtime.MAX_OPEN_POSITIONS or state.get("live_order_lock"):
        return False

    mode = _mode_from_settings(settings)
    count = _today_count(conn, user_id, mode)

    state["daily_trade_mode"] = mode.upper()
    state["daily_trade_count"] = count

    if mode == "paper":
        state["daily_trade_limit"] = None
        state["paper_unlimited_observation"] = True
        state["trade_limit_status"] = "PAPER_UNLIMITED_OBSERVATION"
        return True

    configured = max(
        1,
        runtime._i((settings or {}).get("max_trades_per_day", 5), 5),
    )
    live_limit = min(configured, LIVE_MAX_TRADES_PER_DAY)
    state["daily_trade_limit"] = live_limit
    state["paper_unlimited_observation"] = False
    state["trade_limit_status"] = "LIVE_HARD_CAP_5"
    return count < live_limit


def apply_paper_unlimited_observation_patch():
    if getattr(runtime, "_okai_paper_unlimited_observation_v1", False):
        return

    original_ensure_schema = runtime._ensure_schema
    original_insert = runtime._insert
    original_state_update = runtime._state_update

    def ensure_schema(conn):
        original_ensure_schema(conn)
        _ensure_observation_schema(conn)

    def tagged_insert(
        conn,
        user_id,
        broker_name,
        resolved,
        underlying,
        side,
        entry,
        sizing,
        score,
        spot,
        atr,
        mode,
        slot,
        capital_base,
        order_id=None,
    ):
        _ensure_observation_schema(conn)
        day_number = _today_count(conn, user_id, mode) + 1
        bucket = "FIRST_5" if day_number <= FIRST_BUCKET_SIZE else "AFTER_5"

        trade_id = original_insert(
            conn,
            user_id,
            broker_name,
            resolved,
            underlying,
            side,
            entry,
            sizing,
            score,
            spot,
            atr,
            mode,
            slot,
            capital_base,
            order_id,
        )
        if trade_id:
            conn.execute(
                """
                UPDATE paper_trades
                SET daily_trade_number=?,
                    observation_bucket=?,
                    reason=COALESCE(reason, '') || ?
                WHERE id=?
                """,
                (
                    day_number,
                    bucket,
                    f" | DAY_TRADE_{day_number} | {bucket}",
                    trade_id,
                ),
            )
            conn.commit()
        return trade_id

    def state_update_with_observation(state, scans, selected, settings, rows):
        original_state_update(state, scans, selected, settings, rows)
        mode = _mode_from_settings(settings)
        user_id = int(state.get("user_id") or 0)
        conn = runtime.get_db()
        try:
            _ensure_observation_schema(conn)
            day_rows = _today_rows(conn, user_id, mode)
            stats = _bucket_stats(day_rows)
        finally:
            conn.close()

        state["daily_trade_observation"] = {
            "mode": mode.upper(),
            "total_entries": len(day_rows),
            "open_trades": sum(
                1 for row in day_rows
                if str(row["status"] or "").upper() == "OPEN"
            ),
            "paper_unlimited": mode == "paper",
            "live_max_trades": LIVE_MAX_TRADES_PER_DAY,
            **stats,
        }

    runtime._ensure_schema = ensure_schema
    runtime._insert = tagged_insert
    runtime._can_enter = _can_enter_observation
    runtime._state_update = state_update_with_observation
    runtime._okai_paper_unlimited_observation_v1 = True

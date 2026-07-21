"""
Persist AUTO portfolio score snapshots per instrument and expose the latest
live score on today's chart candles.

AUTO scans NIFTY, BANKNIFTY and SENSEX independently. The normal bot state only
keeps one display scan, so this patch stores every completed scan once per
candle. It also attaches the current display scan score to the newest live
candle, which gives the Today graph an immediate fallback even before saved
history has accumulated.
"""

from datetime import datetime, timezone

from database import get_db
from bot import auto_portfolio_runtime as runtime


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _ensure_signal_history(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            instrument TEXT,
            price REAL,
            score INTEGER,
            signal TEXT,
            adx REAL,
            volume_ratio REAL,
            engine_updated_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_signal_history_user_date
        ON signal_history(user_id, created_at DESC)
        """
    )


def _persist_scan_scores(state, scans):
    user_id = _safe_int(state.get("user_id"), 0)
    if user_id <= 0:
        return

    conn = get_db()
    try:
        _ensure_signal_history(conn)

        for scan in scans or []:
            if scan.get("status") != "OK":
                continue

            instrument = str(scan.get("underlying") or "").upper().strip()
            if not instrument:
                continue

            signal_data = scan.get("signal_data") or {}
            market_data = scan.get("market_data") or {}
            candle_id = str(scan.get("candle_id") or "").strip()
            if not candle_id:
                continue

            snapshot_key = f"AUTO:{instrument}:{candle_id}"
            existing = conn.execute(
                """
                SELECT id FROM signal_history
                WHERE user_id=? AND instrument=? AND engine_updated_at=?
                LIMIT 1
                """,
                (user_id, instrument, snapshot_key),
            ).fetchone()
            if existing:
                continue

            display_signal = (
                signal_data.get("signal")
                if signal_data.get("signal") in ("CE", "PE")
                else signal_data.get("candidate_signal", "WAIT")
            )

            conn.execute(
                """
                INSERT INTO signal_history (
                    user_id, instrument, price, score, signal,
                    adx, volume_ratio, engine_updated_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    instrument,
                    _safe_float(market_data.get("price"), 0),
                    _safe_int(signal_data.get("score"), 0),
                    str(display_signal or "WAIT"),
                    _safe_float(market_data.get("adx"), 0),
                    _safe_float(market_data.get("volume_ratio"), 0),
                    snapshot_key,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        conn.execute(
            """
            DELETE FROM signal_history
            WHERE user_id=?
              AND datetime(created_at) < datetime('now', '-35 days')
            """,
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _attach_current_score_to_live_chart(state):
    """Attach the latest computed score to the newest live candle.

    The live chart candle builder contains OHLC/indicators but not strategy
    score. The mobile Today view can therefore show candles while reporting
    zero score points. Adding only the newest completed candle keeps the value
    honest and gives an immediate graph/tag fallback; normal per-candle history
    continues to come from signal_history.
    """
    candles = state.get("chart_candles")
    if not isinstance(candles, list) or not candles:
        return

    updated = list(candles)
    latest = dict(updated[-1] or {})
    latest["score"] = _safe_int(state.get("score"), 0)
    latest["signal"] = str(
        state.get("candidate_signal")
        or state.get("signal")
        or "WAIT"
    )
    latest["trade_allowed"] = bool(state.get("trade_allowed", False))
    latest["min_score"] = _safe_int(state.get("min_score"), 82)
    latest["score_source"] = "LIVE_CURRENT"
    updated[-1] = latest
    state["chart_candles"] = updated


def apply_score_history_patch():
    """Patch AUTO runtime once without changing strategy or entry rules."""
    if getattr(runtime, "_okai_score_history_patch_v2", False):
        return

    original_state_update = runtime._state_update

    def patched_state_update(state, scans, selected, settings, rows):
        original_state_update(state, scans, selected, settings, rows)
        _attach_current_score_to_live_chart(state)
        try:
            _persist_scan_scores(state, scans)
            state.pop("score_history_warning", None)
        except Exception as exc:
            state["score_history_warning"] = str(exc)[:160]

    runtime._state_update = patched_state_update
    runtime._okai_score_history_patch_v2 = True

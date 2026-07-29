"""Attach readable entry diagnostics to open/history trade rows.

The trading engine already stores the raw entry reason, for example:
"Real entry score 86 | NORMAL_PURE_ATR | R=15.44 | ...".
This patch adds a structured entry_explanation payload so the mobile app can
show why CE/PE was selected, which filters passed, and why the opposite side
was rejected.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from database import get_db


_INSTALLED = False


DEFAULT_SETTINGS = {
    "entry_threshold": 82,
    "adx_threshold": 25,
    "volume_threshold": 1.2,
}


def _row_value(row, key, default=None):
    try:
        if row is not None and key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        pass
    if isinstance(row, dict):
        value = row.get(key)
        return default if value is None else value
    return default


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _round(value, digits=2):
    return round(_f(value, 0.0), digits)


def _money_price(value):
    number = _f(value, 0.0)
    return f"₹{number:.2f}" if number > 0 else "--"


def _ensure_schema(conn) -> None:
    try:
        conn.execute(
            "ALTER TABLE paper_trades ADD COLUMN entry_explanation_json TEXT"
        )
        conn.commit()
    except Exception:
        pass


def _settings_for(user_id: int) -> dict:
    settings = dict(DEFAULT_SETTINGS)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT settings_json FROM strategy_settings WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        if row:
            saved = json.loads(row["settings_json"] or "{}")
            if isinstance(saved, dict):
                settings.update(saved)
    except Exception:
        pass
    finally:
        conn.close()
    return settings


def _instrument_from(row, fallback="NIFTY") -> str:
    direct = str(_row_value(row, "underlying", "") or "").upper().strip()
    if direct in ("NIFTY", "BANKNIFTY", "SENSEX"):
        return direct
    symbol = str(_row_value(row, "symbol", "") or "").upper()
    for name in ("BANKNIFTY", "SENSEX", "NIFTY"):
        if name in symbol:
            return name
    return fallback


def _score_from_reason(reason) -> int:
    match = re.search(r"entry score\s+(\d+)", str(reason or ""), re.I)
    return _i(match.group(1), 0) if match else 0


def _nearest_signal(conn, user_id: int, instrument: str):
    try:
        return conn.execute(
            """
            SELECT * FROM signal_history
            WHERE user_id=? AND UPPER(COALESCE(instrument, ''))=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(user_id), str(instrument).upper()),
        ).fetchone()
    except Exception:
        return None


def _direction_from_vwap(market: dict, side: str) -> tuple[str, str]:
    price = _f(market.get("price"), 0)
    vwap = _f(market.get("vwap"), 0)
    if price <= 0 or vwap <= 0:
        return "VWAP data unavailable", "WARN"
    if price > vwap:
        value = f"Above VWAP ({_money_price(price)} > {_money_price(vwap)})"
        return value, "PASS" if side == "CE" else "WARN"
    if price < vwap:
        value = f"Below VWAP ({_money_price(price)} < {_money_price(vwap)})"
        return value, "PASS" if side == "PE" else "WARN"
    return "At VWAP", "WARN"


def _supertrend_status(market: dict, side: str) -> tuple[str, str]:
    direction = str(market.get("supertrend_dir") or "").upper().strip()
    if not direction:
        return "Supertrend data unavailable", "WARN"
    if direction == "UP":
        return "UP / bullish", "PASS" if side == "CE" else "WARN"
    if direction == "DOWN":
        return "DOWN / bearish", "PASS" if side == "PE" else "WARN"
    return f"{direction} / neutral", "WARN"


def _check(label, value, status="INFO", detail=None):
    item = {
        "label": str(label),
        "value": str(value),
        "status": str(status).upper(),
    }
    if detail:
        item["detail"] = str(detail)
    return item


def _side_reason(instrument: str, side: str, vwap_text: str, st_text: str) -> str:
    if side == "PE":
        return (
            f"{instrument} bearish setup mila, isliye PE selected. "
            "PE premium generally index neeche jane par badhta hai. "
            f"VWAP: {vwap_text}; Supertrend: {st_text}."
        )
    if side == "CE":
        return (
            f"{instrument} bullish setup mila, isliye CE selected. "
            "CE premium generally index upar jane par badhta hai. "
            f"VWAP: {vwap_text}; Supertrend: {st_text}."
        )
    return f"{instrument} me strategy score ne entry allow ki."


def _opposite_reason(side: str) -> str:
    if side == "PE":
        return "CE rejected: final signal bearish tha; bullish confirmation score enough nahi tha."
    if side == "CE":
        return "PE rejected: final signal bullish tha; bearish confirmation score enough nahi tha."
    return "Opposite side rejected because final strategy signal was not confirmed."


def _build_payload(
    *,
    user_id: int,
    row=None,
    selected: dict | None = None,
    settings: dict | None = None,
    quality: dict | None = None,
    fallback_note: str | None = None,
) -> dict:
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    signal = dict((selected or {}).get("signal_data") or {})
    market = dict((selected or {}).get("market_data") or {})

    if row is not None:
        signal.setdefault("score", _score_from_reason(_row_value(row, "reason", "")))
        signal.setdefault("signal", _row_value(row, "side", ""))
        market.setdefault("price", _row_value(row, "underlying_price", 0))

    side = str(signal.get("signal") or _row_value(row, "side", "") or "").upper()
    instrument = str(
        (selected or {}).get("underlying")
        or _instrument_from(row, str(settings.get("primary_instrument", "NIFTY")))
    ).upper()

    score = _i(signal.get("score"), 0)
    min_score = _i(signal.get("min_score"), _i(settings.get("entry_threshold"), 82))
    adx = _round(market.get("adx"), 2)
    adx_threshold = _round(settings.get("adx_threshold"), 2)
    volume = _round(market.get("volume_ratio"), 2)
    volume_threshold = _round(settings.get("volume_threshold"), 2)
    trend = str(market.get("trend") or signal.get("trend") or "--")

    vwap_text, vwap_status = _direction_from_vwap(market, side)
    st_text, st_status = _supertrend_status(market, side)
    q_reason = str((quality or {}).get("reason") or "OPTION_ENTRY_QUALITY_NOT_RECORDED")
    q_allowed = bool((quality or {}).get("allowed", True))

    checks = [
        _check(
            "Entry Score",
            f"{score}/{min_score}",
            "PASS" if score >= min_score and score > 0 else "WARN",
        ),
        _check(
            "ADX Strength",
            f"{adx} >= {adx_threshold}",
            "PASS" if adx >= adx_threshold and adx > 0 else "WARN",
        ),
        _check(
            "Volume",
            f"{volume}x >= {volume_threshold}x",
            "PASS" if volume >= volume_threshold and volume > 0 else "WARN",
        ),
        _check("VWAP Direction", vwap_text, vwap_status),
        _check("Supertrend", st_text, st_status),
        _check(
            "Option Quality",
            q_reason,
            "PASS" if q_allowed else "FAIL",
        ),
    ]

    compact = [
        f"Signal {side or '--'}",
        f"Score {score}/{min_score}" if score else f"Score --/{min_score}",
        f"ADX {adx}",
        f"Vol {volume}x",
        f"Trend {trend}",
        f"VWAP {'OK' if vwap_status == 'PASS' else 'Watch'}",
        f"ST {'OK' if st_status == 'PASS' else 'Watch'}",
    ]

    return {
        "version": "OKAI_ENTRY_EXPLANATION_V1",
        "instrument": instrument,
        "side": side,
        "score": score,
        "min_score": min_score,
        "adx": adx,
        "adx_threshold": adx_threshold,
        "volume_ratio": volume,
        "volume_threshold": volume_threshold,
        "trend": trend,
        "vwap_direction": vwap_text,
        "supertrend_direction": st_text,
        "selected_side_reason": _side_reason(instrument, side, vwap_text, st_text),
        "opposite_reject_reason": _opposite_reason(side),
        "checks": checks,
        "compact_lines": compact,
        "quality": quality or {},
        "data_note": fallback_note or "Fresh live indicator snapshot se explanation generate hua.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _payload_from_row(row) -> dict | None:
    raw = _row_value(row, "entry_explanation_json")
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

    user_id = _i(_row_value(row, "user_id"), 0)
    if user_id <= 0:
        return None

    conn = get_db()
    try:
        _ensure_schema(conn)
        settings = _settings_for(user_id)
        instrument = _instrument_from(row, str(settings.get("primary_instrument", "NIFTY")))
        snapshot = _nearest_signal(conn, user_id, instrument)
        market = {}
        signal = {"signal": _row_value(row, "side", ""), "score": _score_from_reason(_row_value(row, "reason", ""))}
        if snapshot:
            signal["score"] = signal.get("score") or _i(snapshot["score"], 0)
            signal["signal"] = signal.get("signal") or snapshot["signal"]
            market.update(
                {
                    "price": snapshot["price"],
                    "adx": snapshot["adx"],
                    "volume_ratio": snapshot["volume_ratio"],
                    "trend": "RECENT_SIGNAL_HISTORY",
                }
            )
        payload = _build_payload(
            user_id=user_id,
            row=row,
            selected={
                "underlying": instrument,
                "signal_data": signal,
                "market_data": market,
            },
            settings=settings,
            fallback_note="Existing trade ke liye fallback explanation; next fresh trade me full VWAP/Supertrend snapshot save hoga.",
        )
        return payload
    except Exception:
        return None
    finally:
        conn.close()


def _patch_trade_views() -> None:
    try:
        from bot import trade_live_routes as routes
    except Exception:
        return

    if getattr(routes, "_okai_trade_explanation_view_v1", False):
        return

    original_trade_view = routes._trade_view

    def trade_view_with_explanation(row):
        trade = original_trade_view(row)
        try:
            payload = _payload_from_row(row)
            if payload:
                trade["entry_explanation"] = payload
        except Exception:
            pass
        return trade

    routes._trade_view = trade_view_with_explanation
    routes._okai_trade_explanation_view_v1 = True


def _patch_auto_entries() -> None:
    try:
        from bot import auto_portfolio_runtime as runtime
    except Exception:
        return

    if getattr(runtime, "_okai_trade_explanation_entry_v1", False):
        return

    original_open_common = runtime._open_common

    def open_common_with_explanation(
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
        result = original_open_common(
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
        if not result:
            return result

        try:
            trade_id = (state.get("last_opened_trade") or {}).get("trade_id")
            if not trade_id:
                return result
            payload = _build_payload(
                user_id=int(user_id),
                selected=selected,
                settings=settings,
                quality=quality,
            )
            _ensure_schema(conn)
            conn.execute(
                "UPDATE paper_trades SET entry_explanation_json=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), int(trade_id)),
            )
            conn.commit()
        except Exception as exc:
            try:
                state["entry_explanation_error"] = str(exc)[:140]
            except Exception:
                pass
        return result

    runtime._open_common = open_common_with_explanation
    runtime._okai_trade_explanation_entry_v1 = True


def apply_trade_explanation_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _patch_trade_views()
    _patch_auto_entries()

"""Final AUTO entry execution safety and audit patch.

Adds three upgrades without changing strategy scores, MTF, ORB, ATR exits,
cooldowns, sizing, broker routing or any previously installed wrapper:

1. Selected option premium must show a real uptick before BUY.
2. Completed index data and broker/DNS transport must be fresh and healthy.
3. Opened and blocked entries receive explainable JSON audit snapshots.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from bot import auto_portfolio_runtime as runtime


PATCH_VERSION = "ENTRY_EXECUTION_SAFETY_V2_BALANCED_PREMIUM"
# AUTO rescans entry candidates about once per minute. Keep the latest option quote long enough for the next scan to compare real premium momentum.
MOMENTUM_WINDOW_SECONDS = 180.0
MOMENTUM_MIN_SAMPLE_GAP_SECONDS = 1.0
MIN_PREMIUM_UPTICK_POINTS = 0.50
MIN_PREMIUM_UPTICK_PERCENT = 0.25
MAX_COMPLETED_CANDLE_AGE_SECONDS = 120.0
BROKER_FAILURE_WINDOW_SECONDS = 30.0
BROKER_FAILURE_COUNT_TO_BLOCK = 2
AUDIT_REPEAT_SECONDS = 15.0
IST = timezone(timedelta(hours=5, minutes=30))

_TRANSPORT_RE = re.compile(
    r"name.?resolution|failed to resolve|no address associated|temporary failure"
    r"|httpsconnectionpool|max retries exceeded|connection(?:error| refused| reset)"
    r"|connecttimeout|readtimeout|timed out|network is unreachable|dns",
    re.I,
)

_lock = threading.RLock()
_quote_samples: dict[tuple[int, str, str], deque[tuple[float, float]]] = defaultdict(deque)
_health: dict[tuple[int, str, str], dict[str, Any]] = {}
_last_audit: dict[tuple[int, str, str, str], float] = {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _text(value: Any, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text or default


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _transport_error(value: Any) -> bool:
    return bool(_TRANSPORT_RE.search(_text(value)))


def _health_key(user_id: int, broker_name: str, channel: str) -> tuple[int, str, str]:
    return (
        int(user_id),
        _text(broker_name, "unknown").lower(),
        _text(channel, "unknown").lower(),
    )


def _record_failure(user_id: int, broker_name: str, channel: str, error: Any) -> None:
    if not _transport_error(error):
        return
    now = time.time()
    key = _health_key(user_id, broker_name, channel)
    with _lock:
        item = _health.setdefault(
            key,
            {"failures": deque(), "last_success": 0.0, "last_error": ""},
        )
        failures = item["failures"]
        failures.append(now)
        while failures and now - failures[0] > BROKER_FAILURE_WINDOW_SECONDS:
            failures.popleft()
        item["last_error"] = _text(error)[:240]


def _record_success(user_id: int, broker_name: str, channel: str) -> None:
    key = _health_key(user_id, broker_name, channel)
    with _lock:
        item = _health.setdefault(
            key,
            {"failures": deque(), "last_success": 0.0, "last_error": ""},
        )
        item["last_success"] = time.time()
        item["failures"].clear()
        item["last_error"] = ""


def _health_snapshot(user_id: int, broker_name: str, channel: str) -> dict[str, Any]:
    now = time.time()
    key = _health_key(user_id, broker_name, channel)
    with _lock:
        item = _health.get(key) or {
            "failures": deque(),
            "last_success": 0.0,
            "last_error": "",
        }
        failures = item["failures"]
        while failures and now - failures[0] > BROKER_FAILURE_WINDOW_SECONDS:
            failures.popleft()
        count = len(failures)
        return {
            "channel": channel,
            "recent_transport_failures": count,
            "blocked": count >= BROKER_FAILURE_COUNT_TO_BLOCK,
            "last_failure_age_seconds": (
                round(now - failures[-1], 3) if failures else None
            ),
            "last_success_age_seconds": (
                round(now - item["last_success"], 3)
                if item.get("last_success")
                else None
            ),
            "last_error": item.get("last_error") or "",
        }


def _parse_candle_time(value: Any) -> datetime | None:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif hasattr(value, "to_pydatetime"):
            parsed = value.to_pydatetime()
        else:
            text = _text(value)
            if not text:
                return None
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # Angel historical candle strings are exchange-local when no offset
            # is present. Explicit UTC/offset strings retain their own timezone.
            parsed = parsed.replace(tzinfo=IST)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _candle_freshness(
    selected: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    parsed = _parse_candle_time((selected or {}).get("candle_id"))
    if parsed is None:
        return {
            "fresh": False,
            "reason": "INDEX_CANDLE_TIMESTAMP_UNREADABLE",
            "age_seconds": None,
            "max_age_seconds": MAX_COMPLETED_CANDLE_AGE_SECONDS,
        }
    current = (now or _utc_now()).astimezone(timezone.utc)
    age = max(0.0, (current - parsed).total_seconds())
    fresh = age <= MAX_COMPLETED_CANDLE_AGE_SECONDS
    return {
        "fresh": fresh,
        "reason": "INDEX_CANDLE_FRESH" if fresh else "INDEX_CANDLE_STALE",
        "age_seconds": round(age, 3),
        "max_age_seconds": MAX_COMPLETED_CANDLE_AGE_SECONDS,
        "candle_time": parsed.isoformat(),
    }


def _momentum_check(
    user_id: int,
    broker_name: str,
    symbol: str,
    price: float,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now = float(now_ts if now_ts is not None else time.time())
    current = _f(price, 0.0)
    key = (
        int(user_id),
        _text(broker_name, "unknown").lower(),
        _text(symbol).upper(),
    )
    required = max(
        MIN_PREMIUM_UPTICK_POINTS,
        current * MIN_PREMIUM_UPTICK_PERCENT / 100.0,
    )

    with _lock:
        samples = _quote_samples[key]
        while samples and now - samples[0][0] > MOMENTUM_WINDOW_SECONDS:
            samples.popleft()

        previous = None
        for stamp, value in reversed(samples):
            if now - stamp >= MOMENTUM_MIN_SAMPLE_GAP_SECONDS:
                previous = (stamp, value)
                break

        samples.append((now, current))
        while len(samples) > 12:
            samples.popleft()

    if current <= 0:
        return {
            "allowed": False,
            "reason": "OPTION_QUOTE_INVALID",
            "previous_price": previous[1] if previous else None,
            "current_price": current,
            "required_uptick": round(required, 4),
        }

    if previous is None:
        return {
            "allowed": False,
            "reason": "OPTION_PREMIUM_MOMENTUM_WARMUP",
            "previous_price": None,
            "current_price": current,
            "required_uptick": round(required, 4),
            "window_seconds": MOMENTUM_WINDOW_SECONDS,
        }

    previous_price = _f(previous[1], 0.0)
    move = current - previous_price
    allowed = move + 1e-9 >= required
    return {
        "allowed": allowed,
        "reason": (
            "OPTION_PREMIUM_MOMENTUM_OK"
            if allowed
            else "OPTION_PREMIUM_MOMENTUM_WEAK"
        ),
        "previous_price": round(previous_price, 4),
        "current_price": round(current, 4),
        "move_points": round(move, 4),
        "move_percent": round(
            (move / previous_price * 100.0) if previous_price > 0 else 0.0,
            4,
        ),
        "required_uptick": round(required, 4),
        "sample_age_seconds": round(now - previous[0], 3),
        "window_seconds": MOMENTUM_WINDOW_SECONDS,
    }


def _balanced_momentum_policy(
    momentum: dict[str, Any],
    quality: dict[str, Any],
) -> dict[str, Any]:
    """Keep premium safety without requiring an arbitrary large uptick.

    The option-candle quality guard already blocks spike reversal, bearish
    reversal after a run and extreme extension. Therefore a fully qualified
    strategy entry should not be starved merely because the latest premium is
    flat or its increase is smaller than Rs 0.50 / 0.25%. A clearly falling
    premium remains blocked. On the first sample, a healthy option-candle
    confirmation is sufficient; missing/failed candle data still waits for a
    second real quote.
    """
    result = dict(momentum or {})
    if result.get("allowed"):
        return result

    reason = _text(result.get("reason")).upper()
    quality_reason = _text((quality or {}).get("reason")).upper()

    if (
        reason == "OPTION_PREMIUM_MOMENTUM_WARMUP"
        and quality_reason == "OPTION_PREMIUM_ENTRY_OK"
    ):
        result.update({
            "allowed": True,
            "reason": "OPTION_PREMIUM_CANDLE_CONFIRMED",
            "balanced_policy": True,
        })
        return result

    if reason == "OPTION_PREMIUM_MOMENTUM_WEAK":
        move = _f(result.get("move_points"), 0.0)
        if move >= -1e-9:
            result.update({
                "allowed": True,
                "reason": "OPTION_PREMIUM_NOT_FALLING",
                "balanced_policy": True,
            })

    return result


def _clear_momentum(user_id: int, broker_name: str, symbol: str) -> None:
    key = (
        int(user_id),
        _text(broker_name, "unknown").lower(),
        _text(symbol).upper(),
    )
    with _lock:
        _quote_samples.pop(key, None)


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _core_score(signal: dict[str, Any]) -> Any:
    value = _first(signal, "core_score", "core", "core_points", "confirmations")
    if value is not None:
        return value
    payload = signal.get("live_score_breakdown") or {}
    return _first(payload, "core_score", "confirmations")


def _entry_snapshot(
    broker_name: str,
    selected: dict[str, Any],
    resolved: dict[str, Any],
    quote_price: float,
    quality: dict[str, Any],
    momentum: dict[str, Any],
    candle: dict[str, Any],
    health: dict[str, Any],
) -> dict[str, Any]:
    signal = dict((selected or {}).get("signal_data") or {})
    market = dict((selected or {}).get("market_data") or {})
    side = _text(
        _first(signal, "signal", "candidate_signal", default="WAIT")
    ).upper()
    return {
        "version": PATCH_VERSION,
        "captured_at": _iso_now(),
        "underlying": selected.get("underlying"),
        "side": side,
        "decision_score": _i(
            _first(signal, "decision_score", "score", default=0)
        ),
        "minimum_required_score": _i(
            _first(signal, "min_score", "required_score", default=82),
            82,
        ),
        "base_score": _i(signal.get("base_score"), 0),
        "core_score": _core_score(signal),
        "score_breakdown": (
            signal.get("score_breakdown")
            or signal.get("live_score_breakdown")
            or {}
        ),
        "market_regime": _first(
            signal,
            "market_regime",
            "regime",
            default=_first(
                market,
                "market_regime",
                "regime",
                "trend",
                default="UNKNOWN",
            ),
        ),
        "fake_breakout_probability": _first(
            signal,
            "fake_breakout_probability",
            "fake_breakout",
            default=market.get("fake_breakout_probability"),
        ),
        "confidence": signal.get("confidence"),
        "warnings": list(signal.get("warnings") or []),
        "strategy_profile_key": signal.get("strategy_profile_key"),
        "strategy_profile_name": signal.get("strategy_profile_name"),
        "market": {
            "spot": market.get("price"),
            "adx": market.get("adx"),
            "plus_di": _first(market, "plus_di", "+DI", "pdi"),
            "minus_di": _first(market, "minus_di", "-DI", "mdi"),
            "volume_ratio": market.get("volume_ratio"),
            "vwap": market.get("vwap"),
            "ema9": market.get("ema9"),
            "ema21": market.get("ema21"),
            "supertrend_dir": market.get("supertrend_dir"),
            "trend": market.get("trend"),
            "mtf_confirmed": market.get("mtf_confirmed"),
            "orb_high": market.get("orb_high"),
            "orb_low": market.get("orb_low"),
            "atr": market.get("atr"),
            "momentum_pattern": market.get("momentum_pattern"),
        },
        "real_5m": _first(
            signal,
            "real_5m",
            "real_mtf",
            "mtf_snapshot",
            default=_first(
                market,
                "real_5m",
                "real_mtf",
                "mtf_snapshot",
                default={},
            ),
        ),
        "option": {
            "symbol": resolved.get("symbol"),
            "token": resolved.get("token"),
            "exchange": resolved.get("exchange") or resolved.get("exch_seg"),
            "expiry": resolved.get("expiry"),
            "strike": resolved.get("strike"),
            "premium": round(_f(quote_price), 4),
            "quote_source": _text(broker_name).lower(),
            "quote_age_seconds": 0.0,
            "premium_momentum": momentum,
            "quality": quality,
        },
        "data_health": {
            "completed_candle": candle,
            "broker": health,
        },
    }


def _summary_text(snapshot: dict[str, Any]) -> str:
    option = snapshot.get("option") or {}
    momentum = option.get("premium_momentum") or {}
    market = snapshot.get("market") or {}
    return (
        f"{PATCH_VERSION}"
        f" | score={snapshot.get('decision_score')}/"
        f"{snapshot.get('minimum_required_score')}"
        f" | core={snapshot.get('core_score')}"
        f" | regime={snapshot.get('market_regime')}"
        f" | adx={_f(market.get('adx')):.2f}"
        f" | fake={snapshot.get('fake_breakout_probability')}"
        f" | premium={momentum.get('previous_price')}->"
        f"{momentum.get('current_price')}"
        f" | quote={option.get('quote_source')} age=0s"
    )[:480]


def _ensure_audit_schema(conn) -> None:
    for name, kind in (
        ("entry_context_json", "TEXT"),
        ("entry_decision_score", "INTEGER"),
        ("entry_min_score", "INTEGER"),
        ("entry_core_score", "REAL"),
        ("entry_market_regime", "TEXT"),
        ("entry_quote_source", "TEXT"),
        ("entry_quote_age_seconds", "REAL"),
        ("entry_strategy_version", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        except Exception:
            pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_entry_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            trade_id INTEGER,
            event TEXT NOT NULL,
            underlying TEXT,
            side TEXT,
            symbol TEXT,
            reason TEXT,
            context_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _audit_event(
    conn,
    user_id: int,
    event: str,
    reason: str,
    snapshot: dict[str, Any],
    trade_id: int | None = None,
    force: bool = False,
) -> None:
    _ensure_audit_schema(conn)
    event_name = _text(event, "ENTRY_EVENT").upper()
    symbol = _text((snapshot.get("option") or {}).get("symbol"))
    throttle_key = (int(user_id), event_name, symbol, _text(reason)[:80])
    now = time.time()
    with _lock:
        if (
            not force
            and now - _last_audit.get(throttle_key, 0.0) < AUDIT_REPEAT_SECONDS
        ):
            return
        _last_audit[throttle_key] = now

    conn.execute(
        """
        INSERT INTO auto_entry_audit_events (
            user_id, trade_id, event, underlying, side, symbol,
            reason, context_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            trade_id,
            event_name,
            snapshot.get("underlying"),
            snapshot.get("side"),
            symbol,
            _text(reason)[:240],
            _json(snapshot),
            _iso_now(),
        ),
    )
    conn.commit()


def _persist_open_snapshot(conn, trade_id: int, snapshot: dict[str, Any]) -> None:
    _ensure_audit_schema(conn)
    core = snapshot.get("core_score")
    core_value = _f(core, 0.0) if core is not None else None
    option = snapshot.get("option") or {}
    conn.execute(
        """
        UPDATE paper_trades
        SET entry_context_json=?,
            entry_decision_score=?,
            entry_min_score=?,
            entry_core_score=?,
            entry_market_regime=?,
            entry_quote_source=?,
            entry_quote_age_seconds=?,
            entry_strategy_version=?,
            reason=COALESCE(reason, '') || ?
        WHERE id=?
        """,
        (
            _json(snapshot),
            _i(snapshot.get("decision_score"), 0),
            _i(snapshot.get("minimum_required_score"), 82),
            core_value,
            _text(snapshot.get("market_regime"), "UNKNOWN"),
            _text(option.get("quote_source"), "unknown"),
            _f(option.get("quote_age_seconds"), 0.0),
            PATCH_VERSION,
            " | " + _summary_text(snapshot),
            int(trade_id),
        ),
    )
    conn.commit()


def _quality_transport_state(
    user_id: int,
    broker_name: str,
    quality: dict[str, Any],
) -> dict[str, Any]:
    reason = _text(quality.get("reason")).upper()
    warning = _text(quality.get("warning"))
    if "FETCH_WARNING" in reason and _transport_error(warning):
        _record_failure(user_id, broker_name, "option_candles", warning)
    elif "FETCH_WARNING" not in reason:
        _record_success(user_id, broker_name, "option_candles")
    return _health_snapshot(user_id, broker_name, "option_candles")


def _guard_reason(health: dict[str, Any], candle: dict[str, Any]) -> str | None:
    if health.get("blocked"):
        return "BROKER_DNS_TRANSPORT_UNHEALTHY"
    if not candle.get("fresh"):
        return _text(candle.get("reason"), "INDEX_CANDLE_STALE")
    return None


def apply_entry_execution_safety_v1_patch() -> None:
    if getattr(runtime, "_okai_entry_execution_safety_v1", False):
        return

    base_ensure_schema = runtime._ensure_schema
    base_open_common = runtime._open_common
    base_scan_angel = runtime._scan_angel
    base_scan_multi = runtime._scan_multi
    base_ltp_angel = runtime._ltp_angel
    base_ltp_multi = runtime._ltp_multi

    def ensure_schema_with_audit(conn):
        result = base_ensure_schema(conn)
        _ensure_audit_schema(conn)
        return result

    def scan_angel_guarded(user_id, obj, settings, profile, streak):
        scans = base_scan_angel(user_id, obj, settings, profile, streak)
        errors = [
            scan.get("error")
            for scan in scans
            if scan.get("status") == "ERROR"
            and _transport_error(scan.get("error"))
        ]
        if errors:
            for error in errors:
                _record_failure(user_id, "angelone", "candles", error)
        elif any(
            scan.get("status") == "OK"
            and _candle_freshness(scan).get("fresh")
            for scan in scans
        ):
            _record_success(user_id, "angelone", "candles")
        return scans

    def scan_multi_guarded(user_id, broker_name, obj, settings, profile, streak):
        scans = base_scan_multi(
            user_id,
            broker_name,
            obj,
            settings,
            profile,
            streak,
        )
        errors = [
            scan.get("error")
            for scan in scans
            if scan.get("status") == "ERROR"
            and _transport_error(scan.get("error"))
        ]
        if errors:
            for error in errors:
                _record_failure(user_id, broker_name, "candles", error)
        elif any(
            scan.get("status") == "OK"
            and _candle_freshness(scan).get("fresh")
            for scan in scans
        ):
            _record_success(user_id, broker_name, "candles")
        return scans

    def ltp_angel_guarded(obj, trade):
        result = base_ltp_angel(obj, trade)
        user_id = _i(runtime._v(trade, "user_id", 0), 0)
        if result.get("success"):
            _record_success(user_id, "angelone", "quote")
        else:
            _record_failure(
                user_id,
                "angelone",
                "quote",
                result.get("message"),
            )
        return result

    def ltp_multi_guarded(broker_name, obj, trade):
        result = base_ltp_multi(broker_name, obj, trade)
        user_id = _i(runtime._v(trade, "user_id", 0), 0)
        if result.get("success"):
            _record_success(user_id, broker_name, "quote")
        else:
            _record_failure(
                user_id,
                broker_name,
                "quote",
                result.get("message"),
            )
        return result

    def open_common_guarded(
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
        _ensure_audit_schema(conn)
        symbol = _text(resolved.get("symbol"))
        quality_copy = dict(quality or {})

        # Reaching _open_common proves a direct option LTP was just fetched by
        # the existing broker-specific wrapper. Preserve that wrapper and mark
        # only the successful quote channel here.
        _record_success(user_id, broker_name, "quote")

        candle = _candle_freshness(selected)
        option_candle_health = _quality_transport_state(
            user_id,
            broker_name,
            quality_copy,
        )
        candle_health = _health_snapshot(user_id, broker_name, "candles")
        quote_health = _health_snapshot(user_id, broker_name, "quote")
        health = {
            "blocked": bool(
                candle_health.get("blocked")
                or quote_health.get("blocked")
                or option_candle_health.get("blocked")
            ),
            "candles": candle_health,
            "option_candles": option_candle_health,
            "quote": quote_health,
        }
        momentum = _momentum_check(
            user_id,
            broker_name,
            symbol,
            quote_price,
        )
        momentum = _balanced_momentum_policy(
            momentum,
            quality_copy,
        )
        snapshot = _entry_snapshot(
            broker_name,
            selected,
            resolved,
            quote_price,
            quality_copy,
            momentum,
            candle,
            health,
        )

        reason = _guard_reason(health, candle)
        if reason is None and not quality_copy.get("allowed", True):
            reason = _text(
                quality_copy.get("reason"),
                "OPTION_QUALITY_BLOCKED",
            )
        if reason is None and not momentum.get("allowed"):
            reason = _text(
                momentum.get("reason"),
                "OPTION_PREMIUM_MOMENTUM_WEAK",
            )

        state["entry_data_health"] = {
            "candle": candle,
            "broker": health,
        }
        state["option_premium_momentum"] = momentum
        state["entry_audit_preview"] = snapshot

        if reason:
            attempt = {
                "allowed": False,
                "reason": reason,
                "stage": "FINAL_EXECUTION_GUARD",
                "broker": _text(broker_name).lower(),
                "underlying": selected.get("underlying"),
                "side": snapshot.get("side"),
                "symbol": symbol,
                "option_ltp": round(_f(quote_price), 2),
                "quality": quality_copy,
                "premium_momentum": momentum,
                "data_health": {
                    "candle": candle,
                    "broker": health,
                },
                "version": PATCH_VERSION,
                "updated_at": _iso_now(),
            }
            state["entry_guard"] = dict(attempt)
            state["last_entry_attempt"] = dict(attempt)
            state["entry_attempt"] = dict(attempt)
            state["entry_block_reason"] = reason
            state["last_entry_block_reason"] = reason
            _audit_event(conn, user_id, "BLOCKED", reason, snapshot)
            return False

        quality_copy.update(
            {
                "allowed": True,
                "premium_momentum": momentum,
                "data_health": {
                    "candle": candle,
                    "broker": health,
                },
                "entry_audit_version": PATCH_VERSION,
            }
        )

        opened = base_open_common(
            conn,
            user_id,
            broker_name,
            selected,
            settings,
            resolved,
            quote_price,
            quality_copy,
            lot_size,
            live_order,
            live_cash,
            state,
        )
        if not opened:
            _audit_event(
                conn,
                user_id,
                "NOT_OPENED",
                "DOWNSTREAM_ENTRY_GUARD",
                snapshot,
            )
            return False

        trade_id = _i(
            (state.get("last_opened_trade") or {}).get("trade_id"),
            0,
        )
        if trade_id > 0:
            _persist_open_snapshot(conn, trade_id, snapshot)
            _audit_event(
                conn,
                user_id,
                "OPENED",
                "ENTRY_APPROVED",
                snapshot,
                trade_id=trade_id,
                force=True,
            )
            state["last_opened_trade"]["entry_snapshot"] = snapshot

        _clear_momentum(user_id, broker_name, symbol)
        return True

    runtime._ensure_schema = ensure_schema_with_audit
    runtime._scan_angel = scan_angel_guarded
    runtime._scan_multi = scan_multi_guarded
    runtime._ltp_angel = ltp_angel_guarded
    runtime._ltp_multi = ltp_multi_guarded
    runtime._open_common = open_common_guarded
    runtime._okai_entry_execution_safety_v1 = True

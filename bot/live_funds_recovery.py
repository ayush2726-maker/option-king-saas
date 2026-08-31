"""Reliable Angel LIVE capital reads for static-IP gateway entries.

The Railway runtime normally reads Angel funds directly with ``rmsLimit``.
The owner's local gateway independently publishes the same broker balance
every 30 seconds.  A transient Railway-side Angel response must therefore not
drop an otherwise qualified order while a fresh, positive gateway snapshot is
available.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from database import get_db


DIRECT_FUNDS_ATTEMPTS = 3
DIRECT_FUNDS_RETRY_DELAYS = (0.25, 0.5)
GATEWAY_FUNDS_MAX_AGE_SECONDS = 90


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _available_cash(payload: Any) -> float:
    if isinstance(payload, dict) and payload.get("status") is False:
        return 0.0
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return 0.0
    # If Angel explicitly reports available cash (even zero), never replace it
    # with total/net funds that may already be committed as margin.
    for key in ("availablecash", "availableCash"):
        if key in data:
            return max(0.0, _number(data.get(key), 0.0))
    return max(0.0, _number(data.get("net"), 0.0))


def _payload_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "Angel funds response was not an object"
    return str(
        payload.get("message")
        or payload.get("error")
        or payload.get("errorcode")
        or "Angel funds response did not contain positive available cash"
    )[:180]


def _parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def read_fresh_gateway_funds(
    user_id: int,
    max_age_seconds: int = GATEWAY_FUNDS_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a recent positive funds snapshot published by the live gateway."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM live_broker_funds WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()

    if not row:
        return None
    item = dict(row)
    updated_at = _parse_utc(item.get("updated_at"))
    if updated_at is None:
        return None
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = max(0.0, (current - updated_at).total_seconds())
    available = _number(item.get("available_cash"), 0.0)
    if available <= 0 or age_seconds > max(1, int(max_age_seconds)):
        return None
    return {
        "available_cash": available,
        "used_margin": max(0.0, _number(item.get("used_margin"), 0.0)),
        "total_limit": max(0.0, _number(item.get("total_limit"), available)),
        "broker": str(item.get("broker") or "angelone"),
        "updated_at": str(item.get("updated_at") or ""),
        "age_seconds": round(age_seconds, 3),
    }


def read_live_funds_with_recovery(
    user_id: int,
    obj: Any,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    snapshot_reader: Callable[[int], dict[str, Any] | None] = read_fresh_gateway_funds,
) -> dict[str, Any]:
    """Retry Angel directly, then use only a fresh gateway broker snapshot."""
    attempts: list[dict[str, Any]] = []
    for attempt in range(DIRECT_FUNDS_ATTEMPTS):
        try:
            payload = obj.rmsLimit()
            available = _available_cash(payload)
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "success": available > 0,
                    "available_cash": round(available, 2) if available > 0 else 0.0,
                    "error": "" if available > 0 else _payload_error(payload),
                }
            )
            if available > 0:
                return {
                    "success": True,
                    "available_cash": available,
                    "source": "ANGEL_RMS_DIRECT",
                    "direct_attempts": attempts,
                    "snapshot_age_seconds": None,
                }
        except Exception as exc:
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "success": False,
                    "available_cash": 0.0,
                    "error": f"{type(exc).__name__}: {str(exc)[:150]}",
                }
            )

        if attempt < len(DIRECT_FUNDS_RETRY_DELAYS):
            sleeper(DIRECT_FUNDS_RETRY_DELAYS[attempt])

    try:
        snapshot = snapshot_reader(int(user_id))
    except Exception as exc:
        snapshot = None
        snapshot_error = f"{type(exc).__name__}: {str(exc)[:150]}"
    else:
        snapshot_error = ""

    snapshot_cash = _number((snapshot or {}).get("available_cash"), 0.0)
    snapshot_age = _number(
        (snapshot or {}).get("age_seconds"),
        GATEWAY_FUNDS_MAX_AGE_SECONDS + 1,
    )
    if (
        snapshot_cash > 0
        and 0 <= snapshot_age <= GATEWAY_FUNDS_MAX_AGE_SECONDS
    ):
        return {
            "success": True,
            "available_cash": snapshot_cash,
            "source": "LOCAL_GATEWAY_ANGEL_FRESH_SNAPSHOT",
            "direct_attempts": attempts,
            "snapshot_age_seconds": snapshot_age,
            "snapshot_updated_at": (snapshot or {}).get("updated_at"),
        }

    last_error = attempts[-1]["error"] if attempts else "Angel funds read failed"
    return {
        "success": False,
        "available_cash": 0.0,
        "source": "UNAVAILABLE",
        "reason": "BROKER_FUNDS_UNAVAILABLE",
        "message": snapshot_error or last_error,
        "direct_attempts": attempts,
        "snapshot_age_seconds": None,
    }


__all__ = [
    "DIRECT_FUNDS_ATTEMPTS",
    "GATEWAY_FUNDS_MAX_AGE_SECONDS",
    "read_fresh_gateway_funds",
    "read_live_funds_with_recovery",
]

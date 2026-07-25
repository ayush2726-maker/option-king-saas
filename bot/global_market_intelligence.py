"""Broker-independent global risk snapshot for the Railway shadow AI."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict

# Installs the exact-expiry Upstox chain resolver after broker_intelligence loads.
import bot.upstox_option_intelligence_patch  # noqa: F401

VERSION = "OKAI-GLOBAL-MARKET-INTELLIGENCE-V2"
REFRESH_SECONDS = 180
DEFAULT_SYMBOLS = {
    "sp500": "%5EGSPC",
    "nasdaq": "%5EIXIC",
    "nikkei": "%5EN225",
    "hang_seng": "%5EHSI",
    "crude": "CL%3DF",
    "usd_inr": "USDINR%3DX",
    "india_vix": "%5EINDIAVIX",
    "us_10y": "%5ETNX",
}
_lock = threading.RLock()
_cached: Dict[str, Any] = {}
_cached_mono = 0.0


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _symbols():
    result = dict(DEFAULT_SYMBOLS)
    gift = str(os.getenv("OKAI_GIFT_NIFTY_YAHOO_SYMBOL", "")).strip()
    if gift:
        result["gift_nifty"] = urllib.parse.quote(gift, safe="%")
    raw = str(os.getenv("OKAI_GLOBAL_MARKET_SYMBOLS_JSON", "")).strip()
    if raw:
        try:
            custom = json.loads(raw)
            if isinstance(custom, dict):
                result.update({str(k): str(v) for k, v in custom.items() if v})
        except Exception:
            pass
    return result


def _fetch(name, symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2d&interval=5m"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "OptionKingAI-GlobalMonitor/2.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    result = ((payload.get("chart") or {}).get("result") or [{}])[0]
    meta = result.get("meta") or {}
    price = _f(meta.get("regularMarketPrice"))
    previous = _f(meta.get("chartPreviousClose", meta.get("previousClose")))
    change = (price - previous) / previous * 100.0 if price > 0 and previous > 0 else None
    return {
        "name": name,
        "symbol": symbol,
        "last_price": price,
        "previous_close": previous,
        "change_percent": round(change, 4) if change is not None else None,
        "error": None,
    }


def _risk_summary(values):
    score = 0.0
    for name, weight in (
        ("sp500", 1.2),
        ("nasdaq", 1.1),
        ("nikkei", 0.8),
        ("hang_seng", 0.8),
        ("gift_nifty", 1.4),
    ):
        score += max(-2.5, min(2.5, _f((values.get(name) or {}).get("change_percent")))) * weight
    score -= max(-4.0, min(4.0, _f((values.get("crude") or {}).get("change_percent")))) * 0.45
    score -= max(-1.5, min(1.5, _f((values.get("usd_inr") or {}).get("change_percent")))) * 1.3
    score -= max(-8.0, min(8.0, _f((values.get("india_vix") or {}).get("change_percent")))) * 0.35
    score -= max(-4.0, min(4.0, _f((values.get("us_10y") or {}).get("change_percent")))) * 0.40
    available = sum(_f(item.get("last_price")) > 0 for item in values.values())
    risk = (
        abs(_f((values.get("india_vix") or {}).get("change_percent"))) * 4.0
        + abs(_f((values.get("us_10y") or {}).get("change_percent"))) * 3.0
        + max(0.0, _f((values.get("crude") or {}).get("change_percent"))) * 2.0
        + max(0.0, _f((values.get("usd_inr") or {}).get("change_percent"))) * 5.0
    )
    return {
        "direction": "CE" if score >= 0.9 else "PE" if score <= -0.9 else "NEUTRAL",
        "risk_on_score": round(score, 4),
        "global_risk_score": round(max(0.0, min(100.0, risk)), 2),
        "available_count": available,
        "expected_count": len(values),
        "data_coverage_score": round(available / max(1, len(values)) * 100.0, 2),
        "gift_nifty_configured": "gift_nifty" in values,
    }


def snapshot(force=False):
    global _cached, _cached_mono
    current = time.monotonic()
    with _lock:
        if _cached and not force and current - _cached_mono < REFRESH_SECONDS:
            return dict(_cached)
    values = {}
    for name, symbol in _symbols().items():
        try:
            values[name] = _fetch(name, symbol)
        except Exception as exc:
            values[name] = {
                "name": name,
                "symbol": symbol,
                "last_price": 0.0,
                "previous_close": 0.0,
                "change_percent": None,
                "error": f"{type(exc).__name__}:{str(exc)[:120]}",
            }
    result = {
        "available": any(_f(item.get("last_price")) > 0 for item in values.values()),
        "version": VERSION,
        "source": "BROKER_INDEPENDENT_GLOBAL_QUOTES",
        "values": values,
        "summary": _risk_summary(values),
        "fetched_at": _iso(),
        "trade_blocking": False,
        "order_execution": False,
    }
    with _lock:
        _cached = dict(result)
        _cached_mono = current
    return result


# Runs after this module and advanced_intelligence_v2 have both defined the
# functions that need wrapping. The patch is safe and idempotent.
try:
    from bot.institutional_flow_patch import install as _install_institutional_flow
    _install_institutional_flow()
except Exception as exc:
    print(f"AI INSTITUTIONAL OVERLAY WARNING | {type(exc).__name__}:{str(exc)[:180]}")

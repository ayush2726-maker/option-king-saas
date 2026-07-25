"""Broker-neutral advanced AI intelligence, data collection and shadow learning.

This daemon enriches the existing market AI with option-chain, Greeks, liquidity,
global-market, news and option-premium outcomes. It is deliberately shadow-only:
it cannot block strategy signals or place/modify/close orders.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from auth.utils import decrypt_credential
from broker.selection import get_selected_broker
from bot.brokers.factory import create_broker
from bot.news_intelligence import aggregate as aggregate_news
from bot.shared_ai import predict
from database import get_db, get_db_storage_info

VERSION = "OKAI-ADVANCED-INTELLIGENCE-SHADOW-V2"
HORIZONS = (5, 15, 30)
PRIMARY_HORIZON = 15
POLL_SECONDS = 15
BROKER_REFRESH_SECONDS = 60
GLOBAL_REFRESH_SECONDS = 60
MIN_TRAIN_SAMPLES = 300
MAX_CHAIN_CONTRACTS = 18
RISK_FREE_RATE = float(os.getenv("OKAI_RISK_FREE_RATE", "0.065") or 0.065)
COST_MODEL_VERSION = "ESTIMATED_OPTION_COST_V1"

UNDERLYING_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}
ANGEL_EXCHANGE = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "SENSEX": "BFO"}
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}

_LOCK = threading.RLock()
_STARTED = False
_THREAD: Optional[threading.Thread] = None
_SNAPSHOT_BUILDER: Optional[Callable[[int], Dict[str, Any]]] = None
_INSTANCE_ID = uuid.uuid4().hex[:12]
_LAST_CYCLE_AT: Optional[str] = None
_LAST_ERROR: Optional[str] = None
_LAST_GLOBAL_AT: Optional[str] = None
_GLOBAL_CACHE: Dict[str, Any] = {}
_GLOBAL_CACHE_MONO = 0.0
_BROKER_CACHE: Dict[int, Dict[str, Any]] = {}
_SESSION_CACHE: Dict[int, Dict[str, Any]] = {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return float(default)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")


def _parse_dt(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}" if isinstance(value, dict) else "[]"


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _direction(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"CE", "CALL", "BUY", "UP", "BULLISH", "UPTREND", "LONG"}:
        return "CE"
    if text in {"PE", "PUT", "SELL", "DOWN", "BEARISH", "DOWNTREND", "SHORT"}:
        return "PE"
    if text in {"NO_TRADE", "NO TRADE", "WAIT", "WAITING", "HOLD", "SKIP"}:
        return "NO_TRADE"
    return "NEUTRAL"


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _normal_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _time_to_expiry(expiry: Any) -> float:
    text = str(expiry or "")[:10]
    try:
        expiry_day = datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return max(1.0 / 365.0, 7.0 / 365.0)
    now_ist = _utc_now() + timedelta(hours=5, minutes=30)
    expiry_close = datetime.combine(
        expiry_day, datetime.min.time(), tzinfo=timezone(timedelta(hours=5, minutes=30))
    ).replace(hour=15, minute=30)
    seconds = max(900.0, (expiry_close - now_ist).total_seconds())
    return seconds / (365.0 * 24.0 * 3600.0)


def _bs_price(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    vol: float,
    side: str,
) -> float:
    if min(spot, strike, years, vol) <= 0:
        return 0.0
    root_t = math.sqrt(years)
    d1 = (
        math.log(spot / strike)
        + (rate + 0.5 * vol * vol) * years
    ) / (vol * root_t)
    d2 = d1 - vol * root_t
    disc = math.exp(-rate * years)
    if side == "CE":
        return spot * _normal_cdf(d1) - strike * disc * _normal_cdf(d2)
    return strike * disc * _normal_cdf(-d2) - spot * _normal_cdf(-d1)


def _implied_vol(
    premium: float,
    spot: float,
    strike: float,
    years: float,
    side: str,
) -> float:
    intrinsic = max(0.0, spot - strike) if side == "CE" else max(0.0, strike - spot)
    if premium <= intrinsic or min(spot, strike, years) <= 0:
        return 0.0
    low, high = 0.01, 5.0
    for _ in range(45):
        mid = (low + high) / 2.0
        price = _bs_price(spot, strike, years, RISK_FREE_RATE, mid, side)
        if price > premium:
            high = mid
        else:
            low = mid
    return (low + high) / 2.0


def _greeks(
    premium: float,
    spot: float,
    strike: float,
    expiry: Any,
    side: str,
    supplied_iv: float = 0.0,
) -> Dict[str, float]:
    years = _time_to_expiry(expiry)
    vol = supplied_iv
    if vol > 3.0:
        vol /= 100.0
    if vol <= 0:
        vol = _implied_vol(premium, spot, strike, years, side)
    if min(spot, strike, years, vol) <= 0:
        return {"iv": 0.0, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    root_t = math.sqrt(years)
    d1 = (
        math.log(spot / strike)
        + (RISK_FREE_RATE + 0.5 * vol * vol) * years
    ) / (vol * root_t)
    d2 = d1 - vol * root_t
    pdf = _normal_pdf(d1)
    disc = math.exp(-RISK_FREE_RATE * years)
    delta = _normal_cdf(d1) if side == "CE" else _normal_cdf(d1) - 1.0
    gamma = pdf / (spot * vol * root_t)
    vega = spot * pdf * root_t / 100.0
    first = -(spot * pdf * vol) / (2.0 * root_t)
    if side == "CE":
        theta = (
            first
            - RISK_FREE_RATE * strike * disc * _normal_cdf(d2)
        ) / 365.0
    else:
        theta = (
            first
            + RISK_FREE_RATE * strike * disc * _normal_cdf(-d2)
        ) / 365.0
    return {
        "iv": round(vol * 100.0, 4),
        "delta": round(delta, 6),
        "gamma": round(gamma, 8),
        "theta": round(theta, 6),
        "vega": round(vega, 6),
    }


def _best_depth(depth: Any, side: str) -> Tuple[float, float]:
    if not isinstance(depth, dict):
        return 0.0, 0.0
    rows = depth.get("buy" if side == "bid" else "sell") or []
    if not isinstance(rows, list) or not rows:
        return 0.0, 0.0
    row = rows[0] if isinstance(rows[0], dict) else {}
    return _num(row.get("price")), _num(row.get("quantity"))


def _normalize_contract(
    *,
    broker: str,
    symbol: Any,
    token: Any,
    exchange: Any,
    side: Any,
    strike: Any,
    expiry: Any,
    lot_size: Any,
    quote: Optional[Mapping[str, Any]] = None,
    greek: Optional[Mapping[str, Any]] = None,
    spot: float = 0.0,
) -> Dict[str, Any]:
    q = dict(quote or {})
    g = dict(greek or {})
    option_side = _direction(side)
    if option_side not in {"CE", "PE"}:
        option_side = "CE" if str(side or "").upper().endswith("CE") else "PE"
    ltp = _num(
        q.get("last_price", q.get("ltp", q.get("lastPrice", q.get("close"))))
    )
    depth = q.get("depth") or {}
    bid, bid_qty = _best_depth(depth, "bid")
    ask, ask_qty = _best_depth(depth, "ask")
    bid = _num(q.get("best_bid_price", q.get("bestBidPrice")), bid)
    ask = _num(q.get("best_ask_price", q.get("bestAskPrice")), ask)
    bid_qty = _num(q.get("best_bid_qty", q.get("bestBidQty")), bid_qty)
    ask_qty = _num(q.get("best_ask_qty", q.get("bestAskQty")), ask_qty)
    if bid <= 0:
        bid = ltp
    if ask <= 0:
        ask = ltp
    spread_pct = ((ask - bid) / max(0.05, (ask + bid) / 2.0)) * 100.0
    result = {
        "broker": broker,
        "symbol": str(symbol or ""),
        "token": str(token or ""),
        "exchange": str(exchange or ""),
        "side": option_side,
        "strike": round(_num(strike), 2),
        "expiry": str(expiry or "")[:10],
        "lot_size": max(1, _int(lot_size, 1)),
        "ltp": round(ltp, 4),
        "bid": round(bid, 4),
        "ask": round(ask, 4),
        "bid_qty": round(bid_qty, 2),
        "ask_qty": round(ask_qty, 2),
        "spread_percent": round(max(0.0, spread_pct), 4),
        "oi": _num(q.get("oi", q.get("open_interest", q.get("openInterest")))),
        "volume": _num(q.get("volume", q.get("tradeVolume", q.get("trade_volume")))),
        "previous_close": _num(q.get("cp", q.get("close", q.get("previous_close")))),
    }
    supplied = {
        "iv": _num(g.get("iv", g.get("impliedVolatility", g.get("implied_volatility")))),
        "delta": _num(g.get("delta")),
        "gamma": _num(g.get("gamma")),
        "theta": _num(g.get("theta")),
        "vega": _num(g.get("vega")),
    }
    if not supplied["delta"] and result["ltp"] > 0 and spot > 0:
        supplied.update(
            _greeks(
                result["ltp"],
                spot,
                result["strike"],
                result["expiry"],
                option_side,
                supplied["iv"],
            )
        )
        result["greeks_source"] = "BLACK_SCHOLES_FALLBACK"
    else:
        if supplied["iv"] <= 3.0 and supplied["iv"] > 0:
            supplied["iv"] *= 100.0
        result["greeks_source"] = "BROKER"
    result.update({key: round(_num(value), 6) for key, value in supplied.items()})
    return result


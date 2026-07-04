"""
Option chain resolver for Option King AI SaaS.
Downloads Angel One scrip master, caches a filtered subset (NIFTY/BANKNIFTY/
SENSEX options only) locally, and resolves nearest-expiry ATM option tokens
so real premiums can be fetched via broker.get_ltp().
"""

import json
import os
import time
import urllib.request
from datetime import datetime

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "option_scrips_cache.json")
CACHE_TTL_SECONDS = 12 * 60 * 60  # refresh twice a day

STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "SENSEX": 100,
}

EXCHANGE_FOR = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "SENSEX": "BFO",
}


def _download_and_filter():
    req = urllib.request.Request(SCRIP_MASTER_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())

    filtered = [
        d for d in data
        if d.get("name") in ("NIFTY", "BANKNIFTY", "SENSEX")
        and d.get("instrumenttype") == "OPTIDX"
    ]

    cache = {"cached_at": time.time(), "options": filtered}
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f)

    return filtered


def _load_cache(force_refresh=False):
    if not force_refresh and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r") as f:
                cache = json.load(f)
            if time.time() - cache.get("cached_at", 0) < CACHE_TTL_SECONDS:
                return cache["options"]
        except Exception:
            pass

    try:
        return _download_and_filter()
    except Exception:
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, "r") as f:
                    return json.load(f).get("options", [])
            except Exception:
                pass
        return []


def _parse_expiry(expiry_str):
    try:
        return datetime.strptime(expiry_str, "%d%b%Y")
    except Exception:
        return None


def get_atm_strike(underlying: str, spot_price: float) -> int:
    step = STRIKE_STEP.get(underlying, 50)
    return int(round(spot_price / step) * step)


def resolve_option(underlying: str, spot_price: float, option_type: str):
    """
    Returns dict {token, symbol, exch_seg, strike, expiry} for the nearest
    expiry, ATM (or nearest available) strike option contract.
    Returns None if nothing could be resolved (e.g. scrip master unavailable).
    """
    options = _load_cache()
    if not options:
        return None

    underlying = underlying.upper()
    option_type = option_type.upper()
    strike_target = get_atm_strike(underlying, spot_price)

    candidates = [
        d for d in options
        if d.get("name") == underlying and d.get("symbol", "").endswith(option_type)
    ]
    if not candidates:
        return None

    today = datetime.now()
    expiries = sorted(
        {d["expiry"] for d in candidates if _parse_expiry(d["expiry"]) and _parse_expiry(d["expiry"]) >= today},
        key=lambda e: _parse_expiry(e)
    )
    if not expiries:
        return None
    nearest_expiry = expiries[0]

    same_expiry = [d for d in candidates if d["expiry"] == nearest_expiry]

    def strike_of(d):
        try:
            return float(d["strike"]) / 100.0
        except Exception:
            return None

    best = None
    best_diff = None
    for d in same_expiry:
        s = strike_of(d)
        if s is None:
            continue
        diff = abs(s - strike_target)
        if best is None or diff < best_diff:
            best = d
            best_diff = diff

    if not best:
        return None

    return {
        "token": best["token"],
        "symbol": best["symbol"],
        "exch_seg": best["exch_seg"],
        "strike": strike_of(best),
        "expiry": best["expiry"],
    }

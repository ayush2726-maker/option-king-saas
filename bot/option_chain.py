"""
Option chain resolver for Option King AI SaaS.

Downloads Angel One's active scrip master, caches NIFTY/BANKNIFTY/SENSEX
options and resolves the exact nearest-expiry ATM contract. Historical lookup is
strict: it only succeeds when the contract that should have been nearest on the
trade date is still present in the active master. Expired contracts are never
silently replaced by a later expiry.
"""

import json
import os
import time
import urllib.request
from calendar import monthrange
from datetime import date, datetime, timedelta


SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
CACHE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "option_scrips_cache.json",
)
CACHE_TTL_SECONDS = 12 * 60 * 60

STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "SENSEX": 100,
}

LOT_SIZE_FALLBACK = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "SENSEX": 20,
}

EXCHANGE_FOR = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "SENSEX": "BFO",
}


def _download_and_filter():
    req = urllib.request.Request(
        SCRIP_MASTER_URL,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())

    filtered = [
        row
        for row in data
        if row.get("name") in ("NIFTY", "BANKNIFTY", "SENSEX")
        and row.get("instrumenttype") == "OPTIDX"
    ]

    cache = {"cached_at": time.time(), "options": filtered}
    with open(CACHE_PATH, "w") as handle:
        json.dump(cache, handle)

    return filtered


def _load_cache(force_refresh=False):
    if not force_refresh and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as handle:
                cache = json.load(handle)
            if time.time() - cache.get("cached_at", 0) < CACHE_TTL_SECONDS:
                return cache["options"]
        except Exception:
            pass

    try:
        return _download_and_filter()
    except Exception:
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH) as handle:
                    return json.load(handle).get("options", [])
            except Exception:
                pass
        return []


def _parse_expiry(expiry_value):
    text = str(expiry_value or "").strip()
    for fmt in ("%d%b%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _parse_trade_day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "")[:10]
    return datetime.strptime(text, "%Y-%m-%d").date()


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, monthrange(year, month)[1])
    while last.weekday() != weekday:
        last -= timedelta(days=1)
    return last


def expected_expiry_for_trade_date(underlying: str, trade_day: date) -> date:
    """Return OKAI's configured nearest expiry for the supplied trade date."""
    name = str(underlying or "").upper()

    if name == "BANKNIFTY":
        candidate = _last_weekday(trade_day.year, trade_day.month, 1)
        if candidate >= trade_day:
            return candidate
        next_month = trade_day.replace(day=28) + timedelta(days=4)
        first_next = next_month.replace(day=1)
        return _last_weekday(first_next.year, first_next.month, 1)

    weekday = 3 if name == "SENSEX" else 1
    days_ahead = (weekday - trade_day.weekday()) % 7
    return trade_day + timedelta(days=days_ahead)


def get_atm_strike(underlying: str, spot_price: float) -> int:
    step = STRIKE_STEP.get(str(underlying or "").upper(), 50)
    return int(round(float(spot_price) / step) * step)


def _strike_of(row):
    try:
        return float(row["strike"]) / 100.0
    except Exception:
        return None


def _lot_size_of(row, underlying):
    for key in ("lotsize", "lot_size", "minimumlot"):
        try:
            value = int(float(row.get(key) or 0))
            if value > 0:
                return value
        except Exception:
            pass
    return LOT_SIZE_FALLBACK.get(str(underlying).upper(), 1)


def resolve_option_for_date(
    underlying: str,
    trade_date,
    spot_price: float,
    option_type: str,
):
    """Resolve an exact active-master contract for a historical/current date.

    Angel One's public scrip master contains active contracts only. Therefore an
    older date succeeds only while its then-nearest expiry is still active. A
    later weekly/monthly expiry is not substituted because that would corrupt
    real-premium P&L.
    """
    options = _load_cache()
    if not options:
        return None

    name = str(underlying or "").upper()
    side = str(option_type or "").upper()
    if name not in STRIKE_STEP or side not in ("CE", "PE"):
        return None

    try:
        trade_day = _parse_trade_day(trade_date)
    except Exception:
        return None

    expected = expected_expiry_for_trade_date(name, trade_day)
    strike_target = get_atm_strike(name, spot_price)

    candidates = []
    for row in options:
        if row.get("name") != name:
            continue
        if not str(row.get("symbol") or "").upper().endswith(side):
            continue

        expiry_day = _parse_expiry(row.get("expiry"))
        if expiry_day is None or expiry_day < trade_day:
            continue

        # Holiday-adjusted expiry can shift slightly, but the next weekly expiry
        # must never replace an expired contract from the requested trade date.
        if abs((expiry_day - expected).days) > 3:
            continue

        strike = _strike_of(row)
        if strike is None:
            continue
        candidates.append(
            (
                abs((expiry_day - expected).days),
                expiry_day,
                abs(strike - strike_target),
                strike,
                row,
            )
        )

    if not candidates:
        return None

    _, expiry_day, _, strike, best = min(
        candidates,
        key=lambda item: (item[0], item[1], item[2], item[3]),
    )

    return {
        "token": str(best.get("token") or ""),
        "symbol": str(best.get("symbol") or ""),
        "exch_seg": str(
            best.get("exch_seg")
            or EXCHANGE_FOR.get(name, "NFO")
        ),
        "exchange": str(
            best.get("exch_seg")
            or EXCHANGE_FOR.get(name, "NFO")
        ),
        "strike": strike,
        "expiry": expiry_day.isoformat(),
        "lot_size": _lot_size_of(best, name),
        "underlying": name,
        "option_type": side,
        "selection": "EXPECTED_NEAREST_EXPIRY_ATM_ACTIVE_MASTER",
        "expected_expiry": expected.isoformat(),
    }


def resolve_option(underlying: str, spot_price: float, option_type: str):
    """Resolve today's nearest-expiry ATM option for PAPER/LIVE execution."""
    return resolve_option_for_date(
        underlying=underlying,
        trade_date=datetime.now().date(),
        spot_price=spot_price,
        option_type=option_type,
    )

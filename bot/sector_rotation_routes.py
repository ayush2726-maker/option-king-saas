import csv
import io
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests
from fastapi import APIRouter, Header, Query

from auth.routes import get_current_user
from bot.angel_fetcher import _get_ltp_session
from bot.market_routes import (
    _get_active_broker,
    _get_multi_session,
    _market_session_state,
)


router = APIRouter()

SUPPORTED_INDICES = {"NIFTY", "BANKNIFTY", "SENSEX"}
ROTATION_CACHE_SECONDS = 15.0
UNIVERSE_CACHE_SECONDS = 6 * 60 * 60
RESOLUTION_CACHE_SECONDS = 24 * 60 * 60

NIFTY_CONSTITUENT_URLS = {
    "NIFTY": "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "BANKNIFTY": "https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
}

# Current BSE SENSEX constituent reference set. Quotes are requested on NSE for
# consistency across brokers because all members are cross-listed and NSE symbols
# are supported by Angel One, Upstox and Zerodha market-data APIs.
SENSEX_UNIVERSE = [
    ("ADANIPORTS", "Adani Ports", "Services"),
    ("ASIANPAINT", "Asian Paints", "Consumer"),
    ("AXISBANK", "Axis Bank", "Financial Services"),
    ("BAJFINANCE", "Bajaj Finance", "Financial Services"),
    ("BAJAJFINSV", "Bajaj Finserv", "Financial Services"),
    ("BEL", "Bharat Electronics", "Industrials"),
    ("BHARTIARTL", "Bharti Airtel", "Telecom"),
    ("ETERNAL", "Eternal", "Consumer"),
    ("HCLTECH", "HCL Technologies", "Information Technology"),
    ("HDFCBANK", "HDFC Bank", "Financial Services"),
    ("HINDUNILVR", "Hindustan Unilever", "FMCG"),
    ("ICICIBANK", "ICICI Bank", "Financial Services"),
    ("INFY", "Infosys", "Information Technology"),
    ("INDIGO", "InterGlobe Aviation", "Services"),
    ("ITC", "ITC", "FMCG"),
    ("KOTAKBANK", "Kotak Mahindra Bank", "Financial Services"),
    ("LT", "Larsen & Toubro", "Industrials"),
    ("M&M", "Mahindra & Mahindra", "Automobile"),
    ("MARUTI", "Maruti Suzuki", "Automobile"),
    ("NTPC", "NTPC", "Energy & Utilities"),
    ("POWERGRID", "Power Grid", "Energy & Utilities"),
    ("RELIANCE", "Reliance Industries", "Energy & Utilities"),
    ("SBIN", "State Bank of India", "Financial Services"),
    ("SUNPHARMA", "Sun Pharma", "Healthcare"),
    ("TCS", "Tata Consultancy Services", "Information Technology"),
    ("TATASTEEL", "Tata Steel", "Metals & Commodities"),
    ("TECHM", "Tech Mahindra", "Information Technology"),
    ("TITAN", "Titan", "Consumer"),
    ("TRENT", "Trent", "Consumer"),
    ("ULTRACEMCO", "UltraTech Cement", "Metals & Commodities"),
]

# Used only if the official NSE Indices CSV is temporarily unavailable.
NIFTY_FALLBACK = [
    ("ADANIENT", "Adani Enterprises", "Services"),
    ("ADANIPORTS", "Adani Ports", "Services"),
    ("APOLLOHOSP", "Apollo Hospitals", "Healthcare"),
    ("ASIANPAINT", "Asian Paints", "Consumer"),
    ("AXISBANK", "Axis Bank", "Financial Services"),
    ("BAJAJ-AUTO", "Bajaj Auto", "Automobile"),
    ("BAJFINANCE", "Bajaj Finance", "Financial Services"),
    ("BAJAJFINSV", "Bajaj Finserv", "Financial Services"),
    ("BEL", "Bharat Electronics", "Industrials"),
    ("BHARTIARTL", "Bharti Airtel", "Telecom"),
    ("CIPLA", "Cipla", "Healthcare"),
    ("COALINDIA", "Coal India", "Energy & Utilities"),
    ("DRREDDY", "Dr Reddy's", "Healthcare"),
    ("EICHERMOT", "Eicher Motors", "Automobile"),
    ("ETERNAL", "Eternal", "Consumer"),
    ("GRASIM", "Grasim Industries", "Industrials"),
    ("HCLTECH", "HCL Technologies", "Information Technology"),
    ("HDFCBANK", "HDFC Bank", "Financial Services"),
    ("HDFCLIFE", "HDFC Life", "Financial Services"),
    ("HEROMOTOCO", "Hero MotoCorp", "Automobile"),
    ("HINDALCO", "Hindalco", "Metals & Commodities"),
    ("HINDUNILVR", "Hindustan Unilever", "FMCG"),
    ("ICICIBANK", "ICICI Bank", "Financial Services"),
    ("INDUSINDBK", "IndusInd Bank", "Financial Services"),
    ("INFY", "Infosys", "Information Technology"),
    ("ITC", "ITC", "FMCG"),
    ("JIOFIN", "Jio Financial Services", "Financial Services"),
    ("JSWSTEEL", "JSW Steel", "Metals & Commodities"),
    ("KOTAKBANK", "Kotak Mahindra Bank", "Financial Services"),
    ("LT", "Larsen & Toubro", "Industrials"),
    ("M&M", "Mahindra & Mahindra", "Automobile"),
    ("MARUTI", "Maruti Suzuki", "Automobile"),
    ("NESTLEIND", "Nestle India", "FMCG"),
    ("NTPC", "NTPC", "Energy & Utilities"),
    ("ONGC", "ONGC", "Energy & Utilities"),
    ("POWERGRID", "Power Grid", "Energy & Utilities"),
    ("RELIANCE", "Reliance Industries", "Energy & Utilities"),
    ("SBILIFE", "SBI Life", "Financial Services"),
    ("SBIN", "State Bank of India", "Financial Services"),
    ("SHRIRAMFIN", "Shriram Finance", "Financial Services"),
    ("SUNPHARMA", "Sun Pharma", "Healthcare"),
    ("TATACONSUM", "Tata Consumer", "FMCG"),
    ("TATAMOTORS", "Tata Motors", "Automobile"),
    ("TATASTEEL", "Tata Steel", "Metals & Commodities"),
    ("TCS", "Tata Consultancy Services", "Information Technology"),
    ("TECHM", "Tech Mahindra", "Information Technology"),
    ("TITAN", "Titan", "Consumer"),
    ("TRENT", "Trent", "Consumer"),
    ("ULTRACEMCO", "UltraTech Cement", "Metals & Commodities"),
    ("WIPRO", "Wipro", "Information Technology"),
]

BANKNIFTY_FALLBACK = [
    ("AUBANK", "AU Small Finance Bank", "Small Finance Banks"),
    ("AXISBANK", "Axis Bank", "Private Banks"),
    ("BANKBARODA", "Bank of Baroda", "PSU Banks"),
    ("CANBK", "Canara Bank", "PSU Banks"),
    ("FEDERALBNK", "Federal Bank", "Private Banks"),
    ("HDFCBANK", "HDFC Bank", "Private Banks"),
    ("ICICIBANK", "ICICI Bank", "Private Banks"),
    ("IDFCFIRSTB", "IDFC First Bank", "Private Banks"),
    ("INDUSINDBK", "IndusInd Bank", "Private Banks"),
    ("KOTAKBANK", "Kotak Mahindra Bank", "Private Banks"),
    ("PNB", "Punjab National Bank", "PSU Banks"),
    ("SBIN", "State Bank of India", "PSU Banks"),
]

BANK_TYPE = {
    "AUBANK": "Small Finance Banks",
    "BANKBARODA": "PSU Banks",
    "CANBK": "PSU Banks",
    "PNB": "PSU Banks",
    "SBIN": "PSU Banks",
}

_universe_cache = {}
_universe_lock = threading.Lock()
_rotation_cache = {}
_rotation_lock = threading.Lock()
_resolution_cache = {}
_resolution_lock = threading.Lock()


def _safe_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_sector(value):
    raw = str(value or "Other").strip()
    text = raw.lower()
    if "bank" in text or "financial" in text or "insurance" in text:
        return "Financial Services"
    if any(word in text for word in ("software", "computer", "information technology", "it services")):
        return "Information Technology"
    if any(word in text for word in ("automobile", "auto components", "two wheel", "passenger car")):
        return "Automobile"
    if any(word in text for word in ("pharma", "healthcare", "hospital")):
        return "Healthcare"
    if any(word in text for word in ("fmcg", "food", "beverage", "personal products", "tobacco")):
        return "FMCG"
    if any(word in text for word in ("oil", "gas", "petroleum", "power", "utility", "coal")):
        return "Energy & Utilities"
    if any(word in text for word in ("metal", "mining", "cement", "mineral")):
        return "Metals & Commodities"
    if any(word in text for word in ("construction", "capital goods", "industrial", "engineering")):
        return "Industrials"
    if any(word in text for word in ("telecom", "communication")):
        return "Telecom"
    if any(word in text for word in ("consumer", "retail", "paint", "jewellery", "textile")):
        return "Consumer"
    if any(word in text for word in ("transport", "logistics", "port", "airline", "services")):
        return "Services"
    return raw[:48] or "Other"


def _rows(items):
    return [
        {"symbol": symbol, "name": name, "sector": sector}
        for symbol, name, sector in items
    ]


def _fetch_nifty_csv(index_name):
    response = requests.get(
        NIFTY_CONSTITUENT_URLS[index_name],
        headers={"User-Agent": "OptionKingAI/1.0"},
        timeout=12,
    )
    response.raise_for_status()
    text = response.content.decode("utf-8-sig", errors="replace")
    result = []
    for row in csv.DictReader(io.StringIO(text)):
        symbol = str(row.get("Symbol") or "").strip().upper()
        if not symbol:
            continue
        name = str(row.get("Company Name") or symbol).strip()
        industry = str(row.get("Industry") or "Other").strip()
        sector = _normalize_sector(industry)
        if index_name == "BANKNIFTY":
            sector = BANK_TYPE.get(symbol, "Private Banks")
        result.append({"symbol": symbol, "name": name, "sector": sector})
    if not result:
        raise RuntimeError("Official index constituent file returned no rows")
    return result


def _get_universe(index_name):
    now = time.monotonic()
    with _universe_lock:
        cached = _universe_cache.get(index_name)
        if cached and now - cached["stored_at"] < UNIVERSE_CACHE_SECONDS:
            return cached["rows"], cached["source"]

    if index_name == "SENSEX":
        rows = _rows(SENSEX_UNIVERSE)
        source = "BSE_SENSEX_REFERENCE_2026_08"
    else:
        try:
            rows = _fetch_nifty_csv(index_name)
            source = "NSE_INDICES_OFFICIAL_CSV"
        except Exception:
            fallback = NIFTY_FALLBACK if index_name == "NIFTY" else BANKNIFTY_FALLBACK
            rows = _rows(fallback)
            source = "BUILT_IN_FALLBACK"

    with _universe_lock:
        _universe_cache[index_name] = {
            "stored_at": now,
            "rows": rows,
            "source": source,
        }
    return rows, source


def _cache_resolution(key, value):
    with _resolution_lock:
        _resolution_cache[key] = {"stored_at": time.monotonic(), "value": value}


def _get_resolution(key):
    with _resolution_lock:
        item = _resolution_cache.get(key)
        if not item:
            return None
        if time.monotonic() - item["stored_at"] >= RESOLUTION_CACHE_SECONDS:
            _resolution_cache.pop(key, None)
            return None
        return item["value"]


def _quote_row(symbol, name, sector, ltp, close, percent=None, status="connected"):
    ltp_value = _safe_float(ltp)
    close_value = _safe_float(close)
    pct = _safe_float(percent)
    if pct is None and ltp_value is not None and close_value not in (None, 0):
        pct = ((ltp_value - close_value) / close_value) * 100.0
    change = None
    if ltp_value is not None and close_value is not None:
        change = ltp_value - close_value
    return {
        "symbol": symbol,
        "name": name,
        "sector": sector,
        "ltp": round(ltp_value, 2) if ltp_value is not None else None,
        "previous_close": round(close_value, 2) if close_value is not None else None,
        "change": round(change, 2) if change is not None else None,
        "change_percent": round(pct, 2) if pct is not None else None,
        "status": status if pct is not None else "change_unavailable",
    }


def _fetch_zerodha_quotes(user_id, creds, universe):
    obj = _get_multi_session(user_id, "zerodha", creds)
    keys = [f"NSE:{row['symbol']}" for row in universe]
    payload = obj.kite.quote(keys)
    output = {}
    for row in universe:
        symbol = row["symbol"]
        entry = (payload or {}).get(f"NSE:{symbol}") or {}
        ohlc = entry.get("ohlc") or {}
        output[symbol] = _quote_row(
            symbol,
            row["name"],
            row["sector"],
            entry.get("last_price"),
            ohlc.get("close"),
        )
    return output


def _resolve_upstox_key(obj, symbol):
    cache_key = ("upstox", symbol)
    cached = _get_resolution(cache_key)
    if cached:
        return cached

    response = requests.get(
        f"{obj.BASE_URL}/instruments/search",
        params={
            "query": symbol,
            "exchanges": "NSE",
            "segments": "EQ",
            "page_number": 1,
            "records": 20,
        },
        headers=obj._h(),
        timeout=12,
    )
    payload = response.json()
    if response.status_code != 200:
        return None

    exact = None
    fallback = None
    for item in payload.get("data") or []:
        key = str(item.get("instrument_key") or "").strip()
        trading_symbol = str(item.get("trading_symbol") or "").strip().upper()
        if not key or not key.startswith("NSE_EQ|"):
            continue
        fallback = fallback or key
        if trading_symbol == symbol.upper():
            exact = key
            break
    result = exact or fallback
    if result:
        _cache_resolution(cache_key, result)
    return result


def _fetch_upstox_quotes(user_id, creds, universe):
    obj = _get_multi_session(user_id, "upstox", creds)
    key_by_symbol = {}
    for row in universe:
        key = _resolve_upstox_key(obj, row["symbol"])
        if key:
            key_by_symbol[row["symbol"]] = key

    if not key_by_symbol:
        raise RuntimeError("Upstox equity instruments could not be resolved")

    response = requests.get(
        f"{obj.BASE_URL}/market-quote/quotes",
        params={"instrument_key": ",".join(key_by_symbol.values())},
        headers=obj._h(),
        timeout=15,
    )
    payload = response.json()
    if response.status_code != 200 or payload.get("status") != "success":
        raise RuntimeError(str(payload.get("errors") or payload.get("message") or payload)[:220])

    data = payload.get("data") or {}
    by_symbol = {}
    by_token = {}
    by_key = {}
    for response_key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        response_key = str(response_key or "").strip()
        entry_symbol = str(
            entry.get("symbol")
            or entry.get("trading_symbol")
            or entry.get("tradingSymbol")
            or ""
        ).strip().upper()
        token = str(entry.get("instrument_token") or "").strip()
        if response_key:
            by_key[response_key] = entry
            by_key[response_key.replace(":", "|", 1)] = entry
        if entry_symbol:
            by_symbol[entry_symbol] = entry
        if token:
            by_token[token] = entry

    output = {}
    for row in universe:
        symbol = row["symbol"]
        key = key_by_symbol.get(symbol)
        entry = (
            by_symbol.get(symbol)
            or by_key.get(str(key or ""))
            or by_key.get(str(key or "").replace("|", ":", 1))
            or by_token.get(str(key or ""))
            or {}
        )
        ohlc = entry.get("ohlc") or {}
        ltp = entry.get("last_price")
        close = ohlc.get("close")
        net_change = _safe_float(entry.get("net_change"))
        if close is None and ltp is not None and net_change is not None:
            close = float(ltp) - net_change
        output[symbol] = _quote_row(
            symbol,
            row["name"],
            row["sector"],
            ltp,
            close,
        )
    return output


def _resolve_angel_token(obj, symbol):
    cache_key = ("angelone", symbol)
    cached = _get_resolution(cache_key)
    if cached:
        return cached

    payload = obj.searchScrip("NSE", symbol)
    rows = (payload.get("data") or []) if isinstance(payload, dict) else []
    exact = None
    fallback = None
    for item in rows:
        trading_symbol = str(item.get("tradingsymbol") or "").strip().upper()
        token = str(item.get("symboltoken") or "").strip()
        if not token:
            continue
        fallback = fallback or (trading_symbol, token)
        if trading_symbol in {symbol.upper(), f"{symbol.upper()}-EQ"}:
            exact = (trading_symbol, token)
            break
    result = exact or fallback
    if result:
        _cache_resolution(cache_key, result)
    return result


def _fetch_angel_quotes(user_id, creds, universe):
    obj = _get_ltp_session(user_id, creds)
    token_by_symbol = {}
    for row in universe:
        resolved = _resolve_angel_token(obj, row["symbol"])
        if not resolved:
            continue
        trading_symbol, token = resolved
        token_by_symbol[row["symbol"]] = (trading_symbol, token)

    if not token_by_symbol:
        raise RuntimeError("Angel One equity instruments could not be resolved")

    payload = obj.getMarketData(
        "FULL",
        {"NSE": [token for _, token in token_by_symbol.values()]},
    )
    if not isinstance(payload, dict) or not payload.get("status"):
        raise RuntimeError(
            str((payload or {}).get("message", "Angel full market data unavailable"))[:220]
            if isinstance(payload, dict)
            else "Angel full market data unavailable"
        )

    data = payload.get("data") or {}
    fetched = data.get("fetched") if isinstance(data, dict) else data
    fetched = fetched if isinstance(fetched, list) else []
    by_token = {}
    for item in fetched:
        if not isinstance(item, dict):
            continue
        token = str(item.get("symbolToken") or item.get("symboltoken") or "").strip()
        if token:
            by_token[token] = item

    output = {}
    for row in universe:
        symbol = row["symbol"]
        resolved = token_by_symbol.get(symbol)
        item = by_token.get(resolved[1]) if resolved else None
        item = item or {}
        output[symbol] = _quote_row(
            symbol,
            row["name"],
            row["sector"],
            item.get("ltp") or item.get("lastPrice") or item.get("last_price"),
            item.get("close") or item.get("closePrice") or item.get("previousClose"),
            item.get("percentChange") or item.get("percent_change"),
        )
    return output


def _fetch_quotes(user_id, broker_name, creds, universe):
    broker = str(broker_name or "").lower()
    if broker == "angelone":
        return _fetch_angel_quotes(user_id, creds, universe)
    if broker == "upstox":
        return _fetch_upstox_quotes(user_id, creds, universe)
    if broker == "zerodha":
        return _fetch_zerodha_quotes(user_id, creds, universe)
    raise RuntimeError(f"Sector rotation is unavailable for broker: {broker_name}")


def _rotation_label(average_change, breadth_percent):
    if average_change >= 0.25 and breadth_percent >= 65:
        return "BROAD_POSITIVE"
    if average_change <= -0.25 and breadth_percent <= 35:
        return "BROAD_NEGATIVE"
    if average_change > 0.05:
        return "POSITIVE_BIAS"
    if average_change < -0.05:
        return "NEGATIVE_BIAS"
    return "MIXED"


def _build_payload(index_name, broker_name, universe_source, universe, quotes):
    stocks = []
    for row in universe:
        item = quotes.get(row["symbol"])
        if item:
            stocks.append(item)
        else:
            stocks.append(
                _quote_row(
                    row["symbol"],
                    row["name"],
                    row["sector"],
                    None,
                    None,
                    status="quote_unavailable",
                )
            )

    covered = [row for row in stocks if row.get("change_percent") is not None]
    covered.sort(key=lambda row: row["change_percent"], reverse=True)

    advancers = sum(1 for row in covered if row["change_percent"] > 0.02)
    decliners = sum(1 for row in covered if row["change_percent"] < -0.02)
    unchanged = len(covered) - advancers - decliners
    average_change = (
        sum(row["change_percent"] for row in covered) / len(covered)
        if covered
        else 0.0
    )
    breadth_percent = (advancers / len(covered) * 100.0) if covered else 0.0

    grouped = defaultdict(list)
    for row in covered:
        grouped[row["sector"]].append(row)

    sectors = []
    for sector, rows in grouped.items():
        sector_advancers = sum(1 for row in rows if row["change_percent"] > 0.02)
        sector_decliners = sum(1 for row in rows if row["change_percent"] < -0.02)
        sector_average = sum(row["change_percent"] for row in rows) / len(rows)
        sectors.append(
            {
                "sector": sector,
                "average_change_percent": round(sector_average, 2),
                "advancers": sector_advancers,
                "decliners": sector_decliners,
                "unchanged": len(rows) - sector_advancers - sector_decliners,
                "breadth_percent": round(sector_advancers / len(rows) * 100.0, 1),
                "stocks": sorted(rows, key=lambda row: row["change_percent"], reverse=True),
            }
        )
    sectors.sort(key=lambda row: row["average_change_percent"], reverse=True)

    now_ist, market_open = _market_session_state()
    return {
        "success": bool(covered),
        "index": index_name,
        "source": broker_name,
        "constituent_source": universe_source,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "market_open": market_open,
        "market_time_ist": now_ist.isoformat(),
        "summary": {
            "rotation": _rotation_label(average_change, breadth_percent),
            "average_change_percent": round(average_change, 2),
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "breadth_percent": round(breadth_percent, 1),
            "coverage": len(covered),
            "constituents": len(universe),
            "strongest_sector": sectors[0]["sector"] if sectors else None,
            "weakest_sector": sectors[-1]["sector"] if sectors else None,
        },
        "sectors": sectors,
        "top_gainers": covered[:5],
        "top_losers": list(reversed(covered[-5:])),
        "stocks": covered,
        "display_only": True,
        "trade_blocking": False,
        "order_execution": False,
        "message": "Live sector rotation available" if covered else "Live stock change data unavailable",
    }


@router.get("/market/sector-rotation")
def sector_rotation(
    index: str = Query("NIFTY"),
    authorization: str = Header(None),
):
    """Display-only index breadth and sector rotation dashboard.

    It never changes strategy scores, entries, exits, risk limits or broker orders.
    """
    user = get_current_user(authorization)
    index_name = str(index or "NIFTY").upper().replace(" ", "").strip()
    aliases = {
        "NIFTY50": "NIFTY",
        "NIFTYBANK": "BANKNIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "BSESENSEX": "SENSEX",
    }
    index_name = aliases.get(index_name, index_name)
    if index_name not in SUPPORTED_INDICES:
        return {
            "success": False,
            "index": index_name,
            "message": "Supported indices: NIFTY, BANKNIFTY, SENSEX",
            "display_only": True,
            "trade_blocking": False,
        }

    cache_key = (int(user["id"]), index_name)
    now = time.monotonic()
    with _rotation_lock:
        cached = _rotation_cache.get(cache_key)
        if cached and now - cached["stored_at"] < ROTATION_CACHE_SECONDS:
            return {**cached["payload"], "cache_hit": True}

    broker_name, creds = _get_active_broker(user["id"])
    if not creds:
        return {
            "success": False,
            "index": index_name,
            "message": "Active broker not connected",
            "display_only": True,
            "trade_blocking": False,
            "order_execution": False,
        }

    universe, universe_source = _get_universe(index_name)
    try:
        quotes = _fetch_quotes(user["id"], broker_name, creds, universe)
        payload = _build_payload(
            index_name,
            broker_name,
            universe_source,
            universe,
            quotes,
        )
    except Exception as exc:
        payload = {
            "success": False,
            "index": index_name,
            "source": broker_name,
            "constituent_source": universe_source,
            "message": str(exc)[:220],
            "display_only": True,
            "trade_blocking": False,
            "order_execution": False,
        }

    payload["cache_hit"] = False
    with _rotation_lock:
        _rotation_cache[cache_key] = {"stored_at": now, "payload": payload}
    return payload

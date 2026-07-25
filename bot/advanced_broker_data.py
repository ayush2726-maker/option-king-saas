"""Broker-neutral option and global-market feature collection."""
from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from auth.utils import decrypt_credential
from bot.brokers.factory import create_broker
from bot.option_chain import (
    LOT_SIZE_FALLBACK,
    STRIKE_STEP,
    _load_cache,
    expected_expiry_for_trade_date,
    get_atm_strike,
)
from database import get_db

INDEX_KEYS_UPSTOX = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}
EXCHANGE_FOR = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "SENSEX": "BFO"}
CAPABILITIES = {
    "angelone": {
        "option_chain": "ANGEL_SCRIP_MASTER_NEAR_ATM",
        "oi_depth": "ANGEL_FULL_QUOTE_D5",
        "greeks": "ANGEL_NATIVE_WITH_BS_FALLBACK",
    },
    "upstox": {
        "option_chain": "UPSTOX_NATIVE_CONTRACTS",
        "oi_depth": "UPSTOX_FULL_QUOTE_D5_AND_ANALYTICS",
        "greeks": "UPSTOX_NATIVE",
    },
    "zerodha": {
        "option_chain": "KITE_INSTRUMENT_MASTER_NEAR_ATM",
        "oi_depth": "KITE_FULL_QUOTE_D5",
        "greeks": "BLACK_SCHOLES_ESTIMATE",
    },
}

_LOCK = threading.RLock()
_SESSIONS: Dict[int, Dict[str, Any]] = {}
_CONTRACT_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_ANALYTICS_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_GLOBAL_CACHE: Dict[str, Any] = {}
_GLOBAL_AT = 0.0


def num(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
        return value if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def direction(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"CE", "CALL", "BUY", "BULLISH", "UP", "UPTREND"}:
        return "CE"
    if text in {"PE", "PUT", "SELL", "BEARISH", "DOWN", "DOWNTREND"}:
        return "PE"
    if text in {"NO_TRADE", "NO TRADE", "WAIT", "WAITING", "HOLD", "SKIP"}:
        return "NO_TRADE"
    return "NEUTRAL"


def parse_day(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%b-%Y", "%d%b%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    return None


def _credential(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        return decrypt_credential(text)
    except Exception:
        return text


def _broker_row(user_id: int):
    conn = get_db()
    try:
        return conn.execute(
            """SELECT * FROM broker_credentials
               WHERE user_id=? AND is_active=1
               ORDER BY COALESCE(last_connected,created_at) DESC LIMIT 1""",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()


def active_broker_name(user_id: int) -> Optional[str]:
    row = _broker_row(user_id)
    return str(row["broker_name"]).lower() if row else None


def broker_session(user_id: int, force: bool = False) -> Dict[str, Any]:
    with _LOCK:
        cached = _SESSIONS.get(user_id)
        if cached and not force and time.monotonic() - cached["at"] < 1800:
            return cached
    row = _broker_row(user_id)
    if not row:
        raise RuntimeError("BROKER_NOT_CONNECTED")
    broker = str(row["broker_name"] or "angelone").lower()
    creds = {
        "client_id": str(row["client_id"] or ""),
        "api_key": _credential(row["api_key"]),
        "password": _credential(row["api_secret"]),
        "totp_secret": _credential(row["totp_secret"]) if row["totp_secret"] else None,
    }
    if broker == "angelone":
        from bot.angel_fetcher import angel_login

        client = angel_login(creds)
    else:
        client = create_broker(
            broker, creds["client_id"], creds["api_key"],
            creds["password"], creds.get("totp_secret"),
        )
        result = client.login()
        if not result.get("success"):
            raise RuntimeError(result.get("message") or "BROKER_LOGIN_FAILED")
    session = {"broker": broker, "client": client, "at": time.monotonic()}
    with _LOCK:
        _SESSIONS[user_id] = session
    return session


def drop_session(user_id: int) -> None:
    with _LOCK:
        _SESSIONS.pop(user_id, None)


def _contract(side, symbol, token, exchange, expiry, strike, lot_size):
    return {
        "side": direction(side),
        "symbol": str(symbol or ""),
        "token": str(token or ""),
        "exchange": str(exchange or ""),
        "expiry": str(expiry or ""),
        "strike": round(num(strike), 2),
        "lot_size": max(1, integer(lot_size, 1)),
    }


def _angel_contracts(underlying: str, spot: float, width: int) -> List[Dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    expected = expected_expiry_for_trade_date(underlying, today)
    atm = get_atm_strike(underlying, spot)
    step = STRIKE_STEP.get(underlying, 50)
    allowed = {atm + step * offset for offset in range(-width, width + 1)}
    found = []
    for row in _load_cache():
        if str(row.get("name") or "").upper() != underlying:
            continue
        symbol = str(row.get("symbol") or "").upper()
        side = "CE" if symbol.endswith("CE") else "PE" if symbol.endswith("PE") else ""
        expiry = parse_day(row.get("expiry"))
        strike = num(row.get("strike")) / 100.0
        if (
            side and expiry and expiry >= today
            and abs((expiry - expected).days) <= 3
            and strike in allowed
        ):
            found.append((expiry, row, side, strike))
    if not found:
        return []
    nearest = min(item[0] for item in found)
    return [
        _contract(
            side, row.get("symbol"), row.get("token"),
            row.get("exch_seg") or EXCHANGE_FOR.get(underlying, "NFO"),
            nearest.isoformat(), strike,
            row.get("lotsize") or LOT_SIZE_FALLBACK.get(underlying, 1),
        )
        for expiry, row, side, strike in found if expiry == nearest
    ]


def _upstox_contracts(client, underlying: str, spot: float, width: int):
    import requests

    key = INDEX_KEYS_UPSTOX.get(underlying)
    response = requests.get(
        "https://api.upstox.com/v2/option/contract",
        params={"instrument_key": key},
        headers=client._h(), timeout=12,
    )
    payload = response.json()
    if response.status_code != 200 or payload.get("status") != "success":
        raise RuntimeError(str(payload.get("errors") or payload)[:200])
    today = datetime.now(timezone.utc).date()
    atm = get_atm_strike(underlying, spot)
    step = STRIKE_STEP.get(underlying, 50)
    allowed = {atm + step * offset for offset in range(-width, width + 1)}
    found = []
    for row in payload.get("data") or []:
        side = str(row.get("instrument_type") or row.get("option_type") or "").upper()
        expiry = parse_day(row.get("expiry"))
        strike = num(row.get("strike_price") or row.get("strike"))
        if side in {"CE", "PE"} and expiry and expiry >= today and strike in allowed:
            found.append((expiry, row, side, strike))
    if not found:
        return []
    nearest = min(item[0] for item in found)
    return [
        _contract(
            side, row.get("trading_symbol") or row.get("tradingsymbol"),
            row.get("instrument_key"),
            row.get("segment") or ("BSE_FO" if underlying == "SENSEX" else "NSE_FO"),
            nearest.isoformat(), strike,
            row.get("lot_size") or LOT_SIZE_FALLBACK.get(underlying, 1),
        )
        for expiry, row, side, strike in found if expiry == nearest
    ]


def _zerodha_contracts(client, underlying: str, spot: float, width: int):
    exchange = "BFO" if underlying == "SENSEX" else "NFO"
    today = datetime.now(timezone.utc).date()
    atm = get_atm_strike(underlying, spot)
    step = STRIKE_STEP.get(underlying, 50)
    allowed = {atm + step * offset for offset in range(-width, width + 1)}
    found = []
    for row in client.kite.instruments(exchange):
        if str(row.get("name") or "").upper() != underlying:
            continue
        side = str(row.get("instrument_type") or "").upper()
        expiry = row.get("expiry")
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        elif not isinstance(expiry, date):
            expiry = parse_day(expiry)
        strike = num(row.get("strike"))
        if side in {"CE", "PE"} and expiry and expiry >= today and strike in allowed:
            found.append((expiry, row, side, strike))
    if not found:
        return []
    nearest = min(item[0] for item in found)
    return [
        _contract(
            side, row.get("tradingsymbol"), row.get("instrument_token"),
            exchange, nearest.isoformat(), strike,
            row.get("lot_size") or LOT_SIZE_FALLBACK.get(underlying, 1),
        )
        for expiry, row, side, strike in found if expiry == nearest
    ]


def contracts_for(session: Mapping[str, Any], underlying: str, spot: float, width: int = 3):
    broker = str(session["broker"])
    key = (broker, underlying)
    cached = _CONTRACT_CACHE.get(key)
    if cached and time.monotonic() - cached["at"] < 900:
        return [dict(row) for row in cached["rows"]]
    client = session["client"]
    if broker == "angelone":
        rows = _angel_contracts(underlying, spot, width)
    elif broker == "upstox":
        rows = _upstox_contracts(client, underlying, spot, width)
    elif broker == "zerodha":
        rows = _zerodha_contracts(client, underlying, spot, width)
    else:
        rows = []
    if rows:
        _CONTRACT_CACHE[key] = {"at": time.monotonic(), "rows": rows}
    return [dict(row) for row in rows]


def _cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def _pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2 * math.pi)


def _years(expiry: Any) -> float:
    day = parse_day(expiry)
    if not day:
        return 1 / 365
    ist = timezone(timedelta(hours=5, minutes=30))
    end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=ist)
    seconds = max(900, (end - datetime.now(timezone.utc).astimezone(ist)).total_seconds())
    return seconds / (365 * 24 * 3600)


def _bs_price(side, spot, strike, years, rate, sigma):
    if min(spot, strike, years, sigma) <= 0:
        return 0
    root = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2) * years) / (sigma * root)
    d2 = d1 - sigma * root
    if side == "CE":
        return spot * _cdf(d1) - strike * math.exp(-rate * years) * _cdf(d2)
    return strike * math.exp(-rate * years) * _cdf(-d2) - spot * _cdf(-d1)


def estimated_greeks(side, premium, spot, strike, expiry):
    years = _years(expiry)
    rate = num(os.getenv("OKAI_RISK_FREE_RATE"), 0.065)
    low, high = 0.01, 5.0
    for _ in range(55):
        sigma = (low + high) / 2
        if _bs_price(side, spot, strike, years, rate, sigma) > premium:
            high = sigma
        else:
            low = sigma
    sigma = (low + high) / 2
    root = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + sigma * sigma / 2) * years) / (sigma * root)
    d2 = d1 - sigma * root
    delta = _cdf(d1) if side == "CE" else _cdf(d1) - 1
    gamma = _pdf(d1) / (spot * sigma * root)
    vega = spot * _pdf(d1) * root / 100
    theta = (
        -(spot * _pdf(d1) * sigma) / (2 * root)
        + (
            -rate * strike * math.exp(-rate * years) * _cdf(d2)
            if side == "CE"
            else rate * strike * math.exp(-rate * years) * _cdf(-d2)
        )
    ) / 365
    return {
        "iv": round(sigma * 100, 4), "delta": round(delta, 6),
        "gamma": round(gamma, 8), "theta": round(theta, 4),
        "vega": round(vega, 4),
    }


def _first_depth(levels):
    for row in levels or []:
        if isinstance(row, Mapping) and num(row.get("price")) > 0:
            return num(row.get("price")), num(row.get("quantity"))
    return 0.0, 0.0


def normalize_quote(contract, raw, spot):
    depth = raw.get("depth") if isinstance(raw.get("depth"), Mapping) else {}
    bid, bid_qty = _first_depth(depth.get("buy"))
    ask, ask_qty = _first_depth(depth.get("sell"))
    ltp = num(raw.get("last_price") or raw.get("ltp") or raw.get("lastPrice"))
    bid = num(raw.get("bid") or raw.get("best_bid"), bid)
    ask = num(raw.get("ask") or raw.get("best_ask"), ask)
    if bid <= 0:
        bid = ltp * 0.9985
    if ask <= 0:
        ask = ltp * 1.0015
    row = {
        **dict(contract), "ltp": ltp, "bid": bid, "ask": ask,
        "bid_qty": bid_qty, "ask_qty": ask_qty,
        "oi": num(raw.get("oi") or raw.get("opnInterest")),
        "volume": num(raw.get("volume") or raw.get("tradeVolume")),
        "iv": num(raw.get("iv") or raw.get("impliedVolatility")),
        "delta": num(raw.get("delta")), "gamma": num(raw.get("gamma")),
        "theta": num(raw.get("theta")), "vega": num(raw.get("vega")),
    }
    if row["ltp"] > 0 and (row["iv"] <= 0 or row["delta"] == 0):
        row.update(estimated_greeks(
            row["side"], row["ltp"], spot, row["strike"], row["expiry"]
        ))
        row["greeks_source"] = "BLACK_SCHOLES_ESTIMATE"
    else:
        row["greeks_source"] = str(raw.get("greeks_source") or "BROKER_NATIVE")
    row["spread_percent"] = (
        (row["ask"] - row["bid"]) / max(row["ltp"], 0.05) * 100
    )
    total = row["bid_qty"] + row["ask_qty"]
    row["depth_imbalance"] = (
        (row["bid_qty"] - row["ask_qty"]) / total if total else 0
    )
    return row


def _angel_quotes(client, contracts, spot):
    raw_by_token = {}
    for exchange in {row["exchange"] for row in contracts}:
        tokens = [row["token"] for row in contracts if row["exchange"] == exchange]
        try:
            payload = client.getMarketData("FULL", {exchange: tokens})
            for row in (payload.get("data") or {}).get("fetched") or []:
                raw_by_token[str(row.get("symbolToken") or "")] = dict(row)
        except Exception:
            pass
    greek_maps = {}
    for expiry in {row["expiry"] for row in contracts}:
        day = parse_day(expiry)
        if not day:
            continue
        underlying = "BANKNIFTY" if any("BANKNIFTY" in x["symbol"] for x in contracts) else (
            "SENSEX" if any("SENSEX" in x["symbol"] for x in contracts) else "NIFTY"
        )
        try:
            params = {"name": underlying, "expirydate": day.strftime("%d%b%Y").upper()}
            payload = client.optionGreek(params) if hasattr(client, "optionGreek") else {}
            greek_maps[expiry] = {
                (round(num(x.get("strikePrice")), 2), direction(x.get("optionType"))): x
                for x in payload.get("data") or []
            }
        except Exception:
            greek_maps[expiry] = {}
    result = []
    for contract in contracts:
        raw = dict(raw_by_token.get(contract["token"]) or {})
        if not raw:
            try:
                raw = dict((client.ltpData(
                    contract["exchange"], contract["symbol"], contract["token"]
                ) or {}).get("data") or {})
            except Exception:
                pass
        greek = greek_maps.get(contract["expiry"], {}).get(
            (round(contract["strike"], 2), contract["side"]), {}
        )
        raw.update(greek)
        raw["greeks_source"] = "ANGEL_OPTION_GREEKS" if greek else "BLACK_SCHOLES_ESTIMATE"
        quote = normalize_quote(contract, raw, spot)
        if quote["ltp"] > 0:
            result.append(quote)
    return result


def _upstox_quotes(client, contracts, spot):
    import requests

    keys = [row["token"] for row in contracts]
    quotes = requests.get(
        "https://api.upstox.com/v2/market-quote/quotes",
        params={"instrument_key": ",".join(keys)}, headers=client._h(), timeout=12,
    ).json().get("data") or {}
    greeks = requests.get(
        "https://api.upstox.com/v3/market-quote/option-greek",
        params={"instrument_key": ",".join(keys[:50])}, headers=client._h(), timeout=12,
    ).json().get("data") or {}
    result = []
    for contract in contracts:
        variants = {
            contract["token"], contract["token"].replace("|", ":"),
            contract["symbol"], f"{contract['exchange']}:{contract['symbol']}",
        }
        raw = {}
        for key in variants:
            raw.update(dict(quotes.get(key) or {}))
            raw.update(dict(greeks.get(key) or {}))
        raw["greeks_source"] = "UPSTOX_OPTION_GREEKS"
        quote = normalize_quote(contract, raw, spot)
        if quote["ltp"] > 0:
            result.append(quote)
    return result


def _zerodha_quotes(client, contracts, spot):
    keys = [f"{row['exchange']}:{row['symbol']}" for row in contracts]
    payload = client.kite.quote(keys)
    result = []
    for contract, key in zip(contracts, keys):
        raw = dict(payload.get(key) or {})
        raw["greeks_source"] = "BLACK_SCHOLES_ESTIMATE"
        quote = normalize_quote(contract, raw, spot)
        if quote["ltp"] > 0:
            result.append(quote)
    return result


def quote_contracts(session, contracts, spot):
    broker, client = session["broker"], session["client"]
    if broker == "angelone":
        return _angel_quotes(client, contracts, spot)
    if broker == "upstox":
        return _upstox_quotes(client, contracts, spot)
    if broker == "zerodha":
        return _zerodha_quotes(client, contracts, spot)
    return []


def _upstox_analytics(client, underlying, expiry):
    import requests

    cache_key = (underlying, expiry)
    cached = _ANALYTICS_CACHE.get(cache_key)
    if cached and time.monotonic() - cached["at"] < 300:
        return dict(cached["data"])
    key = INDEX_KEYS_UPSTOX.get(underlying)
    today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date().isoformat()
    result = {}
    specs = (
        ("oi", "oi", {}),
        ("change_oi", "change-oi", {"interval": 1}),
        ("pcr", "pcr", {"bucket_interval": 60}),
        ("max_pain", "max-pain", {}),
    )
    for name, endpoint, extra in specs:
        try:
            params = {"instrument_key": key, "expiry": expiry, "date": today, **extra}
            response = requests.get(
                f"https://api.upstox.com/v2/market/{endpoint}",
                params=params, headers=client._h(), timeout=6,
            )
            payload = response.json()
            if response.status_code == 200 and payload.get("status") == "success":
                result[name] = payload.get("data")
        except Exception:
            pass
    _ANALYTICS_CACHE[cache_key] = {"at": time.monotonic(), "data": result}
    return result


def _max_pain(quotes):
    strikes = sorted({num(x.get("strike")) for x in quotes if num(x.get("strike")) > 0})
    if not strikes:
        return 0
    def cost(settlement):
        total = 0
        for row in quotes:
            strike, oi = num(row.get("strike")), max(0, num(row.get("oi")))
            total += (
                max(0, settlement - strike) * oi
                if row.get("side") == "CE"
                else max(0, strike - settlement) * oi
            )
        return total
    return min(strikes, key=cost)


def option_snapshot(user_id: int, market: Mapping[str, Any], previous=None):
    session = broker_session(user_id)
    broker = session["broker"]
    underlying = str(market.get("symbol") or "NIFTY").upper()
    spot = num(market.get("price"))
    contracts = contracts_for(session, underlying, spot)
    quotes = quote_contracts(session, contracts, spot)
    calls = [x for x in quotes if x["side"] == "CE"]
    puts = [x for x in quotes if x["side"] == "PE"]
    if not calls or not puts:
        raise RuntimeError("OPTION_QUOTES_INSUFFICIENT")
    expiry_day = parse_day(quotes[0]["expiry"]) == (
        datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    ).date()
    def score(row):
        delta_target = 0.38 if expiry_day else 0.50
        delta = max(0, 30 - abs(abs(num(row["delta"])) - delta_target) * 80)
        liquidity = min(25, math.log10(num(row["oi"]) + 10) * 4 + math.log10(num(row["volume"]) + 10) * 3)
        spread = max(-25, 20 - num(row["spread_percent"]) * 5)
        theta = min(15, abs(num(row["theta"])) / max(num(row["ltp"]), .05) * 20)
        return delta + liquidity + spread - theta
    for row in quotes:
        row["selection_score"] = round(score(row), 3)
    best_ce, best_pe = max(calls, key=score), max(puts, key=score)
    call_oi = sum(max(0, num(x["oi"])) for x in calls)
    put_oi = sum(max(0, num(x["oi"])) for x in puts)
    pcr = put_oi / max(call_oi, 1)
    pain = _max_pain(quotes)
    native = _upstox_analytics(session["client"], underlying, quotes[0]["expiry"]) if broker == "upstox" else {}
    if isinstance(native.get("pcr"), Mapping):
        pcr = num(native["pcr"].get("pcr") or native["pcr"].get("put_call_ratio"), pcr)
    if isinstance(native.get("max_pain"), Mapping):
        pain = num(native["max_pain"].get("max_pain") or native["max_pain"].get("strike_price"), pain)
    previous = previous or {}
    call_change = call_oi - num(previous.get("call_oi"))
    put_change = put_oi - num(previous.get("put_oi"))
    ce_iv = statistics.mean([num(x["iv"]) for x in calls if num(x["iv"]) > 0] or [0])
    pe_iv = statistics.mean([num(x["iv"]) for x in puts if num(x["iv"]) > 0] or [0])
    ce_score, pe_score = score(best_ce), score(best_pe)
    if pcr >= 1.15:
        ce_score += min(18, (pcr - 1) * 25)
    elif pcr <= .85:
        pe_score += min(18, (1 - pcr) * 25)
    if pain > spot:
        ce_score += min(10, (pain - spot) / STRIKE_STEP.get(underlying, 50) * 3)
    elif pain < spot:
        pe_score += min(10, (spot - pain) / STRIKE_STEP.get(underlying, 50) * 3)
    if put_change > call_change:
        ce_score += 6
    elif call_change > put_change:
        pe_score += 6
    bias = "CE" if ce_score - pe_score >= 5 else "PE" if pe_score - ce_score >= 5 else "NEUTRAL"
    avg_spread = statistics.mean([num(best_ce["spread_percent"]), num(best_pe["spread_percent"])])
    quality = int(clamp(100 - avg_spread * 8 + min(15, math.log10(call_oi + put_oi + 10) * 2), 0, 100))
    return {
        "success": True, "broker": broker, "capabilities": CAPABILITIES.get(broker, {}),
        "underlying": underlying, "spot_price": spot, "expiry": quotes[0]["expiry"],
        "atm_strike": get_atm_strike(underlying, spot), "contract_count": len(quotes),
        "call_oi": call_oi, "put_oi": put_oi, "pcr": round(pcr, 4),
        "call_change_oi": call_change, "put_change_oi": put_change,
        "max_pain": pain, "max_pain_distance_percent": (pain - spot) / spot * 100 if pain else 0,
        "ce_iv": ce_iv, "pe_iv": pe_iv, "iv_skew": pe_iv - ce_iv,
        "depth_imbalance": num(best_ce["depth_imbalance"]) - num(best_pe["depth_imbalance"]),
        "average_spread_percent": avg_spread, "option_bias": bias,
        "option_strength": int(clamp(abs(ce_score - pe_score), 0, 100)),
        "data_quality_score": quality, "best_ce": best_ce, "best_pe": best_pe,
        "near_atm_chain": quotes, "native_analytics": native,
    }


def _yahoo(symbol):
    encoded = urllib.parse.quote(symbol, safe="")
    request = urllib.request.Request(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=2d&interval=5m",
        headers={"User-Agent": "OptionKingAI-GlobalShadow/1.0"},
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    result = ((payload.get("chart") or {}).get("result") or [None])[0] or {}
    meta = result.get("meta") or {}
    price = num(meta.get("regularMarketPrice"))
    previous = num(meta.get("chartPreviousClose") or meta.get("previousClose"))
    return {"price": price, "previous_close": previous, "change_percent": (
        (price - previous) / previous * 100 if price and previous else 0
    )}


def global_snapshot(force: bool = False):
    global _GLOBAL_CACHE, _GLOBAL_AT
    if _GLOBAL_CACHE and not force and time.monotonic() - _GLOBAL_AT < 120:
        return dict(_GLOBAL_CACHE)
    symbols = {"sp500": "^GSPC", "nasdaq": "^IXIC", "crude": "CL=F", "usd_inr": "INR=X", "india_vix": "^INDIAVIX"}
    gift = str(os.getenv("OKAI_GIFT_NIFTY_YAHOO_SYMBOL", "")).strip()
    if gift:
        symbols["gift_nifty"] = gift
    data, errors = {}, []
    for name, symbol in symbols.items():
        try:
            data[name] = {"symbol": symbol, **_yahoo(symbol)}
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}:{str(exc)[:80]}")
    bullish = sum(max(0, num((data.get(x) or {}).get("change_percent"))) for x in ("sp500", "nasdaq", "gift_nifty"))
    bearish = sum(max(0, -num((data.get(x) or {}).get("change_percent"))) for x in ("sp500", "nasdaq", "gift_nifty"))
    crude = num((data.get("crude") or {}).get("change_percent"))
    usd = num((data.get("usd_inr") or {}).get("change_percent"))
    vix = num((data.get("india_vix") or {}).get("change_percent"))
    bearish += max(0, crude) * .8 + max(0, usd) * 1.2 + max(0, vix) * 1.3
    bullish += max(0, -crude) * .3 + max(0, -usd) * .5 + max(0, -vix) * .8
    bias = "CE" if bullish - bearish >= .4 else "PE" if bearish - bullish >= .4 else "NEUTRAL"
    result = {
        "success": bool(data), "fresh": bool(data),
        "source": "BROKER_INDEPENDENT_PUBLIC_GLOBAL_SHADOW",
        "global_bias": bias, "global_strength": int(clamp(abs(bullish - bearish) * 12, 0, 100)),
        "global_risk_score": int(clamp(abs(crude) * 12 + abs(vix) * 15 + abs(usd) * 20, 0, 100)),
        "instruments": data, "errors": errors,
        "trade_blocking": False, "order_execution": False,
    }
    _GLOBAL_CACHE, _GLOBAL_AT = result, time.monotonic()
    return dict(result)


def global_health():
    return dict(_GLOBAL_CACHE) if _GLOBAL_CACHE else {
        "fresh": False, "source": "NOT_FETCHED_YET"
    }

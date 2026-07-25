"""Broker-neutral option-chain, Greeks, OI and liquidity intelligence.

The core AI must work for Angel One, Upstox and Zerodha. Native broker fields
are preferred; missing fields are derived or explicitly marked unavailable.
This module is monitoring-only and never places, modifies or closes orders.
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import requests

from bot.market_routes import (
    UPSTOX_INDEX_KEYS,
    _get_active_broker,
    _get_ltp_session,
    _get_multi_session,
)
from bot.option_chain import (
    EXCHANGE_FOR,
    LOT_SIZE_FALLBACK,
    STRIKE_STEP,
    _load_cache,
    _parse_expiry,
    _strike_of,
    expected_expiry_for_trade_date,
    get_atm_strike,
)


VERSION = "OKAI-BROKER-NEUTRAL-OPTION-INTELLIGENCE-V1"
DEFAULT_WINGS = 5

UPSTOX_UNDERLYING_KEYS = dict(UPSTOX_INDEX_KEYS)
ZERODHA_EXCHANGE = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "SENSEX": "BFO"}

BROKER_CAPABILITIES = {
    "angelone": {
        "core_ai": True,
        "option_chain": "ASSEMBLED_FROM_SCRIP_MASTER",
        "live_option_premium": True,
        "open_interest": True,
        "change_in_oi": "DERIVED_FROM_PREVIOUS_SNAPSHOT",
        "market_depth_levels": 5,
        "greeks": "NATIVE_LIVE_WITH_DERIVED_FALLBACK",
        "implied_volatility": "NATIVE_LIVE_WITH_DERIVED_FALLBACK",
        "pcr": "DERIVED_FROM_CHAIN",
        "max_pain": "DERIVED_FROM_CHAIN",
        "global_market": "NEWS_AND_CONFIGURED_EXTERNAL_KEYS",
    },
    "upstox": {
        "core_ai": True,
        "option_chain": "NATIVE",
        "live_option_premium": True,
        "open_interest": True,
        "change_in_oi": True,
        "market_depth_levels": 5,
        "greeks": "NATIVE",
        "implied_volatility": "NATIVE",
        "pcr": "NATIVE_AND_DERIVED",
        "max_pain": "DERIVED_OR_NATIVE_ANALYTICS",
        "global_market": "NATIVE_WHEN_KEYS_CONFIGURED",
    },
    "zerodha": {
        "core_ai": True,
        "option_chain": "ASSEMBLED_FROM_INSTRUMENTS",
        "live_option_premium": True,
        "open_interest": True,
        "change_in_oi": "DERIVED_FROM_PREVIOUS_SNAPSHOT",
        "market_depth_levels": 5,
        "greeks": "BLACK_SCHOLES_DERIVED",
        "implied_volatility": "DERIVED_FROM_LIVE_PREMIUM",
        "pcr": "DERIVED_FROM_CHAIN",
        "max_pain": "DERIVED_FROM_CHAIN",
        "global_market": "NEWS_AND_CONFIGURED_EXTERNAL_KEYS",
    },
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (
        (value or _utc_now())
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _expiry_datetime(expiry: str) -> Optional[datetime]:
    try:
        day = datetime.fromisoformat(str(expiry)[:10]).date()
        return datetime(day.year, day.month, day.day, 10, 0, tzinfo=timezone.utc)
    except Exception:
        return None


def _years_to_expiry(expiry: str) -> float:
    expiry_dt = _expiry_datetime(expiry)
    if expiry_dt is None:
        return 1.0 / 365.0
    seconds = max(60.0, (expiry_dt - _utc_now()).total_seconds())
    return seconds / (365.0 * 24.0 * 3600.0)


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _norm_pdf(value: float) -> float:
    return math.exp(-0.5 * value * value) / math.sqrt(2.0 * math.pi)


def _bs_price(spot, strike, years, rate, volatility, side):
    if spot <= 0 or strike <= 0 or years <= 0 or volatility <= 0:
        return max(0.0, spot - strike) if side == "CE" else max(0.0, strike - spot)
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility * volatility) * years) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    discount = math.exp(-rate * years)
    if side == "CE":
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _implied_volatility(premium, spot, strike, expiry, side, rate=0.065):
    premium = _f(premium)
    if premium <= 0 or spot <= 0 or strike <= 0:
        return None
    years = _years_to_expiry(expiry)
    intrinsic = max(0.0, spot - strike) if side == "CE" else max(0.0, strike - spot)
    if premium < intrinsic:
        return None
    low, high = 0.01, 5.0
    for _ in range(70):
        mid = (low + high) / 2.0
        if _bs_price(spot, strike, years, rate, mid, side) > premium:
            high = mid
        else:
            low = mid
    return round((low + high) / 2.0 * 100.0, 4)


def _derived_greeks(premium, spot, strike, expiry, side, iv_percent=None, rate=0.065):
    iv = _f(iv_percent, 0.0)
    if iv <= 0:
        iv = _f(_implied_volatility(premium, spot, strike, expiry, side, rate), 0.0)
    volatility = iv / 100.0
    years = _years_to_expiry(expiry)
    if spot <= 0 or strike <= 0 or years <= 0 or volatility <= 0:
        return {"delta": None, "gamma": None, "theta": None, "vega": None, "iv": iv or None, "greeks_source": "UNAVAILABLE"}
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility * volatility) * years) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    discount = math.exp(-rate * years)
    delta = _norm_cdf(d1) if side == "CE" else _norm_cdf(d1) - 1.0
    gamma = _norm_pdf(d1) / (spot * volatility * root_t)
    vega = spot * _norm_pdf(d1) * root_t / 100.0
    first = -(spot * _norm_pdf(d1) * volatility) / (2.0 * root_t)
    theta = (first - rate * strike * discount * _norm_cdf(d2)) / 365.0 if side == "CE" else (first + rate * strike * discount * _norm_cdf(-d2)) / 365.0
    return {"delta": round(delta, 6), "gamma": round(gamma, 8), "theta": round(theta, 6), "vega": round(vega, 6), "iv": round(iv, 4), "greeks_source": "BLACK_SCHOLES_DERIVED"}


def _quote_depth(raw):
    depth = raw.get("depth") or raw.get("market_depth") or {}
    return list(depth.get("buy") or depth.get("bids") or [])[:5], list(depth.get("sell") or depth.get("asks") or [])[:5]


def _normal_option(*, side, strike, expiry, symbol, token, exchange, lot_size, market=None, greeks=None, source):
    market, greeks = dict(market or {}), dict(greeks or {})
    buys, sells = _quote_depth(market)
    bid = _f(market.get("bid_price") or market.get("best_bid") or (buys[0].get("price") if buys else 0))
    ask = _f(market.get("ask_price") or market.get("best_ask") or (sells[0].get("price") if sells else 0))
    ltp = _f(market.get("ltp") or market.get("last_price") or market.get("last_traded_price"))
    if bid <= 0 and ltp > 0: bid = ltp
    if ask <= 0 and ltp > 0: ask = ltp
    spread = max(0.0, ask - bid) if ask > 0 and bid > 0 else 0.0
    midpoint = (ask + bid) / 2.0 if ask > 0 and bid > 0 else ltp
    spread_percent = spread / midpoint * 100.0 if midpoint > 0 else 0.0
    return {
        "side": side, "strike": round(_f(strike), 2), "expiry": str(expiry),
        "symbol": str(symbol or ""), "token": str(token or ""), "instrument_key": str(token or ""),
        "exchange": str(exchange or ""), "lot_size": max(1, _i(lot_size, 1)),
        "ltp": round(ltp, 4), "bid": round(bid, 4), "ask": round(ask, 4),
        "spread": round(spread, 4), "spread_percent": round(spread_percent, 4),
        "bid_qty": _i(market.get("bid_qty") or market.get("buy_quantity") or (buys[0].get("quantity") if buys else 0)),
        "ask_qty": _i(market.get("ask_qty") or market.get("sell_quantity") or (sells[0].get("quantity") if sells else 0)),
        "total_buy_qty": _i(market.get("totBuyQuan") or market.get("total_buy_quantity") or market.get("buy_quantity")),
        "total_sell_qty": _i(market.get("totSellQuan") or market.get("total_sell_quantity") or market.get("sell_quantity")),
        "volume": _i(market.get("volume") or market.get("tradeVolume") or market.get("trade_volume")),
        "oi": _f(market.get("oi") or market.get("opnInterest") or market.get("open_interest")),
        "prev_oi": _f(market.get("prev_oi") or market.get("previous_oi")),
        "delta": round(_f(greeks.get("delta")), 6) if greeks.get("delta") is not None else None,
        "gamma": round(_f(greeks.get("gamma")), 8) if greeks.get("gamma") is not None else None,
        "theta": round(_f(greeks.get("theta")), 6) if greeks.get("theta") is not None else None,
        "vega": round(_f(greeks.get("vega")), 6) if greeks.get("vega") is not None else None,
        "iv": round(_f(greeks.get("iv", greeks.get("impliedVolatility"))), 4) if (greeks.get("iv") is not None or greeks.get("impliedVolatility") is not None) else None,
        "pop": round(_f(greeks.get("pop")), 4) if greeks.get("pop") is not None else None,
        "greeks_source": str(greeks.get("greeks_source") or source),
        "depth_buy": buys, "depth_sell": sells, "source": source,
    }


def _angel_contract_strip(underlying, spot, wings=DEFAULT_WINGS):
    options = _load_cache(); name = str(underlying or "NIFTY").upper()
    if not options or name not in STRIKE_STEP: return []
    today = date.today(); expected = expected_expiry_for_trade_date(name, today)
    atm = get_atm_strike(name, spot); step = STRIKE_STEP[name]
    allowed = {float(atm + offset * step) for offset in range(-max(1, wings), max(1, wings) + 1)}
    rows = []
    for row in options:
        if str(row.get("name") or "").upper() != name: continue
        expiry_day = _parse_expiry(row.get("expiry"))
        if expiry_day is None or abs((expiry_day - expected).days) > 3: continue
        symbol = str(row.get("symbol") or "").upper()
        side = "CE" if symbol.endswith("CE") else "PE" if symbol.endswith("PE") else ""
        strike = _strike_of(row)
        if not side or strike is None or float(strike) not in allowed: continue
        lot_size = next((_i(row.get(k)) for k in ("lotsize", "lot_size", "minimumlot") if _i(row.get(k)) > 0), 0)
        rows.append({"side": side, "strike": float(strike), "expiry": expiry_day.isoformat(), "symbol": str(row.get("symbol") or ""), "token": str(row.get("token") or ""), "exchange": str(row.get("exch_seg") or EXCHANGE_FOR.get(name, "NFO")), "lot_size": lot_size or LOT_SIZE_FALLBACK.get(name, 1)})
    return sorted(rows, key=lambda item: (item["strike"], item["side"]))


def _angel_market_data(obj, contracts):
    exchange_tokens = {}
    for contract in contracts: exchange_tokens.setdefault(str(contract.get("exchange") or "NFO"), []).append(str(contract.get("token") or ""))
    exchange_tokens = {e: [t for t in ts if t] for e, ts in exchange_tokens.items() if any(ts)}
    if not exchange_tokens: return {}
    payload = {"mode": "FULL", "exchangeTokens": exchange_tokens}; response = None
    if hasattr(obj, "getMarketData"):
        try: response = obj.getMarketData("FULL", exchange_tokens)
        except TypeError: response = obj.getMarketData(payload)
    if response is None and hasattr(obj, "marketData"): response = obj.marketData(payload)
    if not isinstance(response, dict) or not response.get("status"): raise RuntimeError(str((response or {}).get("message") or "ANGEL_FULL_QUOTE_FAILED")[:200])
    data = response.get("data") or {}; fetched = data.get("fetched") if isinstance(data, dict) else data
    result = {}
    for row in fetched if isinstance(fetched, list) else []:
        token = str(row.get("symbolToken") or row.get("symboltoken") or row.get("token") or "")
        if token: result[token] = dict(row)
    return result


def _angel_greek_map(obj, underlying, expiry_iso):
    expiry = datetime.fromisoformat(expiry_iso).strftime("%d%b%Y").upper(); payload = {"name": str(underlying).upper(), "expirydate": expiry}; response = None
    for method_name in ("optionGreek", "optionGreekData", "getOptionGreek"):
        method = getattr(obj, method_name, None)
        if callable(method):
            try: response = method(payload); break
            except Exception: continue
    if not isinstance(response, dict) or not response.get("status"): return {}
    output = {}
    for row in response.get("data") or []:
        strike = _f(row.get("strikePrice")); side = str(row.get("optionType") or "").upper()
        if strike > 0 and side in {"CE", "PE"}:
            item = dict(row); item["greeks_source"] = "ANGEL_NATIVE_LIVE"; output[(strike, side)] = item
    return output


def _fetch_angel(obj, underlying, spot, wings):
    contracts = _angel_contract_strip(underlying, spot, wings)
    if not contracts: raise RuntimeError("ANGEL_OPTION_CONTRACTS_UNAVAILABLE")
    quote_map = _angel_market_data(obj, contracts); expiry = str(contracts[0]["expiry"]); greek_map = _angel_greek_map(obj, underlying, expiry); by_strike = {}
    for contract in contracts:
        strike, side = float(contract["strike"]), str(contract["side"])
        option = _normal_option(side=side, strike=strike, expiry=contract["expiry"], symbol=contract["symbol"], token=contract["token"], exchange=contract["exchange"], lot_size=contract["lot_size"], market=quote_map.get(str(contract["token"]), {}), greeks=greek_map.get((strike, side), {}), source="ANGEL_FULL_QUOTE")
        if option["iv"] is None or option["delta"] is None:
            derived = _derived_greeks(option["ltp"], spot, strike, contract["expiry"], side, option["iv"])
            for key in ("delta", "gamma", "theta", "vega", "iv"):
                if option.get(key) is None: option[key] = derived.get(key)
            if option.get("greeks_source") != "ANGEL_NATIVE_LIVE": option["greeks_source"] = derived["greeks_source"]
        by_strike.setdefault(strike, {"strike": strike, "expiry": contract["expiry"], "spot": spot})[side.lower()] = option
    return {"broker": "angelone", "underlying": underlying, "spot": spot, "expiry": expiry, "rows": [by_strike[k] for k in sorted(by_strike)], "native_option_chain": False}


def _fetch_upstox(obj, underlying, spot, wings):
    key = UPSTOX_UNDERLYING_KEYS.get(underlying)
    if not key: raise RuntimeError("UPSTOX_UNDERLYING_KEY_UNAVAILABLE")
    response = requests.get(f"{obj.BASE_URL}/option/chain", params={"instrument_key": key, "expiry_date": "current_week"}, headers=obj._h(), timeout=20); payload = response.json()
    if response.status_code != 200 or payload.get("status") != "success": raise RuntimeError(str(payload.get("errors") or payload)[:240])
    raw_rows = list(payload.get("data") or [])
    if not raw_rows: raise RuntimeError("UPSTOX_OPTION_CHAIN_EMPTY")
    chain_spot = _f(raw_rows[0].get("underlying_spot_price"), spot); atm = get_atm_strike(underlying, chain_spot); step = STRIKE_STEP.get(underlying, 50); low, high = atm - max(1, wings) * step, atm + max(1, wings) * step; rows = []
    for row in raw_rows:
        strike = _f(row.get("strike_price"))
        if strike < low or strike > high: continue
        normalized = {"strike": strike, "expiry": str(row.get("expiry") or ""), "spot": chain_spot, "native_pcr": _f(row.get("pcr"), 0)}
        for side, key_name in (("CE", "call_options"), ("PE", "put_options")):
            raw_option = dict(row.get(key_name) or {}); market = dict(raw_option.get("market_data") or {}); greeks = dict(raw_option.get("option_greeks") or {}); greeks["greeks_source"] = "UPSTOX_NATIVE"
            normalized[side.lower()] = _normal_option(side=side, strike=strike, expiry=normalized["expiry"], symbol=str(raw_option.get("trading_symbol") or ""), token=str(raw_option.get("instrument_key") or ""), exchange="BSE_FO" if underlying == "SENSEX" else "NSE_FO", lot_size=_i(raw_option.get("lot_size"), LOT_SIZE_FALLBACK.get(underlying, 1)), market=market, greeks=greeks, source="UPSTOX_NATIVE_CHAIN")
        rows.append(normalized)
    if not rows: raise RuntimeError("UPSTOX_CHAIN_ATM_WINDOW_EMPTY")
    return {"broker": "upstox", "underlying": underlying, "spot": chain_spot, "expiry": str(rows[0].get("expiry") or ""), "rows": sorted(rows, key=lambda item: item["strike"]), "native_option_chain": True}


def _fetch_zerodha(obj, underlying, spot, wings):
    atm = get_atm_strike(underlying, spot); step = STRIKE_STEP.get(underlying, 50); exchange = ZERODHA_EXCHANGE.get(underlying, "NFO"); today = date.today(); target_strikes = {float(atm + offset * step) for offset in range(-max(1, wings), max(1, wings) + 1)}
    eligible, expiries = [], []
    for row in list(obj.kite.instruments(exchange) or []):
        if str(row.get("name") or "").upper() != underlying: continue
        side = str(row.get("instrument_type") or "").upper()
        if side not in {"CE", "PE"}: continue
        expiry_value = row.get("expiry")
        if isinstance(expiry_value, datetime): expiry_day = expiry_value.date()
        elif isinstance(expiry_value, date): expiry_day = expiry_value
        else:
            try: expiry_day = datetime.fromisoformat(str(expiry_value)).date()
            except Exception: continue
        strike = _f(row.get("strike"))
        if expiry_day < today or strike not in target_strikes: continue
        expiries.append(expiry_day); eligible.append((expiry_day, strike, side, row))
    if not eligible: raise RuntimeError("ZERODHA_OPTION_CONTRACTS_UNAVAILABLE")
    nearest_expiry = min(expiries); contracts = []
    for expiry_day, strike, side, row in eligible:
        if expiry_day == nearest_expiry: contracts.append({"side": side, "strike": strike, "expiry": expiry_day.isoformat(), "symbol": str(row.get("tradingsymbol") or ""), "token": str(row.get("instrument_token") or ""), "exchange": exchange, "lot_size": _i(row.get("lot_size"), LOT_SIZE_FALLBACK.get(underlying, 1))})
    quote_keys = [f"{c['exchange']}:{c['symbol']}" for c in contracts]; quotes = obj.kite.quote(quote_keys); by_strike = {}
    for contract, quote_key in zip(contracts, quote_keys):
        strike, side = _f(contract.get("strike")), str(contract.get("side")); option = _normal_option(side=side, strike=strike, expiry=str(contract.get("expiry") or ""), symbol=str(contract.get("symbol") or ""), token=str(contract.get("token") or ""), exchange=str(contract.get("exchange") or ""), lot_size=_i(contract.get("lot_size"), LOT_SIZE_FALLBACK.get(underlying, 1)), market=dict(quotes.get(quote_key) or {}), greeks={}, source="ZERODHA_FULL_QUOTE"); derived = _derived_greeks(option["ltp"], spot, strike, option["expiry"], side)
        for key in ("delta", "gamma", "theta", "vega", "iv"): option[key] = derived.get(key)
        option["greeks_source"] = derived["greeks_source"]; by_strike.setdefault(strike, {"strike": strike, "expiry": option["expiry"], "spot": spot})[side.lower()] = option
    return {"broker": "zerodha", "underlying": underlying, "spot": spot, "expiry": nearest_expiry.isoformat(), "rows": [by_strike[k] for k in sorted(by_strike)], "native_option_chain": False}


def _max_pain(rows):
    strikes = sorted({_f(row.get("strike")) for row in rows if _f(row.get("strike")) > 0})
    if not strikes: return None
    best_strike, best_pain = None, None
    for settlement in strikes:
        pain = sum(max(0.0, settlement - _f(row.get("strike"))) * _f((row.get("ce") or {}).get("oi")) + max(0.0, _f(row.get("strike")) - settlement) * _f((row.get("pe") or {}).get("oi")) for row in rows)
        if best_pain is None or pain < best_pain: best_strike, best_pain = settlement, pain
    return best_strike


def _coverage_score(rows):
    options = [row.get(side) or {} for row in rows for side in ("ce", "pe") if row.get(side)]
    checks = {"premium": any(_f(o.get("ltp")) > 0 for o in options), "bid_ask": any(_f(o.get("bid")) > 0 and _f(o.get("ask")) > 0 for o in options), "open_interest": any(_f(o.get("oi")) > 0 for o in options), "greeks": any(o.get("delta") is not None for o in options), "implied_volatility": any(o.get("iv") is not None for o in options), "depth": any(_i(o.get("bid_qty")) > 0 or _i(o.get("ask_qty")) > 0 for o in options)}
    return round(sum(checks.values()) / len(checks) * 100), checks


def summarize_chain(chain, previous_oi=None):
    previous_oi = dict(previous_oi or {}); rows = [dict(row) for row in (chain.get("rows") or [])]; spot = _f(chain.get("spot")); atm = get_atm_strike(str(chain.get("underlying") or "NIFTY"), spot)
    total_call_oi = total_put_oi = call_change = put_change = 0.0; bid_qty = ask_qty = 0; spreads, ivs = [], []
    for row in rows:
        for key, direction in (("ce", "call"), ("pe", "put")):
            option = dict(row.get(key) or {})
            if not option: continue
            identity = f"{option.get('exchange')}|{option.get('token')}|{option.get('symbol')}|{option.get('side')}"; current_oi = _f(option.get("oi")); prev_native = _f(option.get("prev_oi")); prev_saved = _f(previous_oi.get(identity)); prev = prev_native if prev_native > 0 else prev_saved; oi_change = current_oi - prev if current_oi > 0 and prev > 0 else 0.0
            option["oi_change"] = round(oi_change, 2); option["oi_change_source"] = "BROKER_NATIVE" if prev_native > 0 else "PREVIOUS_RAILWAY_SNAPSHOT" if prev_saved > 0 else "UNAVAILABLE"; row[key] = option
            if direction == "call": total_call_oi += current_oi; call_change += oi_change
            else: total_put_oi += current_oi; put_change += oi_change
            bid_qty += _i(option.get("bid_qty")); ask_qty += _i(option.get("ask_qty"))
            if _f(option.get("spread_percent")) > 0: spreads.append(_f(option.get("spread_percent")))
            if _f(option.get("iv")) > 0: ivs.append(_f(option.get("iv")))
    pcr = total_put_oi / total_call_oi if total_call_oi > 0 else None; oi_change_balance = put_change - call_change; oi_change_scale = max(1.0, abs(put_change) + abs(call_change)); oi_direction_score = oi_change_balance / oi_change_scale; depth_balance = (bid_qty - ask_qty) / max(1, bid_qty + ask_qty); max_pain = _max_pain(rows); max_pain_distance = (spot - max_pain) / spot * 100.0 if spot > 0 and max_pain is not None else 0.0; average_spread = sum(spreads) / len(spreads) if spreads else 0.0; average_iv = sum(ivs) / len(ivs) if ivs else 0.0; coverage_score, coverage = _coverage_score(rows)
    bullish = bearish = 0.0; reasons = []
    if pcr is not None:
        if pcr >= 1.15: bullish += min(20.0, (pcr - 1.0) * 25.0); reasons.append("PUT_OI_SUPPORT")
        elif pcr <= 0.85: bearish += min(20.0, (1.0 - pcr) * 25.0); reasons.append("CALL_OI_RESISTANCE")
    if oi_direction_score >= 0.12: bullish += min(26.0, abs(oi_direction_score) * 35.0); reasons.append("PUT_WRITING_DOMINANT")
    elif oi_direction_score <= -0.12: bearish += min(26.0, abs(oi_direction_score) * 35.0); reasons.append("CALL_WRITING_DOMINANT")
    if depth_balance >= 0.12: bullish += min(12.0, depth_balance * 20.0); reasons.append("BID_DEPTH_STRONG")
    elif depth_balance <= -0.12: bearish += min(12.0, abs(depth_balance) * 20.0); reasons.append("ASK_DEPTH_STRONG")
    if max_pain_distance > 0.25: bearish += min(8.0, abs(max_pain_distance) * 3.0); reasons.append("SPOT_ABOVE_MAX_PAIN")
    elif max_pain_distance < -0.25: bullish += min(8.0, abs(max_pain_distance) * 3.0); reasons.append("SPOT_BELOW_MAX_PAIN")
    risk = 0.0
    if average_spread >= 2.0: risk += min(35.0, average_spread * 6.0); reasons.append("WIDE_OPTION_SPREAD")
    if average_iv >= 35.0: risk += min(25.0, (average_iv - 25.0) * 1.2); reasons.append("HIGH_IMPLIED_VOLATILITY")
    if coverage_score < 65: risk += 25.0; reasons.append("LIMITED_BROKER_DATA_COVERAGE")
    edge = bullish - bearish; direction = "CE" if edge >= 10.0 else "PE" if edge <= -10.0 else "NO_TRADE"; confidence = int(_clamp(48.0 + abs(edge) * 0.8 + coverage_score * 0.22 - risk * 0.35, 35.0, 92.0))
    if risk >= 60 or coverage_score < 45: direction = "NO_TRADE"; confidence = max(confidence, 65); reasons.append("OPTION_DATA_RISK_GATE")
    atm_row = min(rows, key=lambda row: abs(_f(row.get("strike")) - atm), default={})
    return {"version": VERSION, "broker": chain.get("broker"), "underlying": chain.get("underlying"), "spot": round(spot, 2), "expiry": chain.get("expiry"), "atm_strike": atm, "pcr": round(pcr, 4) if pcr is not None else None, "total_call_oi": round(total_call_oi, 2), "total_put_oi": round(total_put_oi, 2), "call_oi_change": round(call_change, 2), "put_oi_change": round(put_change, 2), "oi_direction_score": round(oi_direction_score, 4), "depth_imbalance": round(depth_balance, 4), "average_spread_percent": round(average_spread, 4), "average_iv": round(average_iv, 4), "max_pain": max_pain, "max_pain_distance_percent": round(max_pain_distance, 4), "option_direction": direction, "option_confidence": confidence, "risk_score": int(_clamp(risk, 0, 100)), "data_coverage_score": coverage_score, "data_coverage": coverage, "reasons": list(dict.fromkeys(reasons))[:12], "atm_row": atm_row, "rows": rows, "native_option_chain": bool(chain.get("native_option_chain"))}


def option_oi_identity_map(summary):
    output = {}
    for row in summary.get("rows") or []:
        for side in ("ce", "pe"):
            option = row.get(side) or {}; identity = f"{option.get('exchange')}|{option.get('token')}|{option.get('symbol')}|{option.get('side')}"
            if identity.strip("|"): output[identity] = _f(option.get("oi"))
    return output


def selected_contract(summary, side):
    requested = str(side or "").upper(); rows = list(summary.get("rows") or [])
    if requested not in {"CE", "PE"} or not rows: return None
    atm = _f(summary.get("atm_strike")); option = dict(min(rows, key=lambda item: abs(_f(item.get("strike")) - atm)).get(requested.lower()) or {})
    return option if option and _f(option.get("ltp")) > 0 else None


def _configured_global_keys():
    defaults = {"india_vix": "NSE_INDEX|India VIX"}; raw = str(os.getenv("OKAI_UPSTOX_GLOBAL_INSTRUMENT_KEYS_JSON", "")).strip()
    if raw:
        try:
            custom = json.loads(raw)
            if isinstance(custom, dict): defaults.update({str(k): str(v) for k, v in custom.items() if v})
        except Exception: pass
    return defaults


def _upstox_global_snapshot(obj):
    keys = _configured_global_keys(); response = requests.get(f"{obj.V3_URL}/market-quote/ohlc", params={"instrument_key": ",".join(keys.values()), "interval": "1d"}, headers=obj._h(), timeout=15); payload = response.json()
    if response.status_code != 200 or payload.get("status") != "success": return {"available": False, "source": "UPSTOX", "reason": str(payload.get("errors") or payload)[:200]}
    raw_data = payload.get("data") or {}; values = {}
    for name, instrument_key in keys.items():
        candidate = raw_data.get(instrument_key) or raw_data.get(instrument_key.replace("|", ":"))
        if candidate is None:
            for raw_key, raw_value in raw_data.items():
                if str(raw_key).replace(":", "|") == instrument_key: candidate = raw_value; break
        if candidate:
            current = _f(candidate.get("last_price") or candidate.get("ltp") or (candidate.get("live_ohlc") or {}).get("close")); previous = _f((candidate.get("prev_ohlc") or {}).get("close") or (candidate.get("ohlc") or {}).get("close")); change = (current - previous) / previous * 100.0 if current > 0 and previous > 0 else None; values[name] = {"instrument_key": instrument_key, "last_price": current, "previous_close": previous, "change_percent": round(change, 4) if change is not None else None}
    return {"available": bool(values), "source": "UPSTOX_V3", "values": values, "configured_keys": keys}


def get_broker_intelligence(user_id, market_snapshot, previous_oi=None, wings=DEFAULT_WINGS):
    broker_name, creds = _get_active_broker(int(user_id))
    if not broker_name or not creds: return {"success": False, "version": VERSION, "broker": None, "reason": "ACTIVE_BROKER_NOT_CONNECTED", "core_ai_available": True, "trade_blocking": False, "order_execution": False}
    broker = str(broker_name).lower(); underlying = str(market_snapshot.get("symbol") or market_snapshot.get("underlying") or "NIFTY").upper(); underlying = underlying if underlying in STRIKE_STEP else "NIFTY"; spot = _f(market_snapshot.get("price"))
    if spot <= 0: return {"success": False, "version": VERSION, "broker": broker, "reason": "INVALID_SPOT_PRICE", "capabilities": BROKER_CAPABILITIES.get(broker, {}), "trade_blocking": False, "order_execution": False}
    try:
        if broker == "angelone": obj = _get_ltp_session(user_id, creds); raw_chain = _fetch_angel(obj, underlying, spot, wings); global_snapshot = {"available": False, "source": "BROKER_INDEPENDENT_NEWS", "reason": "ANGEL_GLOBAL_PRICE_KEYS_NOT_NATIVE"}
        else:
            obj = _get_multi_session(user_id, broker, creds)
            if broker == "upstox": raw_chain = _fetch_upstox(obj, underlying, spot, wings); global_snapshot = _upstox_global_snapshot(obj)
            elif broker == "zerodha": raw_chain = _fetch_zerodha(obj, underlying, spot, wings); global_snapshot = {"available": False, "source": "BROKER_INDEPENDENT_NEWS", "reason": "ZERODHA_GLOBAL_PRICE_KEYS_NOT_CONFIGURED"}
            else: raise RuntimeError(f"UNSUPPORTED_BROKER:{broker}")
        return {"success": True, "version": VERSION, "broker": broker, "underlying": underlying, "as_of": _iso(), "capabilities": BROKER_CAPABILITIES.get(broker, {}), "option_intelligence": summarize_chain(raw_chain, previous_oi), "global_market": global_snapshot, "trade_blocking": False, "order_execution": False}
    except Exception as exc:
        return {"success": False, "version": VERSION, "broker": broker, "underlying": underlying, "as_of": _iso(), "reason": f"{type(exc).__name__}:{str(exc)[:260]}", "capabilities": BROKER_CAPABILITIES.get(broker, {}), "core_ai_available": True, "trade_blocking": False, "order_execution": False}

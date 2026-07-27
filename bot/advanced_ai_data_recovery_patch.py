"""Advanced AI V2 live-data recovery and diagnostics.

The base trading engine is intentionally untouched.  This patch only improves the
shadow/monitoring Advanced AI feed:

* include every actually running in-memory bot in the collector;
* expose complete AUTO market fields to the AI snapshot builder;
* recover Angel option contracts from the nearest active expiry when the configured
  expiry calendar and the live scrip master temporarily differ;
* fall back from Angel's bulk FULL quote endpoint to two paced ATM ltpData calls;
* return a live probe row to the mobile report before the first persisted sample.

Trade blocking and order execution remain OFF.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping

from bot import advanced_intelligence_v2 as advanced
from bot import angel_fetcher
from bot import auto_portfolio_runtime as portfolio
from bot import broker_intelligence as broker


_LOCK = threading.RLock()
_PROBES: Dict[int, Dict[str, Any]] = {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except (TypeError, ValueError):
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _contract_pair_available(rows: Iterable[Mapping[str, Any]]) -> bool:
    sides_by_strike: Dict[float, set] = {}
    for row in rows or []:
        try:
            strike = float(row.get("strike"))
        except (TypeError, ValueError):
            continue
        side = str(row.get("side") or "").upper()
        if side in {"CE", "PE"}:
            sides_by_strike.setdefault(strike, set()).add(side)
    return any({"CE", "PE"}.issubset(sides) for sides in sides_by_strike.values())


def _nearest_live_angel_contracts(underlying: str, spot: float, wings: int) -> List[Dict[str, Any]]:
    name = str(underlying or "NIFTY").upper()
    if name not in broker.STRIKE_STEP or _f(spot) <= 0:
        return []

    options = list(broker._load_cache() or [])
    if not options:
        return []

    today = date.today()
    atm = broker.get_atm_strike(name, spot)
    step = broker.STRIKE_STEP[name]
    width = max(1, min(_i(wings, 2), 3))
    allowed = {float(atm + offset * step) for offset in range(-width, width + 1)}

    candidates = []
    for row in options:
        if str(row.get("name") or "").upper() != name:
            continue
        expiry_day = broker._parse_expiry(row.get("expiry"))
        if expiry_day is None or expiry_day < today:
            continue
        symbol = str(row.get("symbol") or "").upper()
        side = "CE" if symbol.endswith("CE") else "PE" if symbol.endswith("PE") else ""
        strike = broker._strike_of(row)
        if side not in {"CE", "PE"} or strike is None or float(strike) not in allowed:
            continue
        candidates.append((expiry_day, float(strike), side, row))

    if not candidates:
        return []

    for expiry_day in sorted({item[0] for item in candidates}):
        same_expiry = [item for item in candidates if item[0] == expiry_day]
        sides_by_strike: Dict[float, set] = {}
        for _, strike, side, _ in same_expiry:
            sides_by_strike.setdefault(strike, set()).add(side)
        paired_strikes = [
            strike for strike, sides in sides_by_strike.items()
            if {"CE", "PE"}.issubset(sides)
        ]
        if not paired_strikes:
            continue

        output = []
        for _, strike, side, row in same_expiry:
            if strike not in paired_strikes:
                continue
            lot_size = next(
                (
                    _i(row.get(key))
                    for key in ("lotsize", "lot_size", "minimumlot")
                    if _i(row.get(key)) > 0
                ),
                0,
            )
            output.append(
                {
                    "side": side,
                    "strike": strike,
                    "expiry": expiry_day.isoformat(),
                    "symbol": str(row.get("symbol") or ""),
                    "token": str(row.get("token") or ""),
                    "exchange": str(
                        row.get("exch_seg")
                        or broker.EXCHANGE_FOR.get(name, "NFO")
                    ),
                    "lot_size": lot_size or broker.LOT_SIZE_FALLBACK.get(name, 1),
                }
            )
        if _contract_pair_available(output):
            return sorted(output, key=lambda item: (item["strike"], item["side"]))

    return []


def _atm_pair(contracts: Iterable[Mapping[str, Any]], underlying: str, spot: float) -> List[Dict[str, Any]]:
    rows = [dict(item) for item in contracts or []]
    atm = broker.get_atm_strike(str(underlying).upper(), spot)
    sides_by_strike: Dict[float, Dict[str, Dict[str, Any]]] = {}
    for item in rows:
        strike = _f(item.get("strike"), -1)
        side = str(item.get("side") or "").upper()
        if strike <= 0 or side not in {"CE", "PE"}:
            continue
        sides_by_strike.setdefault(strike, {})[side] = item
    paired = [
        strike for strike, sides in sides_by_strike.items()
        if "CE" in sides and "PE" in sides
    ]
    if not paired:
        return []
    selected = min(paired, key=lambda strike: abs(strike - atm))
    return [sides_by_strike[selected]["CE"], sides_by_strike[selected]["PE"]]


def _ltp_market_row(obj, contract: Mapping[str, Any]) -> Dict[str, Any]:
    response = obj.ltpData(
        str(contract.get("exchange") or "NFO"),
        str(contract.get("symbol") or ""),
        str(contract.get("token") or ""),
    )
    if not isinstance(response, dict) or not response.get("status"):
        message = (
            response.get("message")
            if isinstance(response, dict)
            else "Invalid Angel ltpData response"
        )
        raise RuntimeError(str(message or "ANGEL_ATM_LTP_FAILED")[:180])
    data = dict(response.get("data") or {})
    if _f(data.get("ltp")) <= 0:
        raise RuntimeError("ANGEL_ATM_LTP_ZERO")
    return data


def _fallback_angel_chain(obj, underlying: str, spot: float, wings: int, recovery_reason: str):
    contracts = broker._angel_contract_strip(underlying, spot, max(1, min(_i(wings, 2), 2)))
    pair = _atm_pair(contracts, underlying, spot)
    if len(pair) != 2:
        contracts = _nearest_live_angel_contracts(underlying, spot, max(1, min(_i(wings, 2), 2)))
        pair = _atm_pair(contracts, underlying, spot)
    if len(pair) != 2:
        raise RuntimeError("ANGEL_ATM_CE_PE_CONTRACTS_UNAVAILABLE")

    quote_map: Dict[str, Dict[str, Any]] = {}
    for index, contract in enumerate(pair):
        quote_map[str(contract.get("token") or "")] = _ltp_market_row(obj, contract)
        if index == 0:
            # Angel ltpData currently allows a faster rate, but keeping a gap makes
            # this recovery safe even while the main scanner is active.
            time.sleep(0.18)

    expiry = str(pair[0].get("expiry") or "")
    try:
        greek_map = broker._angel_greek_map(obj, underlying, expiry)
    except Exception:
        greek_map = {}

    by_strike: Dict[float, Dict[str, Any]] = {}
    for contract in pair:
        strike = _f(contract.get("strike"))
        side = str(contract.get("side") or "").upper()
        option = broker._normal_option(
            side=side,
            strike=strike,
            expiry=contract.get("expiry"),
            symbol=contract.get("symbol"),
            token=contract.get("token"),
            exchange=contract.get("exchange"),
            lot_size=contract.get("lot_size"),
            market=quote_map.get(str(contract.get("token") or ""), {}),
            greeks=greek_map.get((strike, side), {}),
            source="ANGEL_ATM_LTP_RECOVERY",
        )
        if option.get("iv") is None or option.get("delta") is None:
            derived = broker._derived_greeks(
                option.get("ltp"),
                spot,
                strike,
                str(contract.get("expiry") or ""),
                side,
                option.get("iv"),
            )
            for key in ("delta", "gamma", "theta", "vega", "iv"):
                if option.get(key) is None:
                    option[key] = derived.get(key)
            if option.get("greeks_source") != "ANGEL_NATIVE_LIVE":
                option["greeks_source"] = derived.get("greeks_source")
        by_strike.setdefault(
            strike,
            {
                "strike": strike,
                "expiry": str(contract.get("expiry") or ""),
                "spot": spot,
            },
        )[side.lower()] = option

    return {
        "broker": "angelone",
        "underlying": str(underlying).upper(),
        "spot": spot,
        "expiry": expiry,
        "rows": [by_strike[key] for key in sorted(by_strike)],
        "native_option_chain": False,
        "quote_source": "ANGEL_ATM_LTP_RECOVERY",
        "recovery_reason": str(recovery_reason or "BULK_QUOTE_UNAVAILABLE")[:220],
    }


def _chain_has_live_pair(chain: Mapping[str, Any]) -> bool:
    rows = list((chain or {}).get("rows") or [])
    for row in rows:
        ce = dict(row.get("ce") or {})
        pe = dict(row.get("pe") or {})
        if _f(ce.get("ltp")) > 0 and _f(pe.get("ltp")) > 0:
            return True
    return False


def _probe_from_result(user_id: int, market: Mapping[str, Any], result: Mapping[str, Any]) -> Dict[str, Any]:
    option = dict(result.get("option_intelligence") or {})
    return {
        "user_id": int(user_id),
        "updated_at": _iso(),
        "success": bool(result.get("success")),
        "broker": str(result.get("broker") or "").lower() or None,
        "underlying": str(
            result.get("underlying")
            or market.get("symbol")
            or market.get("underlying")
            or "NIFTY"
        ).upper(),
        "spot": round(_f(market.get("price")), 2),
        "reason": str(result.get("reason") or "")[:260] or None,
        "option_intelligence": option,
        "coverage": _i(option.get("data_coverage_score")),
        "risk": _i(option.get("risk_score")),
        "option_direction": str(option.get("option_direction") or "NO_TRADE"),
        "trade_blocking": False,
        "order_execution": False,
    }


def apply_advanced_ai_data_recovery_patch() -> None:
    if getattr(advanced, "_okai_data_recovery_v1", False):
        return

    original_contract_strip = broker._angel_contract_strip
    original_fetch_angel = broker._fetch_angel
    original_users = advanced._users
    original_option_payload = advanced._option_payload
    original_summary = advanced.get_advanced_summary
    original_health = advanced.advanced_health
    original_state_update = portfolio._state_update

    def contract_strip(underlying, spot, wings=broker.DEFAULT_WINGS):
        try:
            rows = list(original_contract_strip(underlying, spot, wings) or [])
        except Exception:
            rows = []
        if _contract_pair_available(rows):
            return rows
        recovered = _nearest_live_angel_contracts(underlying, spot, wings)
        return recovered or rows

    def fetch_angel(obj, underlying, spot, wings):
        reason = "ANGEL_BULK_QUOTE_EMPTY"
        try:
            chain = original_fetch_angel(obj, underlying, spot, wings)
            if _chain_has_live_pair(chain):
                return chain
        except Exception as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:180]}"
        return _fallback_angel_chain(obj, underlying, spot, wings, reason)

    def users():
        found = set()
        try:
            found.update(int(value) for value in original_users())
        except Exception:
            pass
        try:
            with angel_fetcher._lock:
                for user_id, state in dict(angel_fetcher._user_bots).items():
                    if isinstance(state, dict) and state.get("running"):
                        found.add(int(user_id))
        except Exception:
            pass
        return sorted(found)

    def option_payload(user_id, market):
        try:
            result = dict(original_option_payload(user_id, market) or {})
        except Exception as exc:
            result = {
                "success": False,
                "broker": None,
                "underlying": str(
                    market.get("symbol")
                    or market.get("underlying")
                    or "NIFTY"
                ).upper(),
                "reason": f"{type(exc).__name__}:{str(exc)[:240]}",
                "trade_blocking": False,
                "order_execution": False,
            }
        probe = _probe_from_result(user_id, market, result)
        with _LOCK:
            _PROBES[int(user_id)] = probe
        return result

    def state_update(state, scans, selected, settings, rows):
        original_state_update(state, scans, selected, settings, rows)
        display = portfolio._display_scan(scans, selected, settings)
        if not display:
            return
        market = dict(display.get("market_data") or {})
        for key in (
            "vwap",
            "ema9",
            "ema21",
            "adx",
            "volume_ratio",
            "supertrend_dir",
            "trend",
            "mtf_confirmed",
            "atr",
        ):
            if market.get(key) is not None:
                state[key] = market.get(key)
        state["supertrend_direction"] = market.get("supertrend_dir")
        price = _f(market.get("price"))
        atr = _f(market.get("atr"))
        state["atr_percent"] = round(atr / price * 100.0, 5) if price > 0 else 0.0

    def summary(user_id, recent_limit=20):
        data = dict(original_summary(user_id, recent_limit=recent_limit) or {})
        with _LOCK:
            probe = dict(_PROBES.get(int(user_id)) or {})
        data["current_probe"] = probe or None
        if probe and not data.get("active_broker"):
            data["active_broker"] = probe.get("broker")

        # The existing mobile app reads recent_decisions[0].  Supply a display-only
        # live probe until the first valid persistent sample is written.  It is not
        # inserted into the database and can never train the adaptive model.
        if probe and not list(data.get("recent_decisions") or []):
            option = dict(probe.get("option_intelligence") or {})
            reasons = []
            if probe.get("reason"):
                reasons.append(str(probe.get("reason")))
            reasons.extend(list(option.get("reasons") or []))
            data["recent_decisions"] = [
                {
                    "id": "LIVE_PROBE_DISPLAY_ONLY",
                    "broker": probe.get("broker"),
                    "symbol": probe.get("underlying"),
                    "spot": probe.get("spot"),
                    "advanced_decision": "COLLECTING",
                    "advanced_confidence": 0,
                    "advanced_probabilities": {},
                    "option_decision": option.get("option_direction") or "NO_TRADE",
                    "option_confidence": _i(option.get("option_confidence")),
                    "data_coverage_score": _i(option.get("data_coverage_score")),
                    "option_risk_score": _i(option.get("risk_score")),
                    "option_summary": option,
                    "reasons": list(dict.fromkeys(reasons))[:12],
                    "display_only": True,
                    "trade_blocking": False,
                    "order_execution": False,
                }
            ]
        return data

    def health():
        data = dict(original_health() or {})
        with _LOCK:
            probes = list(_PROBES.values())
        data["live_probe_count"] = len(probes)
        data["live_probes"] = probes[-20:]
        return data

    broker._angel_contract_strip = contract_strip
    broker._fetch_angel = fetch_angel
    advanced._users = users
    advanced._option_payload = option_payload
    advanced.get_advanced_summary = summary
    advanced.advanced_health = health
    portfolio._state_update = state_update

    advanced._okai_data_recovery_v1 = True
    broker._okai_angel_option_recovery_v1 = True
    portfolio._okai_advanced_market_fields_v1 = True

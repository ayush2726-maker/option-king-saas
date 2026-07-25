"""Runtime fix for Upstox option-chain exact expiry resolution."""
from __future__ import annotations

import requests


def install():
    from bot import broker_intelligence as module

    def fetch_upstox(obj, underlying, spot, wings):
        key = module.UPSTOX_UNDERLYING_KEYS.get(underlying)
        if not key:
            raise RuntimeError("UPSTOX_UNDERLYING_KEY_UNAVAILABLE")
        atm = module.get_atm_strike(underlying, spot)
        resolver = obj.search_option(underlying, "current_week", atm, "CE") or {}
        expiry = str(resolver.get("expiry") or "")[:10]
        if not resolver.get("success") or not expiry:
            raise RuntimeError("UPSTOX_CURRENT_EXPIRY_UNAVAILABLE")
        response = requests.get(
            f"{obj.BASE_URL}/option/chain",
            params={"instrument_key": key, "expiry_date": expiry},
            headers=obj._h(),
            timeout=20,
        )
        payload = response.json()
        if response.status_code != 200 or payload.get("status") != "success":
            raise RuntimeError(str(payload.get("errors") or payload)[:240])
        raw_rows = list(payload.get("data") or [])
        if not raw_rows:
            raise RuntimeError("UPSTOX_OPTION_CHAIN_EMPTY")
        chain_spot = module._f(raw_rows[0].get("underlying_spot_price"), spot)
        atm = module.get_atm_strike(underlying, chain_spot)
        step = module.STRIKE_STEP.get(underlying, 50)
        low, high = atm - max(1, wings) * step, atm + max(1, wings) * step
        rows = []
        for row in raw_rows:
            strike = module._f(row.get("strike_price"))
            if strike < low or strike > high:
                continue
            normalized = {
                "strike": strike,
                "expiry": str(row.get("expiry") or expiry),
                "spot": chain_spot,
                "native_pcr": module._f(row.get("pcr"), 0),
            }
            for side, key_name in (("CE", "call_options"), ("PE", "put_options")):
                raw_option = dict(row.get(key_name) or {})
                market = dict(raw_option.get("market_data") or {})
                greeks = dict(raw_option.get("option_greeks") or {})
                greeks["greeks_source"] = "UPSTOX_NATIVE"
                normalized[side.lower()] = module._normal_option(
                    side=side,
                    strike=strike,
                    expiry=normalized["expiry"],
                    symbol=str(raw_option.get("trading_symbol") or ""),
                    token=str(raw_option.get("instrument_key") or ""),
                    exchange="BSE_FO" if underlying == "SENSEX" else "NSE_FO",
                    lot_size=module._i(
                        raw_option.get("lot_size"),
                        module.LOT_SIZE_FALLBACK.get(underlying, 1),
                    ),
                    market=market,
                    greeks=greeks,
                    source="UPSTOX_NATIVE_CHAIN",
                )
            rows.append(normalized)
        if not rows:
            raise RuntimeError("UPSTOX_CHAIN_ATM_WINDOW_EMPTY")
        return {
            "broker": "upstox",
            "underlying": underlying,
            "spot": chain_spot,
            "expiry": expiry,
            "rows": sorted(rows, key=lambda item: item["strike"]),
            "native_option_chain": True,
        }

    module._fetch_upstox = fetch_upstox
    return True


install()

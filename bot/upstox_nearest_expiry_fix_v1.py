"""Force Upstox option intelligence to use the actual configured nearest expiry.

Upstox's option-chain API expects a concrete YYYY-MM-DD expiry. Passing the
literal value ``current_week`` can resolve to an unintended farther contract.
That polluted live/missed-trade premium capture (e.g. NIFTY 25-Aug instead of
18-Aug on 13-Aug-2026).

This patch tries the configured expiry first and only holiday-adjusts backward
within three calendar days. It never silently jumps to the next weekly expiry.
"""
from __future__ import annotations

from datetime import date, timedelta

VERSION = "OKAI-UPSTOX-NEAREST-EXPIRY-FIX-V1"


def apply_upstox_nearest_expiry_fix() -> bool:
    try:
        from bot import broker_intelligence as bi
        if getattr(bi, "UPSTOX_NEAREST_EXPIRY_FIX_APPLIED", False):
            return True

        def _fetch_upstox_nearest(obj, underlying, spot, wings):
            name = str(underlying or "NIFTY").upper()
            key = bi.UPSTOX_UNDERLYING_KEYS.get(name)
            if not key:
                raise RuntimeError("UPSTOX_UNDERLYING_KEY_UNAVAILABLE")

            today = date.today()
            expected = bi.expected_expiry_for_trade_date(name, today)
            # Exchange holidays move an expiry earlier; never roll forward to
            # the following week/month merely because the expected date fails.
            expiry_candidates = [expected - timedelta(days=offset) for offset in range(0, 4)]
            raw_rows = []
            selected_expiry = None
            last_error = None

            for expiry_day in expiry_candidates:
                if expiry_day < today:
                    continue
                try:
                    response = bi.requests.get(
                        f"{obj.BASE_URL}/option/chain",
                        params={
                            "instrument_key": key,
                            "expiry_date": expiry_day.isoformat(),
                        },
                        headers=obj._h(),
                        timeout=20,
                    )
                    payload = response.json()
                    if response.status_code == 200 and payload.get("status") == "success":
                        rows = list(payload.get("data") or [])
                        if rows:
                            raw_rows = rows
                            selected_expiry = expiry_day
                            break
                    last_error = str(payload.get("errors") or payload)[:240]
                except Exception as exc:
                    last_error = f"{type(exc).__name__}:{str(exc)[:180]}"

            if not raw_rows or selected_expiry is None:
                raise RuntimeError(
                    "UPSTOX_NEAREST_EXPIRY_CHAIN_UNAVAILABLE:"
                    + str(last_error or expected.isoformat())
                )

            chain_spot = bi._f(raw_rows[0].get("underlying_spot_price"), spot)
            atm = bi.get_atm_strike(name, chain_spot)
            step = bi.STRIKE_STEP.get(name, 50)
            low = atm - max(1, wings) * step
            high = atm + max(1, wings) * step
            rows = []

            for row in raw_rows:
                strike = bi._f(row.get("strike_price"))
                if strike < low or strike > high:
                    continue
                row_expiry = str(row.get("expiry") or selected_expiry.isoformat())
                # Fail closed if the broker unexpectedly returned another expiry.
                try:
                    parsed = bi.datetime.fromisoformat(row_expiry[:10]).date()
                    if parsed != selected_expiry:
                        continue
                except Exception:
                    continue

                normalized = {
                    "strike": strike,
                    "expiry": selected_expiry.isoformat(),
                    "spot": chain_spot,
                    "native_pcr": bi._f(row.get("pcr"), 0),
                }
                for side, key_name in (("CE", "call_options"), ("PE", "put_options")):
                    raw_option = dict(row.get(key_name) or {})
                    market = dict(raw_option.get("market_data") or {})
                    greeks = dict(raw_option.get("option_greeks") or {})
                    greeks["greeks_source"] = "UPSTOX_NATIVE"
                    normalized[side.lower()] = bi._normal_option(
                        side=side,
                        strike=strike,
                        expiry=selected_expiry.isoformat(),
                        symbol=str(raw_option.get("trading_symbol") or ""),
                        token=str(raw_option.get("instrument_key") or ""),
                        exchange="BSE_FO" if name == "SENSEX" else "NSE_FO",
                        lot_size=bi._i(
                            raw_option.get("lot_size"),
                            bi.LOT_SIZE_FALLBACK.get(name, 1),
                        ),
                        market=market,
                        greeks=greeks,
                        source="UPSTOX_NATIVE_CHAIN_NEAREST_EXPIRY",
                    )
                rows.append(normalized)

            if not rows:
                raise RuntimeError("UPSTOX_NEAREST_EXPIRY_ATM_WINDOW_EMPTY")

            return {
                "broker": "upstox",
                "underlying": name,
                "spot": chain_spot,
                "expiry": selected_expiry.isoformat(),
                "expected_expiry": expected.isoformat(),
                "expiry_selection": "STRICT_NEAREST_CONFIGURED_OR_HOLIDAY_EARLIER",
                "rows": sorted(rows, key=lambda item: item["strike"]),
                "native_option_chain": True,
            }

        bi._fetch_upstox = _fetch_upstox_nearest
        bi.UPSTOX_NEAREST_EXPIRY_FIX_APPLIED = True
        bi.UPSTOX_NEAREST_EXPIRY_FIX_VERSION = VERSION
        return True
    except Exception:
        return False


__all__ = ["apply_upstox_nearest_expiry_fix", "VERSION"]

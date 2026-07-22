import requests
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

from .base import BaseBroker


class UpstoxBroker(BaseBroker):
    BASE_URL = "https://api.upstox.com/v2"
    V3_URL = "https://api.upstox.com/v3"

    INDEX_SEARCH_HINTS = {
        "NSE_INDEX|Nifty 50": {
            "query": "NIFTY",
            "exchange": "NSE",
            "trading_symbol": "NIFTY",
        },
        "NSE_INDEX|Nifty Bank": {
            "query": "BANKNIFTY",
            "exchange": "NSE",
            "trading_symbol": "BANKNIFTY",
        },
        "BSE_INDEX|SENSEX": {
            "query": "SENSEX",
            "exchange": "BSE",
            "trading_symbol": "SENSEX",
        },
    }

    @classmethod
    def broker_name(cls):
        return "upstox"

    @classmethod
    def display_name(cls):
        return "Upstox"

    @classmethod
    def required_fields(cls):
        return [
            {
                "key": "client_id",
                "label": "API Key (Client ID)",
                "hint": "Developer Apps > Option King AI > API Key",
                "optional": False,
            },
            {
                "key": "api_key",
                "label": "API Secret",
                "hint": "Developer Apps > Option King AI > API Secret",
                "optional": False,
            },
            {
                "key": "api_secret",
                "label": "Daily Access Token",
                "hint": "Developer Apps me Generate karke token copy karein",
                "optional": False,
            },
        ]

    @classmethod
    def setup_guide(cls):
        return {
            "broker": "Upstox",
            "api_cost": "Free",
            "free": True,
            "api_portal_url": "https://account.upstox.com/developer/apps",
            "create_app_url": "https://account.upstox.com/developer/apps/createapp",
            "official_auth_docs": "https://upstox.com/developer/api-documentation/authentication/",
            "app_name": "Option King AI",
            "redirect_url": "https://option-king-saas-production.up.railway.app/upstox/callback",
            "postback_url": "https://option-king-saas-production.up.railway.app/upstox/postback",
            "token_type": "Standard daily Access Token",
            "token_expiry_note": "Standard token agle din 3:30 AM tak valid hota hai.",
            "static_ip_note": "Live API orders ke liye registered static IP required ho sakti hai.",
            "steps": [
                {
                    "step": 1,
                    "title": "Developer Apps",
                    "description": "Upstox Developer Apps page kholein.",
                    "url": "https://account.upstox.com/developer/apps",
                },
                {
                    "step": 2,
                    "title": "Create App",
                    "description": "App Name me Option King AI daalein.",
                    "url": "https://account.upstox.com/developer/apps/createapp",
                },
                {
                    "step": 3,
                    "title": "Exact URLs",
                    "description": "Redirect aur Postback URL guide me diye exact values se bharein.",
                    "url": None,
                },
                {
                    "step": 4,
                    "title": "API Key and Secret",
                    "description": "Created app se API Key aur API Secret copy karein.",
                    "url": None,
                },
                {
                    "step": 5,
                    "title": "Generate Token",
                    "description": "Generate dabakar standard daily Access Token copy karein.",
                    "url": None,
                },
                {
                    "step": 6,
                    "title": "Fill OKAI",
                    "description": "API Key, API Secret aur Daily Access Token bharein. TOTP nahi chahiye.",
                    "url": None,
                },
                {
                    "step": 7,
                    "title": "Save and Test",
                    "description": "Save Credentials aur Test Broker Connection dabayein.",
                    "url": None,
                },
            ],
        }

    def _h(self):
        return {
            "Authorization": f"Bearer {self.api_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _day(value):
        return str(value or "")[:10]

    @staticmethod
    def _today_ist():
        return (
            datetime.now(timezone.utc)
            + timedelta(hours=5, minutes=30)
        ).strftime("%Y-%m-%d")

    @staticmethod
    def _error_code(payload):
        if not isinstance(payload, dict):
            return ""
        errors = payload.get("errors") or []
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            return str(
                first.get("errorCode")
                or first.get("error_code")
                or ""
            )
        return str(payload.get("errorCode") or payload.get("error_code") or "")

    @staticmethod
    def _message(payload):
        if not isinstance(payload, dict):
            return str(payload)[:300]
        return str(
            payload.get("errors")
            or payload.get("message")
            or payload
        )[:300]

    def _v3_candle_url(
        self,
        instrument_key,
        unit,
        number,
        from_day,
        to_day,
    ):
        encoded_key = quote(str(instrument_key), safe="")
        is_current_intraday = (
            from_day == to_day == self._today_ist()
        )
        if is_current_intraday:
            return (
                f"{self.V3_URL}/historical-candle/intraday/"
                f"{encoded_key}/{unit}/{number}"
            ), "INTRADAY_CURRENT_DAY"

        # A single historical date still needs the historical endpoint. The old
        # code used the intraday endpoint whenever from_day == to_day, causing
        # every Daily/Monthly backtest date to be skipped.
        return (
            f"{self.V3_URL}/historical-candle/{encoded_key}/"
            f"{unit}/{number}/{to_day}/{from_day}"
        ), "HISTORICAL_V3"

    def _resolve_index_key(self, supplied_key):
        supplied = str(supplied_key or "").strip()
        hint = self.INDEX_SEARCH_HINTS.get(supplied)
        if hint is None:
            upper = supplied.upper()
            if "BANK" in upper:
                hint = self.INDEX_SEARCH_HINTS["NSE_INDEX|Nifty Bank"]
            elif "SENSEX" in upper:
                hint = self.INDEX_SEARCH_HINTS["BSE_INDEX|SENSEX"]
            elif "NIFTY" in upper:
                hint = self.INDEX_SEARCH_HINTS["NSE_INDEX|Nifty 50"]
            else:
                return supplied

        response = requests.get(
            f"{self.BASE_URL}/instruments/search",
            params={
                "query": hint["query"],
                "exchanges": hint["exchange"],
                "segments": "INDEX",
                "page_number": 1,
                "records": 30,
            },
            headers=self._h(),
            timeout=15,
        )
        if response.status_code != 200:
            return supplied

        payload = response.json()
        target = hint["trading_symbol"].upper()
        candidates = []
        for row in payload.get("data") or []:
            if str(row.get("instrument_type") or "").upper() != "INDEX":
                continue
            key = str(row.get("instrument_key") or "").strip()
            if not key:
                continue
            trading_symbol = str(row.get("trading_symbol") or "").upper()
            name = str(row.get("name") or "").upper()
            rank = 0 if trading_symbol == target else 1 if target in name else 2
            candidates.append((rank, key))

        return min(candidates, default=(99, supplied))[1]

    def _v2_interval(self, interval):
        return {
            "1m": "1minute",
            "3m": "3minute",
            "5m": "5minute",
            "15m": "15minute",
            "30m": "30minute",
            "1h": "60minute",
            "1d": "day",
        }.get(str(interval).lower(), "5minute")

    def _fetch_v2_historical(
        self,
        instrument_key,
        interval,
        from_day,
        to_day,
    ):
        encoded_key = quote(str(instrument_key), safe="")
        url = (
            f"{self.BASE_URL}/historical-candle/{encoded_key}/"
            f"{self._v2_interval(interval)}/{to_day}/{from_day}"
        )
        response = requests.get(url, headers=self._h(), timeout=20)
        payload = response.json()
        return response, payload, url

    def login(self):
        try:
            response = requests.get(
                f"{self.BASE_URL}/user/profile",
                headers=self._h(),
                timeout=10,
            )
            if response.status_code == 200:
                self.is_logged_in = True
                return {
                    "success": True,
                    "message": "Upstox session valid",
                    "token": self.api_secret,
                }
            return {
                "success": False,
                "message": f"Invalid token: {response.text}",
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def logout(self):
        self.is_logged_in = False
        return {"success": True}

    def get_ltp(self, symbol, exchange="NFO"):
        try:
            instrument = symbol if "|" in str(symbol) else f"NSE_FO|{symbol}"
            response = requests.get(
                f"{self.BASE_URL}/market-quote/ltp",
                params={"instrument_key": instrument},
                headers=self._h(),
                timeout=10,
            )
            data = response.json().get("data", {})
            key_variant = instrument.replace("|", ":")
            entry = data.get(instrument) or data.get(key_variant)
            if not entry:
                entry = next(iter(data.values()), None)
            if not entry:
                return {
                    "success": False,
                    "message": f"No LTP data: {response.text[:200]}",
                }
            return {
                "success": True,
                "ltp": entry["last_price"],
                "symbol": symbol,
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def get_candles(
        self,
        symbol,
        interval,
        from_date,
        to_date,
        exchange="NFO",
    ):
        """Fetch Upstox candles for index/equity/derivative instrument keys.

        Current-day requests use V3 intraday. Past dates, including one exact
        historical date, use V3 historical. Invalid index keys are resolved once
        through Upstox Instrument Search, with V2 historical as a final fallback.
        """
        try:
            interval_map = {
                "1m": ("minutes", 1),
                "3m": ("minutes", 3),
                "5m": ("minutes", 5),
                "15m": ("minutes", 15),
                "30m": ("minutes", 30),
                "1h": ("hours", 1),
                "1d": ("days", 1),
            }
            unit, number = interval_map.get(
                str(interval).lower(),
                ("minutes", 5),
            )
            instrument_key = (
                str(symbol).strip()
                if symbol is not None
                else ""
            )
            if not instrument_key or instrument_key.lower() == "none":
                return {
                    "success": False,
                    "message": "UPSTOX_INSTRUMENT_KEY_MISSING",
                }
            if "|" not in instrument_key:
                instrument_key = f"NSE_FO|{instrument_key}"

            from_day = self._day(from_date)
            to_day = self._day(to_date)
            if not from_day or not to_day:
                return {
                    "success": False,
                    "message": "UPSTOX_CANDLE_DATE_MISSING",
                }
            if to_day < from_day:
                from_day, to_day = to_day, from_day

            url, request_mode = self._v3_candle_url(
                instrument_key,
                unit,
                number,
                from_day,
                to_day,
            )
            response = requests.get(url, headers=self._h(), timeout=20)
            payload = response.json()

            error_code = self._error_code(payload)
            if (
                response.status_code != 200
                and error_code == "UDAPI100011"
                and "_INDEX|" in instrument_key.upper()
            ):
                resolved_key = self._resolve_index_key(instrument_key)
                if resolved_key:
                    instrument_key = resolved_key
                    url, request_mode = self._v3_candle_url(
                        instrument_key,
                        unit,
                        number,
                        from_day,
                        to_day,
                    )
                    response = requests.get(
                        url,
                        headers=self._h(),
                        timeout=20,
                    )
                    payload = response.json()
                    error_code = self._error_code(payload)

            # Some Upstox accounts temporarily reject an otherwise valid index
            # key on V3 historical. V2 accepts the same official instrument key.
            if (
                (response.status_code != 200 or payload.get("status") != "success")
                and request_mode == "HISTORICAL_V3"
                and error_code == "UDAPI100011"
            ):
                response, payload, url = self._fetch_v2_historical(
                    instrument_key,
                    interval,
                    from_day,
                    to_day,
                )
                request_mode = "HISTORICAL_V2_FALLBACK"

            if response.status_code != 200 or payload.get("status") != "success":
                return {
                    "success": False,
                    "message": self._message(payload),
                    "error_code": self._error_code(payload),
                    "instrument_key": instrument_key,
                    "request_mode": request_mode,
                }

            candles = payload.get("data", {}).get("candles", []) or []
            candles.sort(key=lambda row: str(row[0]) if row else "")
            return {
                "success": True,
                "candles": candles,
                "raw_status": payload.get("status"),
                "instrument_key": instrument_key,
                "request_mode": request_mode,
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def search_option(self, underlying, expiry, strike, option_type):
        try:
            underlying_name = str(underlying).upper()
            option_type_name = str(option_type).upper()
            exchange = "BSE" if underlying_name == "SENSEX" else "NSE"
            response = requests.get(
                f"{self.BASE_URL}/instruments/search",
                params={
                    "query": underlying_name,
                    "exchanges": exchange,
                    "segments": "FO",
                    "instrument_types": option_type_name,
                    "expiry": expiry or "current_week",
                    "atm_offset": 0,
                    "page_number": 1,
                    "records": 30,
                },
                headers=self._h(),
                timeout=15,
            )
            payload = response.json()
            if response.status_code != 200:
                return {"success": False, "message": str(payload)[:250]}
            found = []
            for row in payload.get("data") or []:
                if str(row.get("instrument_type") or "").upper() != option_type_name:
                    continue
                if str(row.get("underlying_symbol") or "").upper() != underlying_name:
                    continue
                row_strike = float(row.get("strike_price") or 0)
                found.append(
                    (
                        str(row.get("expiry") or ""),
                        abs(row_strike - float(strike)),
                        row,
                    )
                )
            if not found:
                return {"success": False, "message": "Upstox option not found"}
            _, _, best = min(found, key=lambda item: (item[0], item[1]))
            return {
                "success": True,
                "symbol": best["trading_symbol"],
                "token": best["instrument_key"],
                "exchange": best.get("segment")
                or ("BSE_FO" if underlying_name == "SENSEX" else "NSE_FO"),
                "expiry": str(best.get("expiry") or ""),
                "strike": float(best.get("strike_price") or 0),
                "lot_size": int(best.get("lot_size") or 0),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def place_order(
        self,
        symbol,
        token,
        transaction_type,
        quantity,
        order_type="MARKET",
        price=0,
        exchange="NFO",
    ):
        try:
            raw_token = str(token or "")
            if "|" in raw_token:
                instrument_token = raw_token
            else:
                segment = (
                    "BSE_FO"
                    if str(exchange).upper().startswith("BSE")
                    else "NSE_FO"
                )
                instrument_token = f"{segment}|{symbol}"
            response = requests.post(
                f"{self.BASE_URL}/order/place",
                json={
                    "quantity": quantity,
                    "product": "I",
                    "validity": "DAY",
                    "price": price,
                    "instrument_token": instrument_token,
                    "order_type": order_type,
                    "transaction_type": transaction_type,
                    "disclosed_quantity": 0,
                    "trigger_price": 0,
                    "is_amo": False,
                },
                headers=self._h(),
                timeout=10,
            )
            data = response.json()
            if data.get("status") == "success":
                order_id = data["data"]["order_id"]
                return {
                    "success": True,
                    "order_id": order_id,
                    "message": f"Upstox order: {order_id}",
                }
            return {
                "success": False,
                "message": str(data.get("errors", data)),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def get_order_status(self, order_id):
        try:
            response = requests.get(
                f"{self.BASE_URL}/order/details",
                params={"order_id": order_id},
                headers=self._h(),
                timeout=10,
            )
            data = response.json().get("data", {})
            return {
                "success": True,
                "status": data.get("status", ""),
                "filled_qty": data.get("filled_quantity", 0),
                "avg_price": data.get("average_price", 0),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def get_positions(self):
        try:
            response = requests.get(
                f"{self.BASE_URL}/portfolio/short-term-positions",
                headers=self._h(),
                timeout=10,
            )
            return {
                "success": True,
                "positions": response.json().get("data", []),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    def close_position(self, symbol, token, quantity, exchange="NFO"):
        return self.place_order(symbol, token, "SELL", quantity)

    def get_funds(self):
        try:
            response = requests.get(
                f"{self.BASE_URL}/user/get-funds-and-margin",
                params={"segment": "SEC"},
                headers=self._h(),
                timeout=10,
            )
            equity = response.json().get("data", {}).get("equity", {})
            return {
                "success": True,
                "available_cash": equity.get("available_margin", 0),
                "used_margin": equity.get("used_margin", 0),
            }
        except Exception as exc:
            return {"success": False, "message": str(exc)}

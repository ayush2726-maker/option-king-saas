import requests
from .base import BaseBroker

class UpstoxBroker(BaseBroker):
    BASE_URL = "https://api.upstox.com/v2"

    @classmethod
    def broker_name(cls): return "upstox"
    @classmethod
    def display_name(cls): return "Upstox"
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
                {"step": 1, "title": "Developer Apps", "description": "Upstox Developer Apps page kholein.", "url": "https://account.upstox.com/developer/apps"},
                {"step": 2, "title": "Create App", "description": "App Name me Option King AI daalein.", "url": "https://account.upstox.com/developer/apps/createapp"},
                {"step": 3, "title": "Exact URLs", "description": "Redirect aur Postback URL guide me diye exact values se bharein.", "url": None},
                {"step": 4, "title": "API Key and Secret", "description": "Created app se API Key aur API Secret copy karein.", "url": None},
                {"step": 5, "title": "Generate Token", "description": "Generate dabakar standard daily Access Token copy karein.", "url": None},
                {"step": 6, "title": "Fill OKAI", "description": "API Key, API Secret aur Daily Access Token bharein. TOTP nahi chahiye.", "url": None},
                {"step": 7, "title": "Save and Test", "description": "Save Credentials aur Test Broker Connection dabayein.", "url": None},
            ],
        }

    def _h(self):
        return {"Authorization":f"Bearer {self.api_secret}","Content-Type":"application/json","Accept":"application/json"}

    def login(self):
        try:
            r = requests.get(f"{self.BASE_URL}/user/profile", headers=self._h(), timeout=10)
            if r.status_code == 200:
                self.is_logged_in = True
                return {"success":True,"message":"Upstox session valid","token":self.api_secret}
            return {"success":False,"message":f"Invalid token: {r.text}"}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def logout(self):
        self.is_logged_in = False
        return {"success":True}

    def get_ltp(self, symbol, exchange="NFO"):
        try:
            inst = symbol if "|" in symbol else f"NSE_FO|{symbol}"
            r = requests.get(f"{self.BASE_URL}/market-quote/ltp", params={"instrument_key":inst}, headers=self._h(), timeout=10)
            data = r.json().get("data", {})
            key_variant = inst.replace("|", ":")
            entry = data.get(inst) or data.get(key_variant)
            if not entry:
                entry = next(iter(data.values()), None)
            if not entry:
                return {"success":False,"message":f"No LTP data: {r.text[:200]}"}
            return {"success":True,"ltp":entry["last_price"],"symbol":symbol}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_candles(self, symbol, interval, from_date, to_date, exchange="NFO"):
        try:
            from datetime import datetime
            m = {"1m":"1minute","5m":"5minute","15m":"15minute","1h":"60minute","1d":"1day"}
            inst = symbol if "|" in symbol else f"NSE_FO|{symbol}"
            today_str = datetime.now().strftime("%Y-%m-%d")
            if to_date == today_str or from_date == today_str:
                r = requests.get(f"{self.BASE_URL}/historical-candle/intraday/{inst}/{m.get(interval,'5minute')}", headers=self._h(), timeout=15)
            else:
                r = requests.get(f"{self.BASE_URL}/historical-candle/{inst}/{m.get(interval,'5minute')}/{to_date}/{from_date}", headers=self._h(), timeout=15)
            data = r.json()
            return {"success":True,"candles":data.get("data",{}).get("candles",[]),"raw_status":data.get("status")}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def search_option(self, underlying, expiry, strike, option_type):
        try:
            u = str(underlying).upper()
            ot = str(option_type).upper()
            exchange = "BSE" if u == "SENSEX" else "NSE"
            r = requests.get(
                f"{self.BASE_URL}/instruments/search",
                params={
                    "query": u,
                    "exchanges": exchange,
                    "segments": "FO",
                    "instrument_types": ot,
                    "expiry": expiry or "current_week",
                    "atm_offset": 0,
                    "page_number": 1,
                    "records": 30,
                },
                headers=self._h(),
                timeout=15,
            )
            payload = r.json()
            if r.status_code != 200:
                return {"success":False,"message":str(payload)[:250]}
            found = []
            for row in payload.get("data") or []:
                if str(row.get("instrument_type") or "").upper() != ot:
                    continue
                if str(row.get("underlying_symbol") or "").upper() != u:
                    continue
                rs = float(row.get("strike_price") or 0)
                found.append((str(row.get("expiry") or ""), abs(rs-float(strike)), row))
            if not found:
                return {"success":False,"message":"Upstox option not found"}
            _, _, best = min(found, key=lambda x: (x[0], x[1]))
            return {
                "success": True,
                "symbol": best["trading_symbol"],
                "token": best["instrument_key"],
                "exchange": best.get("segment") or ("BSE_FO" if u=="SENSEX" else "NSE_FO"),
                "expiry": str(best.get("expiry") or ""),
                "strike": float(best.get("strike_price") or 0),
                "lot_size": int(best.get("lot_size") or 0),
            }
        except Exception as e:
            return {"success":False,"message":str(e)}

    def place_order(self, symbol, token, transaction_type, quantity, order_type="MARKET", price=0, exchange="NFO"):
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
            r = requests.post(f"{self.BASE_URL}/order/place", json={"quantity":quantity,"product":"I","validity":"DAY","price":price,"instrument_token":instrument_token,"order_type":order_type,"transaction_type":transaction_type,"disclosed_quantity":0,"trigger_price":0,"is_amo":False}, headers=self._h(), timeout=10)
            data = r.json()
            if data.get("status") == "success":
                oid = data["data"]["order_id"]
                return {"success":True,"order_id":oid,"message":f"Upstox order: {oid}"}
            return {"success":False,"message":str(data.get("errors",data))}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_order_status(self, order_id):
        try:
            r = requests.get(f"{self.BASE_URL}/order/details", params={"order_id":order_id}, headers=self._h(), timeout=10)
            d = r.json().get("data",{})
            return {"success":True,"status":d.get("status",""),"filled_qty":d.get("filled_quantity",0),"avg_price":d.get("average_price",0)}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_positions(self):
        try:
            r = requests.get(f"{self.BASE_URL}/portfolio/short-term-positions", headers=self._h(), timeout=10)
            return {"success":True,"positions":r.json().get("data",[])}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def close_position(self, symbol, token, quantity, exchange="NFO"):
        return self.place_order(symbol, token, "SELL", quantity)

    def get_funds(self):
        try:
            r = requests.get(f"{self.BASE_URL}/user/get-funds-and-margin", params={"segment":"SEC"}, headers=self._h(), timeout=10)
            eq = r.json().get("data",{}).get("equity",{})
            return {"success":True,"available_cash":eq.get("available_margin",0),"used_margin":eq.get("used_margin",0)}
        except Exception as e:
            return {"success":False,"message":str(e)}

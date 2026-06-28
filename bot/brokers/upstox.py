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
            {"key":"client_id","label":"API Key (Client ID)","hint":"Upstox developer portal se","optional":False},
            {"key":"api_key","label":"API Secret","hint":"Upstox developer portal se","optional":False},
            {"key":"api_secret","label":"Access Token","hint":"OAuth login ke baad milta hai","optional":False}
        ]
    @classmethod
    def setup_guide(cls):
        return {
            "broker":"Upstox","api_cost":"Free","free":True,
            "api_portal_url":"https://developer.upstox.com",
            "steps":[
                {"step":1,"title":"Upstox Account","description":"upstox.com pe account kholein. F&O trading enable karwayein.","url":"https://upstox.com"},
                {"step":2,"title":"Developer Portal","description":"developer.upstox.com pe login karein aur Create App karein.","url":"https://developer.upstox.com"},
                {"step":3,"title":"App Create Karein","description":"App name daalo. Redirect URL http://localhost daalo. Client ID aur Secret milega.","url":None},
                {"step":4,"title":"Access Token","description":"Pehli baar OAuth login karke access token generate karein.","url":None}
            ]
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
            inst = f"NSE_FO|{symbol}"
            r = requests.get(f"{self.BASE_URL}/market-quote/ltp", params={"instrument_key":inst}, headers=self._h(), timeout=10)
            ltp = r.json()["data"][inst]["last_price"]
            return {"success":True,"ltp":ltp,"symbol":symbol}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_candles(self, symbol, interval, from_date, to_date, exchange="NFO"):
        try:
            m = {"1m":"1minute","5m":"5minute","15m":"15minute","1h":"60minute","1d":"1day"}
            r = requests.get(f"{self.BASE_URL}/historical-candle/NSE_FO|{symbol}/{m.get(interval,'5minute')}/{to_date}/{from_date}", headers=self._h(), timeout=15)
            return {"success":True,"candles":r.json().get("data",{}).get("candles",[])}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def search_option(self, underlying, expiry, strike, option_type):
        return {"success":True,"symbol":f"{underlying}{expiry}{int(strike)}{option_type}","token":""}

    def place_order(self, symbol, token, transaction_type, quantity, order_type="MARKET", price=0, exchange="NFO"):
        try:
            r = requests.post(f"{self.BASE_URL}/order/place", json={"quantity":quantity,"product":"I","validity":"DAY","price":price,"instrument_token":f"NSE_FO|{symbol}","order_type":order_type,"transaction_type":transaction_type,"disclosed_quantity":0,"trigger_price":0,"is_amo":False}, headers=self._h(), timeout=10)
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

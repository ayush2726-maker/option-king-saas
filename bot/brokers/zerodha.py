from .base import BaseBroker
try:
    from kiteconnect import KiteConnect
except ImportError:
    KiteConnect = None

class ZerodhaBroker(BaseBroker):
    def __init__(self, client_id, api_key, api_secret, totp_secret=None):
        super().__init__(client_id, api_key, api_secret, totp_secret)
        self.kite = None

    @classmethod
    def broker_name(cls): return "zerodha"
    @classmethod
    def display_name(cls): return "Zerodha"
    @classmethod
    def required_fields(cls):
        return [
            {"key":"client_id","label":"User ID","hint":"Zerodha login ID e.g. ZX1234","optional":False},
            {"key":"api_key","label":"API Key","hint":"Kite Connect dashboard se","optional":False},
            {"key":"api_secret","label":"Access Token","hint":"Daily login ke baad milta hai","optional":False}
        ]
    @classmethod
    def setup_guide(cls):
        return {
            "broker":"Zerodha","api_cost":"Rs 2000/month","free":False,
            "api_portal_url":"https://kite.trade",
            "steps":[
                {"step":1,"title":"Zerodha Account","description":"zerodha.com pe account hona chahiye. F&O trading permission zaroori hai.","url":"https://zerodha.com"},
                {"step":2,"title":"Kite Connect Subscribe","description":"developers.kite.trade pe Rs 2000/month ka plan subscribe karein.","url":"https://developers.kite.trade"},
                {"step":3,"title":"App Banayein","description":"Create a new app karein. Redirect URL http://127.0.0.1 daalo.","url":"https://developers.kite.trade/apps"},
                {"step":4,"title":"API Key & Secret","description":"App create hone ke baad API Key aur API Secret copy karein.","url":None},
                {"step":5,"title":"Daily Login","description":"Zerodha mein har roz subah ek baar manually login karna padta hai access token ke liye.","url":None}
            ]
        }

    def login(self):
        try:
            if KiteConnect is None:
                return {"success":False,"message":"Run: pip install kiteconnect"}
            self.kite = KiteConnect(api_key=self.api_key)
            self.kite.set_access_token(self.api_secret)
            self.is_logged_in = True
            return {"success":True,"message":"Zerodha session set","token":self.api_secret}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def logout(self):
        try:
            if self.kite: self.kite.invalidate_access_token()
            self.is_logged_in = False
            return {"success":True}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_ltp(self, symbol, exchange="NFO"):
        try:
            data = self.kite.ltp([f"{exchange}:{symbol}"])
            return {"success":True,"ltp":data[f"{exchange}:{symbol}"]["last_price"],"symbol":symbol}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_candles(self, symbol, interval, from_date, to_date, exchange="NFO"):
        try:
            data = self.kite.historical_data(instrument_token=symbol, from_date=from_date, to_date=to_date, interval=interval)
            return {"success":True,"candles":data}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def search_option(self, underlying, expiry, strike, option_type):
        return {"success":True,"symbol":f"{underlying}{expiry}{int(strike)}{option_type}","token":""}

    def place_order(self, symbol, token, transaction_type, quantity, order_type="MARKET", price=0, exchange="NFO"):
        try:
            oid = self.kite.place_order(variety=self.kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=symbol, transaction_type=transaction_type, quantity=quantity, product=self.kite.PRODUCT_MIS, order_type=self.kite.ORDER_TYPE_MARKET if order_type=="MARKET" else self.kite.ORDER_TYPE_LIMIT, price=price if order_type=="LIMIT" else None)
            return {"success":True,"order_id":str(oid),"message":f"Zerodha order: {oid}"}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_order_status(self, order_id):
        try:
            for o in self.kite.orders():
                if str(o["order_id"]) == order_id:
                    return {"success":True,"status":o["status"],"filled_qty":o.get("filled_quantity",0),"avg_price":o.get("average_price",0)}
            return {"success":False,"message":"Not found"}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_positions(self):
        try:
            return {"success":True,"positions":self.kite.positions().get("net",[])}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def close_position(self, symbol, token, quantity, exchange="NFO"):
        return self.place_order(symbol, token, "SELL", quantity)

    def get_funds(self):
        try:
            eq = self.kite.margins().get("equity",{})
            return {"success":True,"available_cash":eq.get("available",{}).get("cash",0),"used_margin":eq.get("utilised",{}).get("debits",0)}
        except Exception as e:
            return {"success":False,"message":str(e)}

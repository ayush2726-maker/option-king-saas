import pyotp
from .base import BaseBroker
try:
    from SmartApi import SmartConnect
except ImportError:
    SmartConnect = None

class AngelOneBroker(BaseBroker):
    def __init__(self, client_id, api_key, api_secret, totp_secret=None):
        super().__init__(client_id, api_key, api_secret, totp_secret)
        self.smart_api = None

    @classmethod
    def broker_name(cls): return "angelone"
    @classmethod
    def display_name(cls): return "Angel One"
    @classmethod
    def required_fields(cls):
        return [
            {"key":"client_id","label":"Client ID","hint":"e.g. A1234567","optional":False},
            {"key":"api_key","label":"API Key","hint":"SmartAPI dashboard se","optional":False},
            {"key":"api_secret","label":"Trading Password","hint":"Angel One login password","optional":False},
            {"key":"totp_secret","label":"TOTP Secret Key","hint":"Authenticator app ka secret key","optional":False}
        ]
    @classmethod
    def setup_guide(cls):
        return {
            "broker":"Angel One","api_cost":"Free","free":True,
            "api_portal_url":"https://smartapi.angelbroking.com",
            "steps":[
                {"step":1,"title":"Angel One Account","description":"angelone.in pe account kholein. F&O enable karwayein.","url":"https://www.angelone.in"},
                {"step":2,"title":"SmartAPI Register","description":"smartapi.angelbroking.com pe jaayein aur Sign Up karein.","url":"https://smartapi.angelbroking.com"},
                {"step":3,"title":"App Banayein","description":"Create New App click karein. App type Personal select karein.","url":"https://smartapi.angelbroking.com/apps"},
                {"step":4,"title":"API Key Copy Karein","description":"App create hone ke baad API Key copy karein.","url":None},
                {"step":5,"title":"TOTP Setup","description":"Angel One app → Profile → Security → Enable TOTP → Enter key manually.","url":None},
                {"step":6,"title":"F&O Permission","description":"Profile → Segments → F&O enabled check karein.","url":None}
            ]
        }

    def login(self):
        try:
            if SmartConnect is None:
                return {"success":False,"message":"Run: pip install smartapi-python"}
            self.smart_api = SmartConnect(api_key=self.api_key)
            totp = pyotp.TOTP(self.totp_secret).now() if self.totp_secret else ""
            data = self.smart_api.generateSession(self.client_id, self.api_secret, totp)
            if data.get("status"):
                self.access_token = data["data"]["jwtToken"]
                self.is_logged_in = True
                return {"success":True,"message":"Angel One login successful","token":self.access_token}
            return {"success":False,"message":data.get("message","Login failed")}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def logout(self):
        try:
            if self.smart_api: self.smart_api.terminateSession(self.client_id)
            self.is_logged_in = False
            return {"success":True}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_ltp(self, symbol, exchange="NFO"):
        try:
            data = self.smart_api.ltpData(exchange, symbol, "")
            return {"success":True,"ltp":data["data"]["ltp"],"symbol":symbol}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_candles(self, symbol, interval, from_date, to_date, exchange="NFO"):
        try:
            m = {"1m":"ONE_MINUTE","5m":"FIVE_MINUTE","15m":"FIFTEEN_MINUTE","1h":"ONE_HOUR","1d":"ONE_DAY"}
            data = self.smart_api.getCandleData({"exchange":exchange,"symboltoken":symbol,"interval":m.get(interval,"FIVE_MINUTE"),"fromdate":from_date,"todate":to_date})
            return {"success":True,"candles":data.get("data",[])}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def search_option(self, underlying, expiry, strike, option_type):
        return {"success":True,"symbol":f"{underlying}{expiry}{int(strike)}{option_type}","token":""}

    def place_order(self, symbol, token, transaction_type, quantity, order_type="MARKET", price=0, exchange="NFO"):
        try:
            data = self.smart_api.placeOrder({"variety":"NORMAL","tradingsymbol":symbol,"symboltoken":token,"transactiontype":transaction_type,"exchange":exchange,"ordertype":order_type,"producttype":"INTRADAY","duration":"DAY","price":str(price),"squareoff":"0","stoploss":"0","quantity":str(quantity)})
            oid = data.get("data",{}).get("orderid","")
            return {"success":True,"order_id":oid,"message":f"Order: {oid}"}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_order_status(self, order_id):
        try:
            for o in self.smart_api.orderBook().get("data",[]):
                if o["orderid"] == order_id:
                    return {"success":True,"status":o["status"],"filled_qty":o.get("filledshares",0),"avg_price":o.get("averageprice",0)}
            return {"success":False,"message":"Not found"}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def get_positions(self):
        try:
            return {"success":True,"positions":self.smart_api.position().get("data",[])}
        except Exception as e:
            return {"success":False,"message":str(e)}

    def close_position(self, symbol, token, quantity, exchange="NFO"):
        return self.place_order(symbol, token, "SELL", quantity, "MARKET", 0, exchange)

    def get_funds(self):
        try:
            f = self.smart_api.rmsLimit().get("data",{})
            return {"success":True,"available_cash":float(f.get("availablecash",0)),"used_margin":float(f.get("utiliseddebits",0))}
        except Exception as e:
            return {"success":False,"message":str(e)}

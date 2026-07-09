from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from auth.utils import decrypt_credential
from bot.angel_fetcher import get_index_quotes
from bot.brokers.factory import create_broker

router = APIRouter()

INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"]

ZERODHA_INDEX_TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105, "SENSEX": 265}
ZERODHA_INDEX_EXCHANGE = {"NIFTY": "NSE", "BANKNIFTY": "NSE", "SENSEX": "BSE"}
UPSTOX_INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank", "SENSEX": "BSE_INDEX|SENSEX"}


def _get_active_broker(user_id):
    conn = get_db()
    cred = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    conn.close()
    if not cred:
        return None, None
    creds = {
        "client_id": cred["client_id"],
        "api_key": decrypt_credential(cred["api_key"]),
        "password": decrypt_credential(cred["api_secret"]),
        "totp_secret": decrypt_credential(cred["totp_secret"]) if cred["totp_secret"] else None,
    }
    return cred["broker_name"], creds


def _get_quotes_multi(broker_name, creds):
    """LTP fetch for Zerodha/Upstox (non-Angel brokers)."""
    obj = create_broker(broker_name, creds["client_id"], creds["api_key"], creds["password"], creds.get("totp_secret"))
    login_result = obj.login()
    if not login_result.get("success"):
        raise RuntimeError(login_result.get("message", "Login failed"))

    results = {}
    for symbol in INDEX_SYMBOLS:
        try:
            if broker_name == "zerodha":
                token = ZERODHA_INDEX_TOKENS[symbol]
                exch = ZERODHA_INDEX_EXCHANGE[symbol]
                r = obj.get_ltp(symbol=token, exchange=exch)
            elif broker_name == "upstox":
                key = UPSTOX_INDEX_KEYS[symbol]
                r = obj.get_ltp(symbol=key)
            else:
                r = {"success": False}

            if r.get("success"):
                results[symbol] = {"ltp": r["ltp"], "status": "connected"}
            else:
                results[symbol] = {"ltp": None, "status": "not_connected", "error": r.get("message")}
        except Exception as e:
            results[symbol] = {"ltp": None, "status": "not_connected", "error": str(e)}
    return results


@router.get("/market/status")
def market_status(authorization: str = Header(None)):
    """Returns live market feed status using the user's active broker."""
    user = get_current_user(authorization)

    feed_connected = False
    source = "not_connected"
    indices_data = []
    quotes = {}

    broker_name, creds = _get_active_broker(user["id"])
    if creds:
        try:
            if broker_name == "angelone":
                quotes = get_index_quotes(user["id"], creds)
            else:
                quotes = _get_quotes_multi(broker_name, creds)
            source = broker_name
        except Exception:
            quotes = {}

    for symbol in INDEX_SYMBOLS:
        q = quotes.get(symbol, {})
        status = q.get("status", "not_connected")
        if status == "connected":
            feed_connected = True
        indices_data.append({
            "symbol": symbol,
            "ltp": q.get("ltp"),
            "change": None,
            "change_percent": None,
            "status": status
        })

    message = "Live feed connected" if feed_connected else "Live feed not connected"

    return {
        "success": True,
        "feed_connected": feed_connected,
        "source": source,
        "message": message,
        "indices": indices_data
    }

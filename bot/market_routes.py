from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
from auth.utils import decrypt_credential
from bot.angel_fetcher import get_index_quotes

router = APIRouter()

INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"]


def _get_angelone_creds(user_id):
    conn = get_db()
    cred = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND broker_name='angelone'",
        (user_id,)
    ).fetchone()
    conn.close()
    if not cred:
        return None
    return {
        "client_id": cred["client_id"],
        "api_key": decrypt_credential(cred["api_key"]),
        "password": decrypt_credential(cred["api_secret"]),
        "totp_secret": decrypt_credential(cred["totp_secret"]) if cred["totp_secret"] else None,
    }


@router.get("/market/status")
def market_status(authorization: str = Header(None)):
    """Returns live market feed status using real Angel One LTP."""
    user = get_current_user(authorization)

    feed_connected = False
    source = "not_connected"
    indices_data = []
    quotes = {}

    creds = _get_angelone_creds(user["id"])
    if creds:
        try:
            quotes = get_index_quotes(user["id"], creds)
            source = "broker"
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

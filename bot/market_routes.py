from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user

router = APIRouter()

INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "SENSEX"]


@router.get("/market/status")
def market_status(authorization: str = Header(None)):
    """
    Returns live market feed status.
    NEVER fake prices. If no broker LTP function is wired up,
    every index returns null values with status "not_connected".
    """
    user = get_current_user(authorization)

    feed_connected = False
    source = "not_connected"
    indices_data = []

    for symbol in INDEX_SYMBOLS:
        ltp = None
        change = None
        change_percent = None
        status = "not_connected"

        try:
            # NOTE: broker/routes.py currently has no LTP/quote function.
            # When one is added (e.g. get_ltp(user_id, symbol)), wire it
            # in here inside this try block. Until then, stay safely
            # not_connected rather than fabricate a price.
            #
            # from broker.routes import get_ltp
            # quote = get_ltp(user["id"], symbol)
            # if quote:
            #     ltp = quote["ltp"]
            #     change = quote["change"]
            #     change_percent = quote["change_percent"]
            #     status = "connected"
            #     feed_connected = True
            #     source = "broker"
            pass
        except Exception:
            ltp = None
            change = None
            change_percent = None
            status = "not_connected"

        indices_data.append({
            "symbol": symbol,
            "ltp": ltp,
            "change": change,
            "change_percent": change_percent,
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

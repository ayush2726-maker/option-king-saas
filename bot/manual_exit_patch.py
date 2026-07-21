"""Authenticated manual exit endpoint for the currently open trade.

The endpoint closes PAPER positions at the freshest option LTP available.  For
LIVE positions it submits a broker MARKET SELL and marks the database closed
only after the broker confirms a fill.  Repeated taps are guarded so a pending
live exit is not submitted twice.
"""

from datetime import datetime, timezone

from fastapi import Header

from auth.routes import get_current_user
from auth.utils import decrypt_credential
from database import get_db
from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime
from bot import routes


def _credential_payload(row):
    return {
        "client_id": row["client_id"],
        "api_key": decrypt_credential(row["api_key"]),
        "password": decrypt_credential(row["api_secret"]),
        "totp_secret": (
            decrypt_credential(row["totp_secret"])
            if row["totp_secret"]
            else None
        ),
    }


def _broker_session(broker_row):
    broker_name = str(broker_row["broker_name"] or "angelone").lower()
    creds = _credential_payload(broker_row)

    if broker_name == "angelone":
        obj = angel_fetcher.angel_login(creds)
        return broker_name, obj

    obj = angel_fetcher.create_broker(
        broker_name,
        creds["client_id"],
        creds["api_key"],
        creds["password"],
        creds.get("totp_secret"),
    )
    result = obj.login()
    if not result.get("success"):
        raise RuntimeError(result.get("message") or "Broker login failed")
    return broker_name, obj


def _quote_and_order_functions(broker_name, obj):
    if broker_name == "angelone":
        return (
            lambda trade: runtime._ltp_angel(obj, trade),
            lambda resolved, action, qty, fallback: runtime._place_angel(
                obj, resolved, action, qty, fallback
            ),
        )

    return (
        lambda trade: runtime._ltp_multi(broker_name, obj, trade),
        lambda resolved, action, qty, fallback: runtime._place_multi(
            obj, resolved, action, qty, fallback
        ),
    )


def _find_open_trade(conn, user_id, trade_id=0):
    if trade_id:
        return conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE id=? AND user_id=? AND status='OPEN'
            LIMIT 1
            """,
            (trade_id, user_id),
        ).fetchone()

    return conn.execute(
        """
        SELECT * FROM paper_trades
        WHERE user_id=? AND status='OPEN'
        ORDER BY id DESC LIMIT 1
        """,
        (user_id,),
    ).fetchone()


def _manual_exit(body: dict = None, authorization: str = Header(None)):
    user = get_current_user(authorization)
    body = body or {}

    try:
        requested_id = int(body.get("trade_id") or 0)
    except Exception:
        requested_id = 0

    conn = get_db()
    try:
        runtime._ensure_schema(conn)
        trade = _find_open_trade(conn, user["id"], requested_id)
        if not trade:
            return {
                "success": False,
                "message": "Koi open trade nahi mila.",
            }

        mode = runtime._mode(trade)
        live_status = str(runtime._v(trade, "live_order_status", "") or "").upper()
        if mode == "live" and live_status in {
            "MANUAL_EXIT_SUBMITTING",
            "EXIT_PENDING",
            "MANUAL_EXIT_PENDING",
        }:
            return {
                "success": False,
                "message": "Exit order already pending hai. Dobara submit nahi kiya.",
                "trade_id": trade["id"],
            }

        broker = conn.execute(
            """
            SELECT * FROM broker_credentials
            WHERE user_id=? AND is_active=1
            ORDER BY last_connected DESC LIMIT 1
            """,
            (user["id"],),
        ).fetchone()

        quote = {"success": False, "message": "Broker unavailable"}
        live_order = None
        if broker:
            try:
                broker_name, obj = _broker_session(broker)
                quote_fetcher, live_order = _quote_and_order_functions(
                    broker_name, obj
                )
                quote = quote_fetcher(trade) or quote
            except Exception as exc:
                quote = {"success": False, "message": str(exc)}

        current_ltp = runtime._f(quote.get("ltp"), 0) if quote.get("success") else 0
        used_cached_ltp = False
        if current_ltp <= 0:
            current_ltp = runtime._f(runtime._v(trade, "last_ltp", 0), 0)
            used_cached_ltp = current_ltp > 0

        if current_ltp <= 0:
            return {
                "success": False,
                "message": "Current option LTP nahi mila; galat price par close nahi kiya.",
                "detail": str(quote.get("message") or "")[:180],
                "trade_id": trade["id"],
            }

        reason = "MANUAL EXIT BY USER"
        if used_cached_ltp:
            reason += " | LAST_MONITORED_LTP"

        if mode == "paper":
            runtime._close(
                conn,
                user["id"],
                trade,
                current_ltp,
                reason,
            )
            pnl = round(
                (current_ltp - runtime._f(trade["entry_price"]))
                * max(1, runtime._i(trade["qty"], 1)),
                2,
            )
            return {
                "success": True,
                "message": "Paper trade manually exit ho gaya.",
                "trade_id": trade["id"],
                "symbol": trade["symbol"],
                "exit_price": round(current_ltp, 2),
                "pnl": pnl,
                "mode": "paper",
            }

        if not broker or live_order is None:
            return {
                "success": False,
                "message": "LIVE exit ke liye broker session available nahi hai.",
                "trade_id": trade["id"],
            }

        conn.execute(
            """
            UPDATE paper_trades
            SET live_order_status='MANUAL_EXIT_SUBMITTING'
            WHERE id=? AND user_id=? AND status='OPEN'
            """,
            (trade["id"], user["id"]),
        )
        conn.commit()

        resolved = {
            "symbol": trade["symbol"],
            "token": runtime._v(trade, "token", ""),
            "exchange": runtime._v(trade, "exch_seg", "NFO"),
            "exch_seg": runtime._v(trade, "exch_seg", "NFO"),
        }
        order = live_order(
            resolved,
            "SELL",
            max(1, runtime._i(trade["qty"], 1)),
            current_ltp,
        ) or {}

        if order.get("success"):
            exit_price = runtime._f(order.get("avg_price"), current_ltp)
            runtime._close(
                conn,
                user["id"],
                trade,
                exit_price,
                reason,
                order.get("order_id"),
            )
            runtime._record_event(
                conn,
                user["id"],
                trade["id"],
                order.get("order_id"),
                "SELL",
                "FILLED",
                reason,
            )
            pnl = round(
                (exit_price - runtime._f(trade["entry_price"]))
                * max(1, runtime._i(trade["qty"], 1)),
                2,
            )
            return {
                "success": True,
                "message": "LIVE trade manually exit ho gaya.",
                "trade_id": trade["id"],
                "symbol": trade["symbol"],
                "exit_price": round(exit_price, 2),
                "pnl": pnl,
                "mode": "live",
                "order_id": order.get("order_id"),
            }

        pending = bool(order.get("pending"))
        status = "MANUAL_EXIT_PENDING" if pending else "MANUAL_EXIT_FAILED"
        conn.execute(
            """
            UPDATE paper_trades
            SET exit_order_id=?, live_order_status=?
            WHERE id=?
            """,
            (order.get("order_id"), status, trade["id"]),
        )
        runtime._record_event(
            conn,
            user["id"],
            trade["id"],
            order.get("order_id"),
            "SELL",
            "PENDING" if pending else "FAILED",
            order.get("message") or "Manual exit order failed",
        )
        conn.commit()
        return {
            "success": False,
            "message": (
                "Exit order broker par pending hai; dobara button mat dabana."
                if pending
                else "Broker ne manual exit confirm nahi kiya; trade OPEN rakha hai."
            ),
            "detail": str(order.get("message") or "")[:180],
            "trade_id": trade["id"],
            "order_id": order.get("order_id"),
            "pending": pending,
        }
    finally:
        conn.close()


def apply_manual_exit_patch():
    if getattr(routes, "_okai_manual_exit_v1", False):
        return

    routes.router.add_api_route(
        "/manual-exit",
        _manual_exit,
        methods=["POST"],
        name="manual_exit_open_trade",
    )
    routes._okai_manual_exit_v1 = True

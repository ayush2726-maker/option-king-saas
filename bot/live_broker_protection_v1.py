"""Broker-side protection for LIVE trades only.

Goals:
- Keep PAPER strategy/entry/exit/risk logic untouched.
- Every LIVE filled long-option position gets a real broker STOPLOSS order.
- The broker SL is only tightened (never loosened) to the same ``sl_price`` that
  the shared PAPER/LIVE runtime computes from ATR/profit-lock/reversal rules.
- If Railway/app quotes go stale, the last successfully synced broker stop stays
  active at the broker/exchange instead of disappearing with the app process.

Notes:
- Upstox native GTT TSL requires the ENTRY leg to be created as part of the GTT.
  The existing runtime already confirms a normal market fill first, so this
  module deliberately avoids placing a second GTT entry (which could duplicate
  the position). It therefore uses a real STOPLOSS_MARKET exit order and
  tightens that broker order whenever the shared runtime tightens ``sl_price``.
- Angel One SmartAPI supports STOPLOSS orders (and ROBO trailing when the entry
  itself is ROBO). For parity with the current normal filled-entry flow, this
  module likewise places a STOPLOSS_MARKET exit and modifies it tighter.

This gives broker-resident downside protection without changing any signal,
score, quantity, cutoff, expiry, AI, profit-lock or reversal decision.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import requests

from auth.utils import decrypt_credential
from database import get_db

VERSION = "LIVE_BROKER_PROTECTION_V1"
_INTERVAL_SECONDS = 4
_started = False
_start_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def _ensure_schema(conn):
    for name, kind in [
        ("broker_sl_order_id", "TEXT"),
        ("broker_sl_price", "REAL"),
        ("broker_protection_status", "TEXT"),
        ("broker_protection_error", "TEXT"),
        ("broker_protection_updated_at", "TEXT"),
        ("broker_protection_version", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}")
        except Exception:
            pass
    conn.commit()


def _credential_row(conn, user_id):
    try:
        return conn.execute(
            """
            SELECT * FROM broker_credentials
            WHERE user_id=? AND is_active=1
            ORDER BY last_connected DESC LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
    except Exception:
        return None


def _session(conn, user_id):
    row = _credential_row(conn, user_id)
    if not row:
        raise RuntimeError("ACTIVE_BROKER_CREDENTIALS_MISSING")
    broker = str(row["broker_name"] or "").lower().strip()
    from bot.brokers.factory import create_broker
    obj = create_broker(
        broker,
        row["client_id"],
        decrypt_credential(row["api_key"]),
        decrypt_credential(row["api_secret"]),
        decrypt_credential(row["totp_secret"]) if row["totp_secret"] else None,
    )
    result = obj.login()
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError((result or {}).get("message") or "BROKER_LOGIN_FAILED")
    return broker, obj


def _upstox_token(trade):
    token = str(trade["token"] or "").strip()
    if "|" in token:
        return token
    exchange = str(trade["exch_seg"] or "NSE_FO").upper()
    segment = "BSE_FO" if exchange.startswith(("BSE", "BFO")) else "NSE_FO"
    return f"{segment}|{trade['symbol']}"


def _upstox_place_sl(obj, trade, trigger):
    token = _upstox_token(trade)
    payload = {
        "quantity": _i(trade["qty"], 0),
        "product": "I",
        "validity": "DAY",
        "price": 0,
        "instrument_token": token,
        "order_type": "SL-M",
        "transaction_type": "SELL",
        "disclosed_quantity": 0,
        "trigger_price": round(float(trigger), 2),
        "is_amo": False,
    }
    # Use the same current order endpoint family as the broker adapter. If a
    # broker-side API revision rejects SL-M here, record the error and leave the
    # app exit logic active rather than silently pretending protection exists.
    response = requests.post(
        f"{obj.BASE_URL}/order/place",
        headers=obj._h(),
        json=payload,
        timeout=10,
    )
    data = response.json()
    if response.status_code >= 300 or data.get("status") != "success":
        raise RuntimeError(f"UPSTOX_SL_PLACE:{response.status_code}:{str(data)[:300]}")
    order_id = str((data.get("data") or {}).get("order_id") or "")
    if not order_id:
        raise RuntimeError("UPSTOX_SL_ORDER_ID_MISSING")
    return order_id


def _upstox_modify_sl(obj, trade, order_id, trigger):
    payload = {
        "quantity": _i(trade["qty"], 0),
        "validity": "DAY",
        "price": 0,
        "order_type": "SL-M",
        "trigger_price": round(float(trigger), 2),
        "order_id": str(order_id),
        "disclosed_quantity": 0,
    }
    response = requests.put(
        f"{obj.BASE_URL}/order/modify",
        headers=obj._h(),
        json=payload,
        timeout=10,
    )
    data = response.json()
    if response.status_code >= 300 or data.get("status") != "success":
        raise RuntimeError(f"UPSTOX_SL_MODIFY:{response.status_code}:{str(data)[:300]}")
    return True


def _upstox_cancel(obj, order_id):
    response = requests.delete(
        f"{obj.BASE_URL}/order/cancel",
        headers=obj._h(),
        params={"order_id": str(order_id)},
        timeout=10,
    )
    data = response.json()
    if response.status_code >= 300 or data.get("status") != "success":
        raise RuntimeError(f"UPSTOX_SL_CANCEL:{response.status_code}:{str(data)[:250]}")
    return True


def _angel_place_sl(obj, trade, trigger):
    data = obj.smart_api.placeOrder({
        "variety": "STOPLOSS",
        "tradingsymbol": str(trade["symbol"]),
        "symboltoken": str(trade["token"] or ""),
        "transactiontype": "SELL",
        "exchange": str(trade["exch_seg"] or "NFO"),
        "ordertype": "STOPLOSS_MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "triggerprice": str(round(float(trigger), 2)),
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(_i(trade["qty"], 0)),
    })
    if isinstance(data, dict):
        order_id = str((data.get("data") or {}).get("orderid") or data.get("orderid") or "")
        ok = data.get("status", True)
    else:
        order_id = str(data or "")
        ok = bool(order_id)
    if not ok or not order_id:
        raise RuntimeError(f"ANGEL_SL_PLACE:{str(data)[:300]}")
    return order_id


def _angel_modify_sl(obj, trade, order_id, trigger):
    result = obj.smart_api.modifyOrder({
        "variety": "STOPLOSS",
        "orderid": str(order_id),
        "ordertype": "STOPLOSS_MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "triggerprice": str(round(float(trigger), 2)),
        "quantity": str(_i(trade["qty"], 0)),
        "tradingsymbol": str(trade["symbol"]),
        "symboltoken": str(trade["token"] or ""),
        "exchange": str(trade["exch_seg"] or "NFO"),
    })
    if isinstance(result, dict) and result.get("status") is False:
        raise RuntimeError(f"ANGEL_SL_MODIFY:{str(result)[:300]}")
    return True


def _angel_cancel(obj, order_id):
    result = obj.smart_api.cancelOrder(str(order_id), "STOPLOSS")
    if isinstance(result, dict) and result.get("status") is False:
        raise RuntimeError(f"ANGEL_SL_CANCEL:{str(result)[:250]}")
    return True


def _place(broker, obj, trade, trigger):
    if broker == "upstox":
        return _upstox_place_sl(obj, trade, trigger)
    if broker == "angelone":
        return _angel_place_sl(obj, trade, trigger)
    raise RuntimeError(f"BROKER_PROTECTION_UNSUPPORTED:{broker}")


def _modify(broker, obj, trade, order_id, trigger):
    if broker == "upstox":
        return _upstox_modify_sl(obj, trade, order_id, trigger)
    if broker == "angelone":
        return _angel_modify_sl(obj, trade, order_id, trigger)
    raise RuntimeError(f"BROKER_PROTECTION_UNSUPPORTED:{broker}")


def _cancel(broker, obj, order_id):
    if broker == "upstox":
        return _upstox_cancel(obj, order_id)
    if broker == "angelone":
        return _angel_cancel(obj, order_id)
    return False


def _save_error(conn, trade_id, message):
    conn.execute(
        """
        UPDATE paper_trades
        SET broker_protection_status='ERROR',
            broker_protection_error=?,
            broker_protection_updated_at=?,
            broker_protection_version=?
        WHERE id=?
        """,
        (str(message)[:500], _now(), VERSION, int(trade_id)),
    )
    conn.commit()


def _protect_open_rows(user_id, rows):
    conn = get_db()
    try:
        broker, obj = _session(conn, user_id)
        for snapshot in rows:
            trade = conn.execute(
                "SELECT * FROM paper_trades WHERE id=? AND status='OPEN' AND COALESCE(trading_mode,'paper')='live'",
                (snapshot["id"],),
            ).fetchone()
            if not trade:
                continue
            desired = _f(trade["sl_price"], 0)
            entry = _f(trade["entry_price"], 0)
            if desired <= 0 or entry <= 0 or desired >= entry:
                _save_error(conn, trade["id"], f"INVALID_LIVE_SL:{desired}/{entry}")
                continue
            order_id = str(trade["broker_sl_order_id"] or "").strip()
            broker_sl = _f(trade["broker_sl_price"], 0)
            try:
                if not order_id:
                    order_id = _place(broker, obj, trade, desired)
                    conn.execute(
                        """
                        UPDATE paper_trades
                        SET broker_sl_order_id=?, broker_sl_price=?,
                            broker_protection_status='ACTIVE',
                            broker_protection_error=NULL,
                            broker_protection_updated_at=?,
                            broker_protection_version=?
                        WHERE id=? AND status='OPEN'
                        """,
                        (order_id, desired, _now(), VERSION, trade["id"]),
                    )
                    conn.commit()
                elif desired > broker_sl + 0.009:
                    _modify(broker, obj, trade, order_id, desired)
                    conn.execute(
                        """
                        UPDATE paper_trades
                        SET broker_sl_price=?, broker_protection_status='ACTIVE',
                            broker_protection_error=NULL,
                            broker_protection_updated_at=?,
                            broker_protection_version=?
                        WHERE id=? AND status='OPEN'
                        """,
                        (desired, _now(), VERSION, trade["id"]),
                    )
                    conn.commit()
            except Exception as exc:
                _save_error(conn, trade["id"], f"{type(exc).__name__}:{exc}")
    finally:
        conn.close()


def _cleanup_closed_rows(user_id, rows):
    conn = get_db()
    try:
        broker, obj = _session(conn, user_id)
        for trade in rows:
            order_id = str(trade["broker_sl_order_id"] or "").strip()
            if not order_id:
                continue
            try:
                _cancel(broker, obj, order_id)
                status = "CANCELLED_AFTER_EXIT"
                error = None
            except Exception as exc:
                # If the stop itself filled, cancellation may legitimately say
                # completed/not found. Mark cleanup attempted; do not re-open or
                # create any new broker order for a closed trade.
                status = "CLEANUP_ATTEMPTED"
                error = f"{type(exc).__name__}:{exc}"[:500]
            conn.execute(
                """
                UPDATE paper_trades
                SET broker_protection_status=?, broker_protection_error=?,
                    broker_protection_updated_at=?, broker_protection_version=?
                WHERE id=?
                """,
                (status, error, _now(), VERSION, trade["id"]),
            )
            conn.commit()
    finally:
        conn.close()


def _loop():
    while True:
        try:
            conn = get_db()
            try:
                _ensure_schema(conn)
                open_rows = conn.execute(
                    """
                    SELECT * FROM paper_trades
                    WHERE status='OPEN' AND COALESCE(trading_mode,'paper')='live'
                    """
                ).fetchall()
                closed_rows = conn.execute(
                    """
                    SELECT * FROM paper_trades
                    WHERE status='CLOSED'
                      AND COALESCE(trading_mode,'paper')='live'
                      AND COALESCE(broker_sl_order_id,'')<>''
                      AND COALESCE(broker_protection_status,'')='ACTIVE'
                    """
                ).fetchall()
            finally:
                conn.close()

            grouped_open = {}
            for row in open_rows:
                grouped_open.setdefault(int(row["user_id"]), []).append(row)
            for uid, rows in grouped_open.items():
                try:
                    _protect_open_rows(uid, rows)
                except Exception:
                    pass

            grouped_closed = {}
            for row in closed_rows:
                grouped_closed.setdefault(int(row["user_id"]), []).append(row)
            for uid, rows in grouped_closed.items():
                try:
                    _cleanup_closed_rows(uid, rows)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(_INTERVAL_SECONDS)


def schedule_live_broker_protection():
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        threading.Thread(
            target=_loop,
            name="okai-live-broker-protection",
            daemon=True,
        ).start()
        _started = True

"""Runtime Telegram trade-alert bridge for OKAI.

This keeps Telegram alerting separate from strategy logic. It only observes trade
state changes and sends per-user Telegram notifications when that user has linked
Telegram from the app.
"""

from datetime import datetime, timedelta, timezone
from html import escape
import json

from database import get_db
from telegram.routes import notify_trade_alert

_PATCHED = False


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _num(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _money(value):
    if value is None or value == "":
        return "--"
    return f"₹{_num(value):.2f}"


def _int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _text(value, limit=180):
    return escape(str(value or "")[:limit])


def _format_time(value):
    raw = str(value or "").strip()
    if not raw:
        return "--"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        ist = parsed.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return ist.strftime("%d %b %H:%M IST")
    except Exception:
        return escape(raw[:32])


def _safe_json(value):
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _ensure_alert_log(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_alert_log (
            user_id INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, event_key)
        )
        """
    )
    conn.execute(
        """
        DELETE FROM telegram_alert_log
        WHERE datetime(created_at) < datetime('now', '-10 days')
        """
    )
    conn.commit()


def _send_once(user_id, event_key, message):
    if not user_id or not event_key or not message:
        return {"success": False, "message": "invalid alert"}
    conn = get_db()
    try:
        _ensure_alert_log(conn)
        try:
            conn.execute(
                "INSERT INTO telegram_alert_log(user_id, event_key, created_at) VALUES (?, ?, ?)",
                (int(user_id), str(event_key), _now_iso()),
            )
            conn.commit()
        except Exception:
            return {"success": False, "message": "duplicate alert"}
    finally:
        conn.close()

    return notify_trade_alert(int(user_id), message)


def _fetch_paper_latest(user_id):
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE user_id=?
            ORDER BY id DESC LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        open_row = conn.execute(
            """
            SELECT * FROM paper_trades
            WHERE user_id=? AND status='OPEN'
            ORDER BY id DESC LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        return row, open_row
    except Exception:
        return None, None
    finally:
        conn.close()


def _fetch_paper_trade(user_id, trade_id):
    if not trade_id:
        return None
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM paper_trades WHERE user_id=? AND id=? LIMIT 1",
            (int(user_id), int(trade_id)),
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()


def _paper_entry_message(row):
    return "\n".join([
        "🚀 <b>OKAI Paper Trade Entry</b>",
        f"Symbol: <b>{_text(row['symbol'])}</b>",
        f"Side: {_text(row['side'])}",
        f"Entry: {_money(row['entry_price'])}",
        f"Qty: {_int(row['qty'])}",
        f"SL: {_money(row['sl_price'] if 'sl_price' in row.keys() else None)}",
        f"Target: {_money(row['target_price'] if 'target_price' in row.keys() else None)}",
        f"Reason: {_text(row['reason'])}",
        f"Time: {_format_time(row['created_at'])}",
    ])


def _paper_exit_message(row):
    pnl = _num(row['pnl'])
    icon = "✅" if pnl >= 0 else "⚠️"
    return "\n".join([
        f"{icon} <b>OKAI Paper Trade Exit</b>",
        f"Symbol: <b>{_text(row['symbol'])}</b>",
        f"Side: {_text(row['side'])}",
        f"Entry: {_money(row['entry_price'])}",
        f"Exit: {_money(row['exit_price'])}",
        f"Qty: {_int(row['qty'])}",
        f"P&amp;L: <b>{_money(pnl)}</b>",
        f"Reason: {_text(row['reason'])}",
        f"Time: {_format_time(row['created_at'])}",
    ])


def _command_info(user_id, command_id):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM local_order_commands WHERE user_id=? AND id=? LIMIT 1",
            (int(user_id), int(command_id)),
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()


def _live_trade(user_id, trade_id):
    if not trade_id:
        return None
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM trades WHERE user_id=? AND id=? LIMIT 1",
            (int(user_id), int(trade_id)),
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()


def _live_queue_message(payload, result):
    return "\n".join([
        "📨 <b>OKAI Live Entry Queued</b>",
        f"Symbol: <b>{_text(payload.get('symbol'))}</b>",
        f"Side: {_text(payload.get('option_type'))}",
        f"Expected Entry: {_money(payload.get('expected_entry_price'))}",
        f"Qty: {_int(payload.get('quantity'))}",
        f"SL: {_money(payload.get('sl_price'))}",
        f"Target: {_money(payload.get('target_price'))}",
        f"Score: {_int(payload.get('score'))}/{_int(payload.get('min_score'), 82)}",
        f"Trade ID: {_text(result.get('trade_id'))}",
    ])


def _live_entry_filled_message(trade, result):
    return "\n".join([
        "✅ <b>OKAI Live Entry Filled</b>",
        f"Symbol: <b>{_text(trade['symbol'] if trade else result.get('symbol'))}</b>",
        f"Side: {_text((trade['option_type'] if trade else result.get('option_type')) or (trade['side'] if trade else ''))}",
        f"Entry: {_money((trade['entry_price'] if trade else None) or result.get('entry_price'))}",
        f"Qty: {_int((trade['quantity'] if trade else None) or result.get('quantity'))}",
        f"SL: {_money((trade['sl_price'] if trade else None) or result.get('sl_price'))}",
        f"Target: {_money((trade['target_price'] if trade else None) or result.get('target_price'))}",
        f"Order ID: {_text(result.get('broker_order_id') or (trade['broker_order_id'] if trade else ''))}",
        f"Time: {_format_time(result.get('entry_time') or (trade['entry_time'] if trade else None))}",
    ])


def _live_entry_failed_message(command, result, error):
    payload = _safe_json(command['payload_json'] if command else "{}")
    reason = error or result.get('error') or "ENTRY FAILED"
    return "\n".join([
        "❌ <b>OKAI Live Entry Failed</b>",
        f"Symbol: <b>{_text(payload.get('symbol'))}</b>",
        f"Side: {_text(payload.get('option_type'))}",
        f"Qty: {_int(payload.get('quantity'))}",
        f"Reason: {_text(reason, 260)}",
    ])


def _live_exit_filled_message(trade, result):
    pnl = _num((trade['pnl'] if trade else None) or result.get('pnl'))
    icon = "✅" if pnl >= 0 else "⚠️"
    return "\n".join([
        f"{icon} <b>OKAI Live Exit Filled</b>",
        f"Symbol: <b>{_text(trade['symbol'] if trade else result.get('symbol'))}</b>",
        f"Entry: {_money(trade['entry_price'] if trade else None)}",
        f"Exit: {_money((trade['exit_price'] if trade else None) or result.get('exit_price'))}",
        f"Qty: {_int((trade['quantity'] if trade else None) or result.get('quantity'))}",
        f"P&amp;L: <b>{_money(pnl)}</b>",
        f"Reason: {_text(result.get('reason') or (trade['exit_reason'] if trade else 'EXIT FILLED'))}",
        f"Time: {_format_time(result.get('exit_time') or (trade['exit_time'] if trade else None))}",
    ])


def _live_exit_failed_message(command, result, error):
    payload = _safe_json(command['payload_json'] if command else "{}")
    reason = error or result.get('error') or "EXIT FAILED"
    return "\n".join([
        "❌ <b>OKAI Live Exit Failed</b>",
        f"Symbol: <b>{_text(payload.get('symbol'))}</b>",
        f"Qty: {_int(payload.get('quantity'))}",
        f"Reason: {_text(reason, 260)}",
    ])


def _patch_paper_manager():
    import bot.angel_fetcher as angel_fetcher

    original = getattr(angel_fetcher, "_manage_paper_trade", None)
    if not original or getattr(original, "_telegram_alerts_wrapped", False):
        return False

    def wrapped_manage_paper_trade(*args, **kwargs):
        user_id = kwargs.get("user_id") if "user_id" in kwargs else (args[0] if args else None)
        before_latest, before_open = _fetch_paper_latest(user_id) if user_id else (None, None)
        before_latest_id = int(before_latest["id"]) if before_latest else 0
        before_open_id = int(before_open["id"]) if before_open else 0

        result = original(*args, **kwargs)

        try:
            after_latest, after_open = _fetch_paper_latest(user_id)
            after_latest_id = int(after_latest["id"]) if after_latest else 0
            after_open_id = int(after_open["id"]) if after_open else 0

            if after_latest and after_latest_id > before_latest_id and str(after_latest["status"]).upper() == "OPEN":
                _send_once(
                    user_id,
                    f"paper_entry:{after_latest_id}",
                    _paper_entry_message(after_latest),
                )

            if before_open_id and before_open_id != after_open_id:
                closed = _fetch_paper_trade(user_id, before_open_id)
                if closed and str(closed["status"]).upper() != "OPEN":
                    _send_once(
                        user_id,
                        f"paper_exit:{before_open_id}",
                        _paper_exit_message(closed),
                    )
        except Exception as exc:
            print(f"Telegram paper alert warning: {str(exc)[:160]}")

        return result

    wrapped_manage_paper_trade._telegram_alerts_wrapped = True
    angel_fetcher._manage_paper_trade = wrapped_manage_paper_trade
    return True


def _patch_live_gateway():
    import local_gateway.service as service

    patched_any = False

    original_queue = getattr(service, "queue_live_entry", None)
    if original_queue and not getattr(original_queue, "_telegram_alerts_wrapped", False):
        def wrapped_queue_live_entry(user_id, payload, idempotency_key, max_concurrent=1, max_trades_per_day=None):
            result = original_queue(user_id, payload, idempotency_key, max_concurrent, max_trades_per_day)
            try:
                if isinstance(result, dict) and result.get("queued"):
                    _send_once(
                        user_id,
                        f"live_queue:{result.get('trade_id')}:{result.get('command_id')}",
                        _live_queue_message(dict(payload or {}), result),
                    )
            except Exception as exc:
                print(f"Telegram live queue alert warning: {str(exc)[:160]}")
            return result

        wrapped_queue_live_entry._telegram_alerts_wrapped = True
        service.queue_live_entry = wrapped_queue_live_entry
        try:
            import bot.angel_fetcher as angel_fetcher
            angel_fetcher.queue_live_entry = wrapped_queue_live_entry
        except Exception:
            pass
        patched_any = True

    original_complete = getattr(service, "complete_command", None)
    if original_complete and not getattr(original_complete, "_telegram_alerts_wrapped", False):
        def wrapped_complete_command(gateway, command_id, lease_token, success, result=None, error=""):
            result_payload = dict(result or {})
            user_id = int(gateway["user_id"])
            command = _command_info(user_id, command_id)
            response = original_complete(gateway, command_id, lease_token, success, result, error)
            try:
                action = str(command["action"] if command else "").upper()
                trade_id = command["trade_id"] if command else result_payload.get("trade_id")
                trade = _live_trade(user_id, trade_id)
                if action == "PLACE_ENTRY":
                    if success:
                        _send_once(
                            user_id,
                            f"live_entry_filled:{trade_id}",
                            _live_entry_filled_message(trade, result_payload),
                        )
                    else:
                        _send_once(
                            user_id,
                            f"live_entry_failed:{trade_id}:{command_id}",
                            _live_entry_failed_message(command, result_payload, error),
                        )
                elif action == "EXIT_POSITION":
                    if success:
                        _send_once(
                            user_id,
                            f"live_exit_filled:{trade_id}",
                            _live_exit_filled_message(trade, result_payload),
                        )
                    else:
                        _send_once(
                            user_id,
                            f"live_exit_failed:{trade_id}:{command_id}",
                            _live_exit_failed_message(command, result_payload, error),
                        )
            except Exception as exc:
                print(f"Telegram complete-command alert warning: {str(exc)[:160]}")
            return response

        wrapped_complete_command._telegram_alerts_wrapped = True
        service.complete_command = wrapped_complete_command
        patched_any = True

    original_event = getattr(service, "record_position_event", None)
    if original_event and not getattr(original_event, "_telegram_alerts_wrapped", False):
        def wrapped_record_position_event(gateway, event):
            event_payload = dict(event or {})
            response = original_event(gateway, event)
            try:
                user_id = int(gateway["user_id"])
                trade_id = int(event_payload.get("trade_id") or 0)
                trade = _live_trade(user_id, trade_id)
                event_type = str(event_payload.get("event") or "").upper()
                if event_type == "ENTRY_FILLED":
                    _send_once(
                        user_id,
                        f"live_entry_filled:{trade_id}",
                        _live_entry_filled_message(trade, event_payload),
                    )
                elif event_type == "EXIT_FILLED":
                    _send_once(
                        user_id,
                        f"live_exit_filled:{trade_id}",
                        _live_exit_filled_message(trade, event_payload),
                    )
            except Exception as exc:
                print(f"Telegram position-event alert warning: {str(exc)[:160]}")
            return response

        wrapped_record_position_event._telegram_alerts_wrapped = True
        service.record_position_event = wrapped_record_position_event
        patched_any = True

    return patched_any


def apply_trade_telegram_alerts_patch():
    global _PATCHED
    if _PATCHED:
        return {"patched": False, "already_patched": True}

    patched = []
    try:
        patched.append(_patch_paper_manager())
    except Exception as exc:
        print(f"Telegram paper manager patch failed: {str(exc)[:180]}")
        patched.append(False)

    try:
        patched.append(_patch_live_gateway())
    except Exception as exc:
        print(f"Telegram live gateway patch failed: {str(exc)[:180]}")
        patched.append(False)

    _PATCHED = any(patched)
    return {
        "patched": _PATCHED,
        "paper_manager": bool(patched[0]) if patched else False,
        "live_gateway": bool(patched[1]) if len(patched) > 1 else False,
    }

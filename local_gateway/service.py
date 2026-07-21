import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from database import get_db


GATEWAY_ONLINE_SECONDS = 45
LEASE_SECONDS = 30
ADMIN_ONLY_DEFAULT = True


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(value=None):
    return (value or _utcnow()).isoformat()


def _hash_secret(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_json(value):
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return "{}"


def _load_json(value):
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def admin_only_enabled():
    value = str(os.getenv("LOCAL_GATEWAY_ADMIN_ONLY", "true")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def ensure_local_gateway_schema():
    conn = get_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_gateways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                device_name TEXT NOT NULL,
                token_hash TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                server_armed INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT,
                last_ip TEXT,
                expected_static_ip TEXT,
                agent_version TEXT,
                paired_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_order_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                trade_id INTEGER,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_token_hash TEXT,
                lease_expires_at TEXT,
                leased_at TEXT,
                broker_order_id TEXT,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (trade_id) REFERENCES trades(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_local_commands_user_status
            ON local_order_commands(user_id, status, created_at)
            """
        )

        trade_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        additions = [
            ("underlying", "TEXT"),
            ("side", "TEXT"),
            ("symboltoken", "TEXT"),
            ("exchange", "TEXT"),
            ("broker_order_id", "TEXT"),
            ("sl_price", "REAL"),
            ("target_price", "REAL"),
            ("exit_reason", "TEXT"),
            ("entry_candle_id", "TEXT"),
            ("gateway_command_id", "INTEGER"),
            ("metadata_json", "TEXT"),
        ]
        for name, sql_type in additions:
            if name not in trade_columns:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}")

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_live_user_status
            ON trades(user_id, status, created_at)
            """
        )
        conn.commit()
    finally:
        conn.close()


def require_personal_user(user):
    if admin_only_enabled() and not bool(user["is_admin"]):
        raise HTTPException(
            status_code=403,
            detail="Personal static-IP gateway is enabled only for the owner/admin account",
        )


def pair_gateway(user_id, device_name, expected_static_ip=""):
    ensure_local_gateway_schema()
    token = "okai_gw_" + secrets.token_urlsafe(36)
    now = _iso()
    clean_name = str(device_name or "OKAI Server Phone").strip()[:80]
    expected_ip = str(expected_static_ip or "").strip()[:64]
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO local_gateways(
                user_id, device_name, token_hash, enabled, server_armed,
                expected_static_ip, paired_at, updated_at
            ) VALUES (?, ?, ?, 1, 0, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                device_name=excluded.device_name,
                token_hash=excluded.token_hash,
                enabled=1,
                server_armed=0,
                expected_static_ip=excluded.expected_static_ip,
                paired_at=excluded.paired_at,
                updated_at=excluded.updated_at
            """,
            (
                int(user_id),
                clean_name,
                _hash_secret(token),
                expected_ip,
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE local_order_commands
            SET status='cancelled', error='Gateway re-paired', updated_at=?
            WHERE user_id=? AND status IN ('pending','leased')
            """,
            (now, int(user_id)),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def authenticate_gateway(token):
    ensure_local_gateway_schema()
    token_hash = _hash_secret(token)
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT g.*, u.email, u.is_admin, u.is_active
            FROM local_gateways g
            JOIN users u ON u.id=g.user_id
            WHERE g.token_hash=? AND g.enabled=1
            LIMIT 1
            """,
            (token_hash,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not bool(row["is_active"]):
        raise HTTPException(status_code=401, detail="Invalid or disabled gateway token")
    if admin_only_enabled() and not bool(row["is_admin"]):
        raise HTTPException(status_code=403, detail="Gateway is restricted to owner/admin")
    return row


def heartbeat_gateway(gateway, observed_ip, agent_version=""):
    now = _iso()
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE local_gateways
            SET last_seen_at=?, last_ip=?, agent_version=?, updated_at=?
            WHERE id=?
            """,
            (
                now,
                str(observed_ip or "")[:64],
                str(agent_version or "")[:40],
                now,
                gateway["id"],
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM local_gateways WHERE id=?",
            (gateway["id"],),
        ).fetchone()
    finally:
        conn.close()
    expected = str(row["expected_static_ip"] or "").strip()
    actual = str(row["last_ip"] or "").strip()
    return {
        "observed_ip": actual,
        "expected_static_ip": expected or None,
        "static_ip_matches": bool(expected and actual and expected == actual),
        "server_armed": bool(row["server_armed"]),
        "gateway_enabled": bool(row["enabled"]),
        "last_seen_at": row["last_seen_at"],
    }


def set_gateway_armed(user_id, armed):
    ensure_local_gateway_schema()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM local_gateways WHERE user_id=? AND enabled=1",
            (int(user_id),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=409, detail="Local gateway is not paired")
        conn.execute(
            "UPDATE local_gateways SET server_armed=?, updated_at=? WHERE user_id=?",
            (int(bool(armed)), _iso(), int(user_id)),
        )
        if not armed:
            pending_trade_rows = conn.execute(
                """
                SELECT DISTINCT trade_id
                FROM local_order_commands
                WHERE user_id=? AND action='PLACE_ENTRY'
                  AND status IN ('pending','leased')
                  AND trade_id IS NOT NULL
                """,
                (int(user_id),),
            ).fetchall()
            conn.execute(
                """
                UPDATE local_order_commands
                SET status='cancelled', error='Gateway disarmed',
                    lease_token_hash=NULL, lease_expires_at=NULL,
                    updated_at=?
                WHERE user_id=? AND action='PLACE_ENTRY'
                  AND status IN ('pending','leased')
                """,
                (_iso(), int(user_id)),
            )
            trade_ids = [int(row["trade_id"]) for row in pending_trade_rows]
            if trade_ids:
                placeholders = ",".join("?" for _ in trade_ids)
                conn.execute(
                    f"""
                    UPDATE trades
                    SET status='cancelled', exit_reason='Gateway disarmed before entry'
                    WHERE user_id=? AND status='pending'
                      AND id IN ({placeholders})
                    """,
                    (int(user_id), *trade_ids),
                )
        conn.commit()
    finally:
        conn.close()


def get_gateway_status(user_id):
    ensure_local_gateway_schema()
    conn = get_db()
    try:
        gateway = conn.execute(
            "SELECT * FROM local_gateways WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
        counts = conn.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM local_order_commands
            WHERE user_id=?
            GROUP BY status
            """,
            (int(user_id),),
        ).fetchall()
        positions = conn.execute(
            """
            SELECT id, underlying, symbol, option_type, quantity, entry_price,
                   sl_price, target_price, status, broker_order_id, entry_time
            FROM trades
            WHERE user_id=? AND status IN ('pending','open','exit_pending')
            ORDER BY id DESC
            """,
            (int(user_id),),
        ).fetchall()
    finally:
        conn.close()

    online = False
    if gateway and gateway["last_seen_at"]:
        last_seen = _parse_datetime(gateway["last_seen_at"])
        online = bool(last_seen and (_utcnow() - last_seen).total_seconds() <= GATEWAY_ONLINE_SECONDS)

    return {
        "paired": bool(gateway),
        "online": online,
        "server_armed": bool(gateway["server_armed"]) if gateway else False,
        "enabled": bool(gateway["enabled"]) if gateway else False,
        "device_name": gateway["device_name"] if gateway else None,
        "last_seen_at": gateway["last_seen_at"] if gateway else None,
        "observed_ip": gateway["last_ip"] if gateway else None,
        "expected_static_ip": gateway["expected_static_ip"] if gateway else None,
        "agent_version": gateway["agent_version"] if gateway else None,
        "command_counts": {row["status"]: row["total"] for row in counts},
        "open_positions": [dict(row) for row in positions],
        "admin_only": admin_only_enabled(),
        "online_timeout_seconds": GATEWAY_ONLINE_SECONDS,
    }


def gateway_ready(user_id):
    status = get_gateway_status(user_id)
    if not status["paired"]:
        return False, "LOCAL_GATEWAY_NOT_PAIRED", status
    if not status["enabled"]:
        return False, "LOCAL_GATEWAY_DISABLED", status
    if not status["online"]:
        return False, "LOCAL_GATEWAY_OFFLINE", status
    if not status["server_armed"]:
        return False, "LOCAL_GATEWAY_NOT_ARMED", status
    expected = str(status.get("expected_static_ip") or "").strip()
    observed = str(status.get("observed_ip") or "").strip()
    if expected and expected != observed:
        return False, "STATIC_IP_MISMATCH", status
    return True, "READY", status


def queue_live_entry(user_id, payload, idempotency_key, max_concurrent=1, max_trades_per_day=5):
    ensure_local_gateway_schema()
    ready, reason, gateway = gateway_ready(user_id)
    if not ready:
        return {"queued": False, "reason": reason, "gateway": gateway}

    now = _utcnow()
    now_iso = _iso(now)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    payload = dict(payload or {})
    underlying = str(payload.get("underlying") or "").upper()
    symbol = str(payload.get("symbol") or "")
    option_type = str(payload.get("option_type") or "").upper()
    quantity = int(payload.get("quantity") or 0)
    if underlying not in {"NIFTY", "BANKNIFTY", "SENSEX"}:
        return {"queued": False, "reason": "INVALID_UNDERLYING"}
    if option_type not in {"CE", "PE"} or not symbol or quantity <= 0:
        return {"queued": False, "reason": "INVALID_ENTRY_PAYLOAD"}

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id, status, trade_id FROM local_order_commands WHERE idempotency_key=?",
            (str(idempotency_key),),
        ).fetchone()
        if existing:
            conn.commit()
            return {
                "queued": existing["status"] in {"pending", "leased", "succeeded"},
                "reason": "DUPLICATE_CANDLE_BLOCKED",
                "command_id": existing["id"],
                "trade_id": existing["trade_id"],
            }

        open_count = conn.execute(
            """
            SELECT COUNT(*) AS total FROM trades
            WHERE user_id=? AND status IN ('pending','open','exit_pending')
            """,
            (int(user_id),),
        ).fetchone()["total"]
        if int(open_count or 0) >= max(1, int(max_concurrent or 1)):
            conn.rollback()
            return {"queued": False, "reason": "MAX_CONCURRENT_LIVE_TRADES"}

        daily_count = conn.execute(
            """
            SELECT COUNT(*) AS total FROM trades
            WHERE user_id=? AND datetime(created_at) >= datetime(?)
              AND status NOT IN ('failed','cancelled')
            """,
            (int(user_id), day_start.isoformat()),
        ).fetchone()["total"]
        if int(daily_count or 0) >= max(1, int(max_trades_per_day or 1)):
            conn.rollback()
            return {"queued": False, "reason": "MAX_DAILY_LIVE_TRADES"}

        if bool(payload.get("different_index_required")):
            same_index = conn.execute(
                """
                SELECT id FROM trades
                WHERE user_id=? AND underlying=?
                  AND status IN ('pending','open','exit_pending')
                LIMIT 1
                """,
                (int(user_id), underlying),
            ).fetchone()
            if same_index:
                conn.rollback()
                return {"queued": False, "reason": "SAME_INDEX_ALREADY_OPEN"}

        cursor = conn.execute(
            """
            INSERT INTO trades(
                user_id, broker, symbol, option_type, quantity, status,
                underlying, side, symboltoken, exchange, sl_price, target_price,
                entry_candle_id, metadata_json, created_at
            ) VALUES (?, 'angelone-local-gateway', ?, ?, ?, 'pending',
                      ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(user_id),
                symbol,
                option_type,
                quantity,
                underlying,
                option_type,
                str(payload.get("symboltoken") or ""),
                str(payload.get("exchange") or ""),
                float(payload.get("sl_price") or 0) or None,
                float(payload.get("target_price") or 0) or None,
                str(payload.get("candle_id") or ""),
                _safe_json(payload),
                now_iso,
            ),
        )
        trade_id = cursor.lastrowid
        command_cursor = conn.execute(
            """
            INSERT INTO local_order_commands(
                user_id, trade_id, action, payload_json, idempotency_key,
                status, created_at, updated_at
            ) VALUES (?, ?, 'PLACE_ENTRY', ?, ?, 'pending', ?, ?)
            """,
            (
                int(user_id),
                trade_id,
                _safe_json({**payload, "trade_id": trade_id}),
                str(idempotency_key),
                now_iso,
                now_iso,
            ),
        )
        command_id = command_cursor.lastrowid
        conn.execute(
            "UPDATE trades SET gateway_command_id=? WHERE id=?",
            (command_id, trade_id),
        )
        conn.commit()
        return {
            "queued": True,
            "reason": "QUEUED_TO_STATIC_IP_GATEWAY",
            "trade_id": trade_id,
            "command_id": command_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def queue_exit(user_id, trade_id, reason="MANUAL EXIT"):
    ensure_local_gateway_schema()
    conn = get_db()
    now = _iso()
    try:
        conn.execute("BEGIN IMMEDIATE")
        trade = conn.execute(
            """
            SELECT * FROM trades
            WHERE id=? AND user_id=? AND status IN ('open','exit_pending')
            """,
            (int(trade_id), int(user_id)),
        ).fetchone()
        if not trade:
            conn.rollback()
            return {"queued": False, "reason": "OPEN_TRADE_NOT_FOUND"}
        existing = conn.execute(
            """
            SELECT id FROM local_order_commands
            WHERE trade_id=? AND action='EXIT_POSITION' AND status IN ('pending','leased','succeeded')
            LIMIT 1
            """,
            (trade["id"],),
        ).fetchone()
        if existing:
            conn.commit()
            return {"queued": True, "reason": "EXIT_ALREADY_QUEUED", "command_id": existing["id"]}

        payload = {
            "trade_id": trade["id"],
            "symbol": trade["symbol"],
            "symboltoken": trade["symboltoken"],
            "exchange": trade["exchange"],
            "quantity": trade["quantity"],
            "reason": str(reason or "MANUAL EXIT")[:120],
        }
        cursor = conn.execute(
            """
            INSERT INTO local_order_commands(
                user_id, trade_id, action, payload_json, idempotency_key,
                status, created_at, updated_at
            ) VALUES (?, ?, 'EXIT_POSITION', ?, ?, 'pending', ?, ?)
            """,
            (
                int(user_id),
                trade["id"],
                _safe_json(payload),
                f"EXIT:{trade['id']}",
                now,
                now,
            ),
        )
        conn.execute(
            "UPDATE trades SET status='exit_pending', exit_reason=? WHERE id=?",
            (payload["reason"], trade["id"]),
        )
        conn.commit()
        return {"queued": True, "reason": "EXIT_QUEUED", "command_id": cursor.lastrowid}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def lease_commands(gateway, limit=5, allow_entries=True):
    ensure_local_gateway_schema()
    limit = max(1, min(int(limit or 5), 10))
    now = _utcnow()
    now_iso = _iso(now)
    conn = get_db()
    commands = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT * FROM local_order_commands
            WHERE user_id=?
              AND (?=1 OR action<>'PLACE_ENTRY')
              AND (
                status='pending'
                OR (status='leased' AND datetime(lease_expires_at) <= datetime(?))
              )
            ORDER BY CASE action WHEN 'EXIT_POSITION' THEN 0 ELSE 1 END, id ASC
            LIMIT ?
            """,
            (gateway["user_id"], int(bool(allow_entries)), now_iso, limit),
        ).fetchall()
        for row in rows:
            lease_token = secrets.token_urlsafe(24)
            expires = now + timedelta(seconds=LEASE_SECONDS)
            conn.execute(
                """
                UPDATE local_order_commands
                SET status='leased', attempts=attempts+1, lease_token_hash=?,
                    lease_expires_at=?, leased_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    _hash_secret(lease_token),
                    _iso(expires),
                    now_iso,
                    now_iso,
                    row["id"],
                ),
            )
            commands.append({
                "id": row["id"],
                "trade_id": row["trade_id"],
                "action": row["action"],
                "payload": _load_json(row["payload_json"]),
                "lease_token": lease_token,
                "lease_expires_at": _iso(expires),
                "attempt": int(row["attempts"] or 0) + 1,
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return commands


def complete_command(gateway, command_id, lease_token, success, result=None, error=""):
    ensure_local_gateway_schema()
    result = dict(result or {})
    now = _iso()
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        command = conn.execute(
            "SELECT * FROM local_order_commands WHERE id=? AND user_id=?",
            (int(command_id), gateway["user_id"]),
        ).fetchone()
        if not command:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Gateway command not found")
        if command["status"] in {"succeeded", "failed", "cancelled"}:
            conn.commit()
            return {"accepted": True, "already_final": True}
        if not secrets.compare_digest(
            str(command["lease_token_hash"] or ""),
            _hash_secret(lease_token),
        ):
            conn.rollback()
            raise HTTPException(status_code=409, detail="Invalid or expired command lease")

        final_status = "succeeded" if success else "failed"
        broker_order_id = str(result.get("broker_order_id") or "")[:120]
        conn.execute(
            """
            UPDATE local_order_commands
            SET status=?, broker_order_id=?, result_json=?, error=?,
                completed_at=?, updated_at=?
            WHERE id=?
            """,
            (
                final_status,
                broker_order_id,
                _safe_json(result),
                str(error or result.get("error") or "")[:500],
                now,
                now,
                command["id"],
            ),
        )

        if command["trade_id"]:
            if command["action"] == "PLACE_ENTRY":
                if success:
                    conn.execute(
                        """
                        UPDATE trades
                        SET status='open', broker_order_id=?, entry_price=?,
                            entry_time=?, sl_price=COALESCE(?, sl_price),
                            target_price=COALESCE(?, target_price)
                        WHERE id=? AND user_id=?
                        """,
                        (
                            broker_order_id,
                            float(result.get("entry_price") or 0) or None,
                            str(result.get("entry_time") or now),
                            float(result.get("sl_price") or 0) or None,
                            float(result.get("target_price") or 0) or None,
                            command["trade_id"],
                            gateway["user_id"],
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE trades SET status='failed', exit_reason=? WHERE id=? AND user_id=?",
                        (str(error or "ENTRY FAILED")[:300], command["trade_id"], gateway["user_id"]),
                    )
            elif command["action"] == "EXIT_POSITION":
                if success:
                    conn.execute(
                        """
                        UPDATE trades
                        SET status='closed', exit_price=?, pnl=?, exit_time=?,
                            exit_reason=?, broker_order_id=COALESCE(broker_order_id, ?)
                        WHERE id=? AND user_id=?
                        """,
                        (
                            float(result.get("exit_price") or 0) or None,
                            float(result.get("pnl") or 0),
                            str(result.get("exit_time") or now),
                            str(result.get("reason") or "EXIT FILLED")[:200],
                            broker_order_id,
                            command["trade_id"],
                            gateway["user_id"],
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE trades SET status='open', exit_reason=? WHERE id=? AND user_id=?",
                        (str(error or "EXIT FAILED")[:300], command["trade_id"], gateway["user_id"]),
                    )
        conn.commit()
        return {"accepted": True, "status": final_status}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_position_event(gateway, event):
    ensure_local_gateway_schema()
    event = dict(event or {})
    trade_id = int(event.get("trade_id") or 0)
    event_type = str(event.get("event") or "").upper()
    if trade_id <= 0 or event_type not in {"ENTRY_FILLED", "EXIT_FILLED", "POSITION_HEARTBEAT"}:
        raise HTTPException(status_code=400, detail="Invalid position event")
    conn = get_db()
    now = _iso()
    try:
        trade = conn.execute(
            "SELECT * FROM trades WHERE id=? AND user_id=?",
            (trade_id, gateway["user_id"]),
        ).fetchone()
        if not trade:
            raise HTTPException(status_code=404, detail="Trade not found")
        if event_type == "ENTRY_FILLED":
            conn.execute(
                """
                UPDATE trades SET status='open', broker_order_id=?, entry_price=?,
                    entry_time=?, sl_price=?, target_price=?
                WHERE id=?
                """,
                (
                    str(event.get("broker_order_id") or trade["broker_order_id"] or "")[:120],
                    float(event.get("entry_price") or trade["entry_price"] or 0) or None,
                    str(event.get("entry_time") or now),
                    float(event.get("sl_price") or trade["sl_price"] or 0) or None,
                    float(event.get("target_price") or trade["target_price"] or 0) or None,
                    trade_id,
                ),
            )
        elif event_type == "EXIT_FILLED":
            conn.execute(
                """
                UPDATE trades SET status='closed', exit_price=?, pnl=?, exit_time=?, exit_reason=?
                WHERE id=?
                """,
                (
                    float(event.get("exit_price") or 0) or None,
                    float(event.get("pnl") or 0),
                    str(event.get("exit_time") or now),
                    str(event.get("reason") or "LOCAL EXIT")[:200],
                    trade_id,
                ),
            )
        else:
            metadata = _load_json(trade["metadata_json"])
            metadata["gateway_position"] = {
                "ltp": event.get("ltp"),
                "updated_at": now,
                "local_status": event.get("local_status"),
            }
            conn.execute(
                "UPDATE trades SET metadata_json=? WHERE id=?",
                (_safe_json(metadata), trade_id),
            )
        conn.commit()
    finally:
        conn.close()
    return {"accepted": True, "trade_id": trade_id, "event": event_type}

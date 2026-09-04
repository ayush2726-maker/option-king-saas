import sqlite3
import sys
import types
from datetime import datetime, timezone

from broker import upstox_token_automation as automation


class _Response:
    def __init__(self, payload, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


def _database(tmp_path, monkeypatch):
    db_file = tmp_path / "upstox-token.sqlite"

    def open_db():
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    monkeypatch.setattr(automation, "get_db", open_db)
    monkeypatch.setattr(
        automation,
        "encrypt_credential",
        lambda value: "enc:" + value,
    )
    monkeypatch.setattr(
        automation,
        "decrypt_credential",
        lambda value: value.removeprefix("enc:"),
    )

    conn = open_db()
    conn.execute(
        "CREATE TABLE users(id INTEGER PRIMARY KEY, is_active INTEGER DEFAULT 1)"
    )
    conn.execute(
        """
        CREATE TABLE broker_credentials(
            id INTEGER PRIMARY KEY, user_id INTEGER, broker_name TEXT,
            client_id TEXT, api_key TEXT, api_secret TEXT, totp_secret TEXT,
            is_active INTEGER, last_connected TEXT
        )
        """
    )
    conn.execute("INSERT INTO users(id, is_active) VALUES (7, 1)")
    conn.execute(
        """
        INSERT INTO broker_credentials
            (id, user_id, broker_name, client_id, api_key, api_secret, is_active)
        VALUES (1, 7, 'upstox', 'client-123', 'enc:secret-456', 'enc:old-token', 1)
        """
    )
    conn.commit()
    conn.close()
    return open_db


def test_request_uses_saved_secret_and_marks_approval_pending(tmp_path, monkeypatch):
    open_db = _database(tmp_path, monkeypatch)
    captured = {}

    def post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return _Response(
            {
                "status": "success",
                "data": {
                    "authorization_expiry": "1788514200000",
                    "notifier_url": "https://example.test/upstox/notifier",
                },
            }
        )

    result = automation.initiate_token_request(7, http_post=post)
    conn = open_db()
    row = conn.execute(
        "SELECT * FROM upstox_token_automation WHERE user_id=7"
    ).fetchone()
    conn.close()

    assert result["status"] == "approval_pending"
    assert captured["url"].endswith("/client-123")
    assert captured["json"] == {"client_secret": "secret-456"}
    assert row["status"] == "approval_pending"


def test_notifier_validates_and_replaces_only_access_token(tmp_path, monkeypatch):
    open_db = _database(tmp_path, monkeypatch)

    broker_routes_stub = types.ModuleType("broker.routes")
    broker_routes_stub._invalidate_test_result = lambda *_args: None
    broker_routes_stub._stop_stale_broker_runtime = lambda *_args: None
    telegram_routes_stub = types.ModuleType("telegram.routes")
    telegram_routes_stub.notify_user = lambda *_args, **_kwargs: {"success": True}
    monkeypatch.setitem(sys.modules, "broker.routes", broker_routes_stub)
    monkeypatch.setitem(sys.modules, "telegram.routes", telegram_routes_stub)

    def get(_url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer fresh-token"
        return _Response({"status": "success", "data": {"user_id": "U123"}})

    result = automation.store_notifier_token(
        {
            "client_id": "client-123",
            "user_id": "U123",
            "access_token": "fresh-token",
            "expires_at": "1788514200000",
            "message_type": "access_token",
        },
        http_get=get,
    )
    conn = open_db()
    credential = conn.execute(
        "SELECT * FROM broker_credentials WHERE id=1"
    ).fetchone()
    status = conn.execute(
        "SELECT * FROM upstox_token_automation WHERE user_id=7"
    ).fetchone()
    conn.close()

    assert result["success"] is True
    assert credential["api_secret"] == "enc:fresh-token"
    assert credential["api_key"] == "enc:secret-456"
    assert status["status"] == "connected"
    assert status["upstox_user_id"] == "U123"


def test_scheduler_skips_weekend_without_network(tmp_path, monkeypatch):
    _database(tmp_path, monkeypatch)
    saturday = datetime(2026, 9, 5, 4, 0, tzinfo=timezone.utc)
    result = automation.request_due_tokens_once(saturday)

    assert result == {
        "eligible": 0,
        "requested": 0,
        "failed": 0,
        "window_open": False,
    }


def test_scheduler_skips_a_token_that_has_not_expired(tmp_path, monkeypatch):
    open_db = _database(tmp_path, monkeypatch)
    conn = open_db()
    automation.ensure_upstox_token_schema(conn)
    conn.execute(
        """
        UPDATE upstox_token_automation
        SET status='connected', last_token_at='2026-09-04T03:01:00+00:00',
            token_expires_at='2026-09-05T03:30:00+00:00'
        WHERE user_id=7
        """
    )
    conn.commit()
    conn.close()

    friday = datetime(2026, 9, 4, 4, 0, tzinfo=timezone.utc)
    result = automation.request_due_tokens_once(friday)

    assert result["window_open"] is True
    assert result["eligible"] == 0

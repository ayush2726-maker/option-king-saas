from bot import routes


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Conn:
    def __init__(self, broker=None):
        self.broker = broker
        self.closed = False

    def execute(self, sql, params=()):
        if "broker_credentials" in sql:
            return _Cursor(self.broker)
        raise AssertionError(f"Unexpected SQL: {sql}")

    def close(self):
        self.closed = True


def _live_broker():
    return {
        "broker_name": "angelone",
        "client_id": "R12345",
        "api_key": "encrypted-api-key",
        "api_secret": "encrypted-mpin",
        "totp_secret": "encrypted-totp",
    }


def _install_common(monkeypatch, start_result, runtime_running):
    connections = []

    def get_db():
        conn = _Conn(_live_broker() if not connections else None)
        connections.append(conn)
        return conn

    persisted = []
    monkeypatch.setattr(routes, "get_db", get_db)
    monkeypatch.setattr(routes, "ensure_tables", lambda conn: None)
    monkeypatch.setattr(
        routes,
        "get_current_user",
        lambda authorization: {"id": 7},
    )
    monkeypatch.setattr(
        routes,
        "get_strategy_settings",
        lambda conn, user_id: {"trading_mode": "live"},
    )
    monkeypatch.setattr(routes, "decrypt_credential", lambda value: value)
    monkeypatch.setattr(routes, "start_user_bot", lambda user_id, creds: start_result)
    monkeypatch.setattr(
        routes,
        "get_user_bot_state",
        lambda user_id: {"running": runtime_running},
    )
    monkeypatch.setattr(
        routes,
        "save_bot_status",
        lambda conn, user_id, is_running, signal: persisted.append(
            (user_id, is_running, signal)
        ),
    )
    monkeypatch.setattr(routes, "notify_user", lambda *args, **kwargs: None)
    return connections, persisted


def test_successful_live_start_persists_running_status(monkeypatch):
    connections, persisted = _install_common(
        monkeypatch,
        {"success": True, "message": "Bot started"},
        True,
    )

    result = routes.bot_start("Bearer test")

    assert result["success"] is True
    assert persisted == [(7, 1, "LIVE_MODE")]
    assert len(connections) == 2
    assert all(conn.closed for conn in connections)


def test_already_running_live_engine_repairs_stopped_dashboard(monkeypatch):
    _, persisted = _install_common(
        monkeypatch,
        {"success": False, "message": "Bot already running"},
        True,
    )

    result = routes.bot_start("Bearer test")

    assert result == {
        "success": True,
        "message": "LIVE bot already running. Status synchronized.",
        "already_running": True,
    }
    assert persisted == [(7, 1, "LIVE_MODE")]


def test_failed_live_start_does_not_mark_bot_running(monkeypatch):
    connections, persisted = _install_common(
        monkeypatch,
        {"success": False, "message": "Angel login failed"},
        False,
    )

    result = routes.bot_start("Bearer test")

    assert result == {"success": False, "message": "Angel login failed"}
    assert persisted == []
    assert len(connections) == 1
    assert connections[0].closed is True

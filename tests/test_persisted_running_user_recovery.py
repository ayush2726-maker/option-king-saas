from bot import angel_fetcher
from bot import broker_session_reset_patch as recovery
from bot import routes


class _Rows:
    def __init__(self, user_ids):
        self._rows = [{"user_id": user_id} for user_id in user_ids]

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, user_ids):
        self.user_ids = user_ids
        self.closed = False

    def execute(self, sql):
        assert "user_bot_state" in sql
        assert "bot_status" in sql
        return _Rows(self.user_ids)

    def close(self):
        self.closed = True


def test_startup_recovers_all_persisted_on_users(monkeypatch):
    conn = _Conn([2, 3, 4])
    monkeypatch.setattr("database.get_db", lambda: conn)
    monkeypatch.setattr(routes, "ensure_tables", lambda _conn: None)
    monkeypatch.setattr(
        angel_fetcher,
        "get_user_bot_state",
        lambda user_id: {"running": user_id == 2},
    )

    attempted = []

    def start(user_id):
        attempted.append(user_id)
        if user_id == 3:
            return {
                "started": True,
                "state": {"running": True, "shared_paper_feed": True},
            }
        return {"started": False, "state": {"running": False}, "reason": "NO_FEED"}

    monkeypatch.setattr(routes, "_start_saved_runtime_engine", start)

    result = recovery.recover_persisted_running_user_engines()

    assert conn.closed is True
    assert attempted == [3, 4]
    assert result["eligible_users"] == 3
    assert result["already_running"] == 1
    assert result["started"] == 1
    assert result["failed"] == [{"user_id": 4, "reason": "NO_FEED"}]

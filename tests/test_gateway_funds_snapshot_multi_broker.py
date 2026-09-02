import sqlite3

import local_gateway.routes as routes


def _conn(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_authenticated_upstox_gateway_funds_are_persisted(monkeypatch, tmp_path):
    db_path = tmp_path / "gateway.db"
    monkeypatch.setattr(routes, "get_db", lambda: _conn(db_path))

    result = routes._persist_funds_snapshot(
        9,
        {
            "broker": "upstox",
            "available_cash": 18750.25,
            "used_margin": 1249.75,
            "total_limit": 20000,
        },
    )

    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM live_broker_funds WHERE user_id=9"
        ).fetchone()
    finally:
        conn.close()
    assert result["broker"] == "upstox"
    assert row["broker"] == "upstox"
    assert row["available_cash"] == 18750.25
    assert row["total_limit"] == 20000


def test_unknown_broker_funds_are_rejected(monkeypatch):
    monkeypatch.setattr(
        routes,
        "get_db",
        lambda: (_ for _ in ()).throw(AssertionError("database should not open")),
    )

    assert routes._persist_funds_snapshot(
        9,
        {"broker": "unknown", "available_cash": 99999},
    ) is None

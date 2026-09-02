from datetime import datetime, timezone

import bot.live_signal_broker_truth_middleware as live_signal


class _Connection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_live_current_capital_prefers_fresh_gateway_available_cash(monkeypatch):
    conn = _Connection()
    captured = {}

    monkeypatch.setattr(live_signal, "get_db", lambda: conn)

    def snapshot_reader(db, user_id, current):
        captured.update(db=db, user_id=user_id, current=current)
        return {
            "available_cash": 18234.5,
            "used_margin": 1765.5,
            "total_limit": 20000,
            "updated_at": "2026-09-02T10:00:00+00:00",
            "age_seconds": 4.0,
        }

    monkeypatch.setattr(live_signal, "_fresh_live_funds_snapshot", snapshot_reader)
    monkeypatch.setattr(
        live_signal,
        "_capital_from_live_rows",
        lambda *_args: (_ for _ in ()).throw(AssertionError("trade fallback used")),
    )

    now = datetime(2026, 9, 2, 10, 0, 4, tzinfo=timezone.utc)
    result = live_signal._live_capital_payload(7, -250, now=now)

    assert captured == {"db": conn, "user_id": 7, "current": now}
    assert conn.closed is True
    assert result["current_capital"] == 18234.5
    assert result["live_capital"] == 18234.5
    assert result["available_cash"] == 18234.5
    assert result["current_equity"] == 20000
    assert result["starting_capital"] == 20000
    assert result["capital_sync_ok"] is True
    assert result["capital_source"] == "LOCAL_GATEWAY_ANGEL_FRESH_SNAPSHOT"
    assert result["broker_funds_age_seconds"] == 4.0


def test_live_current_capital_accepts_real_zero_cash(monkeypatch):
    conn = _Connection()
    monkeypatch.setattr(live_signal, "get_db", lambda: conn)
    monkeypatch.setattr(
        live_signal,
        "_fresh_live_funds_snapshot",
        lambda *_args: {
            "available_cash": 0,
            "used_margin": 20000,
            "total_limit": 20000,
            "updated_at": "2026-09-02T10:00:00+00:00",
            "age_seconds": 1,
        },
    )

    result = live_signal._live_capital_payload(7, 0)

    assert result["current_capital"] == 0
    assert result["current_equity"] == 20000
    assert result["capital_sync_ok"] is True


def test_live_current_capital_falls_back_without_using_paper_seed(monkeypatch):
    conn = _Connection()
    monkeypatch.setattr(live_signal, "get_db", lambda: conn)
    monkeypatch.setattr(live_signal, "_fresh_live_funds_snapshot", lambda *_args: None)
    monkeypatch.setattr(
        live_signal,
        "_capital_from_live_rows",
        lambda user_id, open_pnl: (19750.0, "LIVE_TRADE_CAPITAL_BASE_PLUS_OPEN_PNL"),
    )

    result = live_signal._live_capital_payload(7, -250)

    assert result["current_capital"] == 19750
    assert result["capital_source"] == "LIVE_TRADE_CAPITAL_BASE_PLUS_OPEN_PNL"
    assert result["capital_sync_ok"] is False
    assert "paper" not in result["capital_source"].lower()


def test_live_signal_exposes_current_capital_top_level_and_in_account(monkeypatch):
    monkeypatch.setattr(live_signal, "_settings", lambda _user_id: {})
    monkeypatch.setattr(
        live_signal,
        "_history_payload",
        lambda _user_id: {"today": {}, "ledger": {}},
    )
    monkeypatch.setattr(
        live_signal,
        "_live_payload",
        lambda _user_id: {"trades": [], "open_positions": []},
    )
    monkeypatch.setattr(live_signal, "_running", lambda _user_id: True)
    monkeypatch.setattr(live_signal, "get_user_bot_state", lambda _user_id: {})
    monkeypatch.setattr(
        live_signal,
        "_live_capital_payload",
        lambda *_args: {
            "starting_capital": 20000,
            "current_capital": 18234.5,
            "current_equity": 20000,
            "live_capital": 18234.5,
            "available_cash": 18234.5,
            "used_margin": 1765.5,
            "broker_total_limit": 20000,
            "capital_source": "LOCAL_GATEWAY_ANGEL_FRESH_SNAPSHOT",
            "capital_sync_ok": True,
        },
    )

    payload = live_signal._payload(7)

    assert payload["current_capital"] == 18234.5
    assert payload["capital"] == 18234.5
    assert payload["account"]["current_capital"] == 18234.5
    assert payload["account"]["available_cash"] == 18234.5
    assert payload["account"]["equity"] == 20000

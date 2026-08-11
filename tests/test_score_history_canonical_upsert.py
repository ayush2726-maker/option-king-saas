import sqlite3

from bot import score_history_patch as history


def _scan(score):
    return {
        "underlying": "NIFTY",
        "status": "OK",
        "candle_id": "2026-08-11T15:29:00+05:30",
        "market_data": {
            "price": 24450.25,
            "adx": 10.0,
            "volume_ratio": 0.0,
        },
        "signal_data": {
            "signal": "WAIT",
            "candidate_signal": "PE",
            "score": score,
        },
    }


def test_same_candle_history_point_updates_to_latest_canonical_score(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "history.db"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(history, "get_db", connect)
    state = {"user_id": 7}

    history._persist_scan_scores(state, [_scan(75)])
    history._persist_scan_scores(state, [_scan(67)])

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT score, signal, engine_updated_at FROM signal_history"
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["score"] == 67
    assert rows[0]["signal"] == "PE"
    assert rows[0]["engine_updated_at"].startswith("AUTO:NIFTY:")

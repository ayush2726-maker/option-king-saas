import json
import sqlite3
from datetime import datetime, timedelta, timezone

from bot import entry_execution_safety_v1_patch as patch


def setup_function():
    with patch._lock:
        patch._quote_samples.clear()
        patch._health.clear()
        patch._last_audit.clear()


def test_option_premium_requires_warmup_then_real_uptick():
    first = patch._momentum_check(7, "angelone", "NIFTYTESTPE", 79.75, now_ts=100.0)
    assert first["allowed"] is False
    assert first["reason"] == "OPTION_PREMIUM_MOMENTUM_WARMUP"

    weak = patch._momentum_check(7, "angelone", "NIFTYTESTPE", 79.90, now_ts=105.0)
    assert weak["allowed"] is False
    assert weak["reason"] == "OPTION_PREMIUM_MOMENTUM_WEAK"

    passed = patch._momentum_check(7, "angelone", "NIFTYTESTPE", 81.00, now_ts=110.0)
    assert passed["allowed"] is True
    assert passed["reason"] == "OPTION_PREMIUM_MOMENTUM_OK"
    assert passed["previous_price"] == 79.9
    assert passed["move_points"] == 1.1


def test_completed_candle_freshness_blocks_stale_and_accepts_fresh():
    now = datetime(2026, 8, 4, 5, 2, 30, tzinfo=timezone.utc)
    fresh = patch._candle_freshness(
        {"candle_id": (now - timedelta(seconds=90)).isoformat()},
        now=now,
    )
    assert fresh["fresh"] is True

    stale = patch._candle_freshness(
        {"candle_id": (now - timedelta(seconds=121)).isoformat()},
        now=now,
    )
    assert stale["fresh"] is False
    assert stale["reason"] == "INDEX_CANDLE_STALE"


def test_repeated_dns_failures_block_until_success():
    message = "HTTPSConnectionPool Max retries exceeded: Failed to resolve apiconnect.angelone.in"
    patch._record_failure(9, "angelone", "candles", message)
    first = patch._health_snapshot(9, "angelone", "candles")
    assert first["blocked"] is False

    patch._record_failure(9, "angelone", "candles", message)
    second = patch._health_snapshot(9, "angelone", "candles")
    assert second["blocked"] is True
    assert second["recent_transport_failures"] == 2

    patch._record_success(9, "angelone", "candles")
    recovered = patch._health_snapshot(9, "angelone", "candles")
    assert recovered["blocked"] is False
    assert recovered["recent_transport_failures"] == 0


def test_entry_snapshot_is_persisted_with_explainable_fields():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE paper_trades (
            id INTEGER PRIMARY KEY,
            reason TEXT
        )
        """
    )
    conn.execute("INSERT INTO paper_trades (id, reason) VALUES (1, 'Real entry score 82')")

    snapshot = {
        "version": patch.PATCH_VERSION,
        "decision_score": 82,
        "minimum_required_score": 82,
        "core_score": 4,
        "market_regime": "TRENDING_BEARISH",
        "fake_breakout_probability": 12,
        "market": {"adx": 39.11},
        "option": {
            "symbol": "NIFTY04AUG2624650PE",
            "quote_source": "angelone",
            "quote_age_seconds": 0.0,
            "premium_momentum": {
                "previous_price": 79.75,
                "current_price": 81.0,
            },
        },
    }

    patch._persist_open_snapshot(conn, 1, snapshot)
    row = conn.execute("SELECT * FROM paper_trades WHERE id=1").fetchone()
    saved = json.loads(row["entry_context_json"])

    assert row["entry_decision_score"] == 82
    assert row["entry_core_score"] == 4
    assert row["entry_market_regime"] == "TRENDING_BEARISH"
    assert row["entry_quote_source"] == "angelone"
    assert row["entry_strategy_version"] == patch.PATCH_VERSION
    assert saved["option"]["premium_momentum"]["previous_price"] == 79.75
    assert patch.PATCH_VERSION in row["reason"]


def test_snapshot_contains_strategy_market_option_and_health_context():
    selected = {
        "underlying": "NIFTY",
        "candle_id": "2026-08-04T10:31:00+05:30",
        "signal_data": {
            "signal": "PE",
            "score": 82,
            "min_score": 82,
            "core_score": 4,
            "market_regime": "TRENDING_BEARISH",
            "fake_breakout_probability": 12,
            "score_breakdown": {"vwap": 14, "ema": 14, "adx": 12},
            "strategy_profile_key": "okai_default_82",
        },
        "market_data": {
            "price": 24583.70,
            "adx": 39.11,
            "volume_ratio": 1.4,
            "vwap": 24620.0,
            "ema9": 24595.0,
            "ema21": 24610.0,
            "supertrend_dir": "DOWN",
            "trend": "DOWNTREND",
            "mtf_confirmed": True,
            "orb_high": 24700.0,
            "orb_low": 24600.0,
            "atr": 31.0,
        },
    }
    resolved = {
        "symbol": "NIFTY04AUG2624650PE",
        "token": "65880",
        "exchange": "NFO",
        "expiry": "2026-08-04",
        "strike": 24650,
    }
    momentum = {
        "allowed": True,
        "reason": "OPTION_PREMIUM_MOMENTUM_OK",
        "previous_price": 79.75,
        "current_price": 81.0,
    }
    candle = {"fresh": True, "age_seconds": 90.0}
    health = {"blocked": False, "candles": {}, "quote": {}}

    snapshot = patch._entry_snapshot(
        "angelone",
        selected,
        resolved,
        81.0,
        {"allowed": True, "grade": "A", "score": 73},
        momentum,
        candle,
        health,
    )

    assert snapshot["side"] == "PE"
    assert snapshot["decision_score"] == 82
    assert snapshot["core_score"] == 4
    assert snapshot["market"]["adx"] == 39.11
    assert snapshot["option"]["premium"] == 81.0
    assert snapshot["option"]["premium_momentum"]["allowed"] is True
    assert snapshot["data_health"]["completed_candle"]["fresh"] is True

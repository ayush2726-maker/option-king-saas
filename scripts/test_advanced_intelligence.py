"""Standalone smoke test for OKAI advanced broker-neutral shadow intelligence."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import bot.advanced_intelligence as advanced


def main() -> None:
    handle, path = tempfile.mkstemp(prefix="okai-advanced-", suffix=".db")
    os.close(handle)

    def get_db():
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    advanced.get_db = get_db
    advanced.get_db_storage_info = lambda: {
        "path": path,
        "persistent": True,
        "source": "SMOKE_TEST",
    }
    advanced.ensure_schema()

    conn = get_db()
    conn.execute("CREATE TABLE user_bot_state(user_id INTEGER,is_running INTEGER)")
    conn.execute("CREATE TABLE bot_status(user_id INTEGER,is_running INTEGER)")
    conn.commit()
    conn.close()

    def contract(broker: str, side: str, token: str, bid: float, ask: float, oi: int):
        return advanced._normalize_contract(
            broker=broker,
            symbol=f"NIFTY_{broker}_{side}",
            token=token,
            exchange="NFO",
            side=side,
            strike=25000,
            expiry="2026-07-30",
            lot_size=65,
            quote={
                "ltp": (bid + ask) / 2,
                "oi": oi,
                "volume": 5000,
                "depth": {
                    "buy": [{"price": bid, "quantity": 1000}],
                    "sell": [{"price": ask, "quantity": 800}],
                },
            },
            greek={},
            spot=25000,
        )

    for broker in ("angelone", "upstox", "zerodha"):
        item = contract(broker, "CE", broker + "-ce", 99, 101, 10000)
        assert item["broker"] == broker
        assert item["greeks_source"] in {"BROKER", "BLACK_SCHOLES_FALLBACK"}
        assert item["ask"] > item["bid"]

    chain = {
        "success": True,
        "broker": "angelone",
        "source": "SMOKE",
        "contracts": [
            contract("angelone", "CE", "ce", 99, 101, 10000),
            contract("angelone", "PE", "pe", 89, 91, 13000),
        ],
    }
    option_data = advanced._option_metrics(1, chain, 25000)
    assert option_data["contract_count"] == 2
    assert option_data["pcr"] > 1.0

    snapshot = {
        "feed_connected": True,
        "market_open": True,
        "price": 25000,
        "symbol": "NIFTY",
        "signal": "CE",
        "strategy_score": 85,
        "adx": 30,
        "volume_ratio": 1.2,
        "vwap": 24980,
    }
    base = {
        "decision": "CE",
        "confidence": 80,
        "probabilities": {"CE": 70, "PE": 10, "NO_TRADE": 20},
    }
    news = {
        "news_bias": "PE",
        "news_strength": 75,
        "news_risk_score": 80,
        "fresh": True,
    }
    global_data = {
        "risk_direction": "PE",
        "risk_score": -55,
        "items": {"sp500": {}, "nasdaq": {}, "crude": {}, "usd_inr": {}},
    }

    result = advanced.build_advanced_decision(
        1, snapshot, base, news, option_data, global_data
    )
    assert result["trade_blocking"] is False
    assert result["order_execution"] is False
    assert result["decision"] in {"CE", "PE", "NO_TRADE"}

    decision_id = advanced._register_decision(1, snapshot, result)
    assert decision_id

    old = (
        datetime.now(timezone.utc) - timedelta(minutes=31)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn = get_db()
    conn.execute(
        "UPDATE ai_advanced_decisions SET created_at=? WHERE id=?",
        (old, decision_id),
    )
    conn.commit()
    conn.close()

    moved_contracts = []
    for item in option_data["contracts"]:
        copy = dict(item)
        if copy["side"] == result["decision"]:
            copy["ltp"] = copy["ltp"] + 15
            copy["bid"] = copy["bid"] + 15
            copy["ask"] = copy["ask"] + 15
        moved_contracts.append(copy)
    advanced._observe_outcomes(
        1,
        {**snapshot, "price": 25040},
        {**option_data, "contracts": moved_contracts},
    )
    summary = advanced.get_advanced_summary(1)
    assert summary["supported_brokers"] == ["angelone", "upstox", "zerodha"]
    assert len(summary["recent_decisions"][0]["outcomes"]) == 3
    assert summary["trade_blocking"] is False
    assert summary["order_execution"] is False

    print("PASS OKAI-ADVANCED-INTELLIGENCE-SHADOW-V1")


if __name__ == "__main__":
    main()

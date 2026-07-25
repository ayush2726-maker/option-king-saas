from bot import advanced_intelligence as advanced
from bot import broker_intelligence as broker


def synthetic_chain():
    return {
        "broker": "angelone",
        "underlying": "NIFTY",
        "spot": 25010.0,
        "expiry": "2026-07-28",
        "native_option_chain": False,
        "rows": [
            {
                "strike": 24950.0,
                "expiry": "2026-07-28",
                "spot": 25010.0,
                "ce": {
                    "side": "CE", "exchange": "NFO", "token": "1",
                    "symbol": "NIFTY24950CE", "ltp": 105, "bid": 104,
                    "ask": 106, "spread_percent": 1.9, "bid_qty": 600,
                    "ask_qty": 250, "oi": 12000, "prev_oi": 9000,
                    "iv": 18, "delta": 0.60, "lot_size": 65,
                },
                "pe": {
                    "side": "PE", "exchange": "NFO", "token": "2",
                    "symbol": "NIFTY24950PE", "ltp": 50, "bid": 49,
                    "ask": 51, "spread_percent": 4.0, "bid_qty": 500,
                    "ask_qty": 250, "oi": 18000, "prev_oi": 12000,
                    "iv": 19, "delta": -0.40, "lot_size": 65,
                },
            },
            {
                "strike": 25000.0,
                "expiry": "2026-07-28",
                "spot": 25010.0,
                "ce": {
                    "side": "CE", "exchange": "NFO", "token": "3",
                    "symbol": "NIFTY25000CE", "ltp": 80, "bid": 79,
                    "ask": 81, "spread_percent": 2.5, "bid_qty": 900,
                    "ask_qty": 250, "oi": 10000, "prev_oi": 7000,
                    "iv": 18, "delta": 0.52, "lot_size": 65,
                },
                "pe": {
                    "side": "PE", "exchange": "NFO", "token": "4",
                    "symbol": "NIFTY25000PE", "ltp": 70, "bid": 69,
                    "ask": 71, "spread_percent": 2.85, "bid_qty": 1000,
                    "ask_qty": 250, "oi": 22000, "prev_oi": 12000,
                    "iv": 19, "delta": -0.48, "lot_size": 65,
                },
            },
            {
                "strike": 25050.0,
                "expiry": "2026-07-28",
                "spot": 25010.0,
                "ce": {
                    "side": "CE", "exchange": "NFO", "token": "5",
                    "symbol": "NIFTY25050CE", "ltp": 55, "bid": 54,
                    "ask": 56, "spread_percent": 3.64, "bid_qty": 500,
                    "ask_qty": 500, "oi": 14000, "prev_oi": 13000,
                    "iv": 20, "delta": 0.42, "lot_size": 65,
                },
                "pe": {
                    "side": "PE", "exchange": "NFO", "token": "6",
                    "symbol": "NIFTY25050PE", "ltp": 95, "bid": 94,
                    "ask": 96, "spread_percent": 2.1, "bid_qty": 700,
                    "ask_qty": 300, "oi": 16000, "prev_oi": 12000,
                    "iv": 20, "delta": -0.58, "lot_size": 65,
                },
            },
        ],
    }


def main():
    summary = broker.summarize_chain(synthetic_chain())
    assert summary["data_coverage_score"] >= 80
    assert summary["pcr"] > 1
    assert broker.selected_contract(summary, "CE")["symbol"] == "NIFTY25000CE"
    for name in ("angelone", "upstox", "zerodha"):
        assert broker.BROKER_CAPABILITIES[name]["core_ai"] is True

    advanced.predict_adaptive = lambda feature, horizon=15: {
        "available": False,
        "status": "COLLECTING",
        "sample_count": 0,
    }
    market = {
        "price": 25010,
        "symbol": "NIFTY",
        "adx": 27,
        "rsi": 56,
        "atr_percent": 0.5,
        "volume_ratio": 1.3,
    }
    base = {
        "decision": "CE",
        "confidence": 68,
        "probabilities": {"CE": 68, "PE": 12, "NO_TRADE": 20},
    }
    news = {
        "fresh": True,
        "news_bias": "CE",
        "news_strength": 55,
        "news_risk_score": 30,
    }
    result = advanced.fuse_advanced(
        market,
        base,
        {
            "success": True,
            "broker": "angelone",
            "option_intelligence": summary,
            "global_market": {"available": False},
        },
        news,
    )
    assert result["decision"] in {"CE", "NO_TRADE"}
    assert result["trade_blocking"] is False
    assert result["order_execution"] is False
    print("PASS OKAI-BROKER-NEUTRAL-ADVANCED-AI-V1")


if __name__ == "__main__":
    main()

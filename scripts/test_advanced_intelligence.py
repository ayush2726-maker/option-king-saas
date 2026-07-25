from bot.advanced_intelligence import fuse, global_features, option_features


def main():
    rows = [
        {
            "strike": 24900,
            "call": {"oi": 1200, "change_oi": 100, "spread_pct": 0.4, "imbalance": 0.1, "greeks": {"iv": 14}},
            "put": {"oi": 2600, "change_oi": 300, "spread_pct": 0.5, "imbalance": 0.2, "greeks": {"iv": 16}},
        },
        {
            "strike": 25000,
            "call": {"oi": 3000, "change_oi": 250, "spread_pct": 0.4, "imbalance": -0.1, "greeks": {"iv": 15}},
            "put": {"oi": 5200, "change_oi": 600, "spread_pct": 0.5, "imbalance": 0.1, "greeks": {"iv": 17}},
        },
        {
            "strike": 25100,
            "call": {"oi": 6000, "change_oi": 500, "spread_pct": 0.6, "imbalance": -0.2, "greeks": {"iv": 16}},
            "put": {"oi": 1800, "change_oi": 150, "spread_pct": 0.5, "imbalance": 0.0, "greeks": {"iv": 18}},
        },
    ]
    option_data = option_features(rows, 25000)
    assert option_data["available"] is True
    assert option_data["points"] == 3
    assert option_data["max_pain"] == 25000

    global_data = global_features([
        {"name": "sp500", "price": 1, "change_pct": 1.0},
        {"name": "nasdaq", "price": 1, "change_pct": 1.2},
        {"name": "crude", "price": 1, "change_pct": -1.0},
        {"name": "usd_inr", "price": 1, "change_pct": -0.2},
        {"name": "india_vix", "price": 1, "change_pct": -2.0},
    ])
    assert global_data["direction"] == "CE"

    result = fuse(
        {"decision": "CE", "confidence": 70, "probabilities": {"CE": 60, "PE": 20, "NO_TRADE": 20}},
        option_data,
        global_data,
        {"fresh": True, "news_bias": "CE", "news_strength": 30},
        {"spread_pct": 0.5},
        {"theta": -5, "iv": 16},
    )
    assert result["decision"] == "CE"
    assert result["trade_blocking"] is False
    assert result["order_execution"] is False

    high_risk = fuse(
        {"decision": "CE", "confidence": 70, "probabilities": {"CE": 55, "PE": 15, "NO_TRADE": 30}},
        {**option_data, "risk": 90, "direction": "NEUTRAL"},
        {**global_data, "direction": "NEUTRAL", "risk_on_score": 0},
        {"fresh": False, "news_bias": "NEUTRAL", "news_strength": 0},
        {"spread_pct": 2.5},
        {"theta": -25, "iv": 60},
    )
    assert high_risk["decision"] == "NO_TRADE"
    assert high_risk["trade_blocking"] is False

    print("PASS OKAI-ADVANCED-INTELLIGENCE-SHADOW-V1")


if __name__ == "__main__":
    main()

"""Offline smoke test for broker-neutral advanced AI components."""
from bot.advanced_intelligence_v2 import estimated_greeks
from bot.advanced_model import FEATURE_NAMES, dumps, fuse, train


greeks = estimated_greeks(
    "CE",
    premium=100,
    spot=25000,
    strike=25000,
    expiry="2026-07-30",
)
assert 0 < greeks["iv"] < 500
assert 0 < greeks["delta"] < 1

market = {
    "price": 25000,
    "vwap": 24980,
    "strategy_score": 85,
    "min_strategy_score": 82,
    "adx": 30,
    "volume_ratio": 1.3,
}
base = {
    "decision": "CE",
    "confidence": 70,
    "probabilities": {"CE": 70, "PE": 15, "NO_TRADE": 15},
}
news = {
    "news_bias": "PE",
    "news_strength": 90,
    "news_risk_score": 80,
    "fresh": True,
}
global_data = {
    "global_bias": "PE",
    "global_strength": 70,
    "global_risk_score": 60,
}
options = {
    "option_bias": "PE",
    "option_strength": 90,
    "data_quality_score": 85,
    "average_spread_percent": 0.5,
    "pcr": 0.7,
    "max_pain_distance_percent": -0.4,
    "put_change_oi": 10,
    "call_change_oi": 100,
    "put_oi": 100,
    "call_oi": 200,
    "iv_skew": 3,
    "depth_imbalance": -0.2,
    "institutional_bias_score": -20,
}
fusion = fuse(
    market,
    base,
    news,
    global_data,
    options,
    {"active": False},
)
assert fusion["probabilities"]["PE"] > fusion["probabilities"]["CE"]
assert fusion["trade_blocking"] is False
assert fusion["order_execution"] is False

rows = []
for index in range(420):
    label = "CE" if index % 3 == 0 else "PE" if index % 3 == 1 else "NO_TRADE"
    features = {name: 0.0 for name in FEATURE_NAMES}
    if label == "CE":
        features.update(
            base_ce=0.85,
            option_ce=1,
            option_strength=0.8,
            pcr=1.3,
            news_ce=1,
            institutional_bias=0.4,
        )
        ce_net, pe_net = 120, -80
    elif label == "PE":
        features.update(
            base_pe=0.85,
            option_pe=1,
            option_strength=0.8,
            pcr=0.7,
            news_pe=1,
            institutional_bias=-0.4,
        )
        ce_net, pe_net = -80, 120
    else:
        features.update(
            base_no_trade=0.9,
            spread_percent=0.08,
            news_risk=0.9,
            global_risk=0.9,
        )
        ce_net, pe_net = -30, -25

    rows.append({
        "feature_json": dumps(features),
        "best_label": label,
        "ce_net_pnl": ce_net,
        "pe_net_pnl": pe_net,
    })

trained = train(rows)
assert trained["success"]
assert trained["validation_accuracy"] > 80

print(
    "PASS OKAI-BROKER-NEUTRAL-ADVANCED-AI-V2",
    fusion["decision"],
    trained["validation_accuracy"],
)

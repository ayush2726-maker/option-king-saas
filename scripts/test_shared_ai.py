from bot.shared_ai import MODEL_VERSION, predict


def check(name, payload, expected):
    result = predict(payload)
    decision = result.get("decision")
    print("\n===", name, "===")
    print("model:", result.get("model_version"))
    print("decision:", decision)
    print("confidence:", result.get("confidence"))
    print("probabilities:", result.get("probabilities"))
    print("reasons:", result.get("reasons"))
    if decision != expected:
        raise SystemExit("%s expected %s but got %s" % (name, expected, decision))


common = {
    "feed_connected": True,
    "feed_age_ms": 500,
    "market_open": True,
    "spread_percent": 0.2,
    "daily_loss_percent": 0,
    "consecutive_losses": 0,
    "has_open_position": False,
}

check(
    "Bullish",
    {
        **common,
        "source": "TEST",
        "symbol": "NIFTY",
        "price": 24520,
        "ema_fast": 24505,
        "ema_slow": 24472,
        "vwap": 24490,
        "signal": "CE",
        "strategy_score": 88,
        "min_strategy_score": 82,
        "server_trade_allowed": True,
        "supertrend_direction": "UP",
        "structure_direction": "UP",
        "mtf_direction": "UP",
        "mtf_confirmed": True,
        "adx": 31,
        "rsi": 61,
        "atr_percent": 0.42,
        "volume_ratio": 1.45,
    },
    "CE",
)

check(
    "Bearish",
    {
        **common,
        "source": "TEST",
        "symbol": "NIFTY",
        "price": 24380,
        "ema_fast": 24405,
        "ema_slow": 24442,
        "vwap": 24420,
        "signal": "PE",
        "strategy_score": 87,
        "min_strategy_score": 82,
        "server_trade_allowed": True,
        "supertrend_direction": "DOWN",
        "structure_direction": "DOWN",
        "mtf_direction": "DOWN",
        "mtf_confirmed": True,
        "adx": 29,
        "rsi": 39,
        "atr_percent": 0.48,
        "volume_ratio": 1.35,
    },
    "PE",
)

check(
    "Stale feed",
    {
        **common,
        "price": 24500,
        "signal": "CE",
        "feed_age_ms": 999999,
    },
    "NO_TRADE",
)

print("\nPASS", MODEL_VERSION)

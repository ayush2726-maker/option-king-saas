"""Offline smoke test for institutional and global-risk overlays."""
from bot.institutional_flow_patch import _flow_summary
from bot.global_market_intelligence import _risk_summary


fii = {
    "NSE_FO|INDEX_OPTIONS": [
        {
            "time_stamp": 2,
            "buy_amount": 200,
            "sell_amount": 100,
            "long_contracts": 50000,
            "short_contracts": 20000,
        }
    ]
}
dii = {
    "NSE_EQ|CASH": [
        {
            "time_stamp": 2,
            "buy_amount": 80,
            "sell_amount": 40,
        }
    ]
}
flow = _flow_summary(fii, dii)
assert flow["available"] is True
assert flow["direction"] == "CE"
assert flow["confidence"] >= 45

risk_on = _risk_summary({
    "sp500": {"last_price": 1, "change_percent": 1.0},
    "nasdaq": {"last_price": 1, "change_percent": 1.0},
    "nikkei": {"last_price": 1, "change_percent": 0.5},
    "hang_seng": {"last_price": 1, "change_percent": 0.4},
    "gift_nifty": {"last_price": 1, "change_percent": 0.5},
    "crude": {"last_price": 1, "change_percent": -1.0},
    "usd_inr": {"last_price": 1, "change_percent": -0.1},
    "india_vix": {"last_price": 1, "change_percent": -2.0},
    "us_10y": {"last_price": 1, "change_percent": -0.2},
})
assert risk_on["direction"] == "CE"
assert risk_on["gift_nifty_configured"] is True

risk_off = _risk_summary({
    "sp500": {"last_price": 1, "change_percent": -2.0},
    "nasdaq": {"last_price": 1, "change_percent": -2.0},
    "crude": {"last_price": 1, "change_percent": 3.0},
    "usd_inr": {"last_price": 1, "change_percent": 1.0},
    "india_vix": {"last_price": 1, "change_percent": 7.0},
    "us_10y": {"last_price": 1, "change_percent": 2.0},
})
assert risk_off["direction"] == "PE"
assert risk_off["global_risk_score"] > risk_on["global_risk_score"]

print("PASS OKAI-INSTITUTIONAL-GLOBAL-OVERLAY-V1")

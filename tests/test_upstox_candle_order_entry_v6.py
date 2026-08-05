from datetime import datetime, timedelta, timezone

from bot import angel_fetcher
from bot.entry_execution_safety_v1_patch import _candle_freshness


class FakeUpstox:
    def get_candles(self, **kwargs):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        rows = []
        for index in range(35):
            stamp = (now - timedelta(minutes=34-index)).isoformat()
            price = 24000 + index
            rows.append([stamp, price, price+2, price-2, price+1, 0, 0])
        return {"success": True, "candles": rows}


def test_upstox_normalized_candles_remain_oldest_first():
    df = angel_fetcher.get_candles_multi("upstox", FakeUpstox(), "NIFTY")
    assert df is not None
    assert str(df.iloc[0]["time"]) < str(df.iloc[-1]["time"])
    assert float(df.iloc[-1]["close"]) > float(df.iloc[0]["close"])


def test_auto_selected_completed_candle_is_fresh():
    df = angel_fetcher.get_candles_multi("upstox", FakeUpstox(), "NIFTY")
    selected = {"candle_id": str(df.iloc[-2]["time"])}
    freshness = _candle_freshness(selected)
    assert freshness["fresh"] is True
    assert freshness["age_seconds"] <= 120

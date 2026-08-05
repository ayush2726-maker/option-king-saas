from pathlib import Path


path = Path("bot/angel_fetcher.py")
text = path.read_text(encoding="utf-8")
old = '''        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "oi"])
        df = df[["time", "open", "high", "low", "close", "volume"]]
        df = df.iloc[::-1].reset_index(drop=True)  # Upstox returns newest-first
'''
new = '''        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "oi"])
        df = df[["time", "open", "high", "low", "close", "volume"]]
        # UpstoxBroker.get_candles already normalizes rows oldest-first.
        # Reversing here again made the AUTO engine read the morning candle as
        # the latest completed candle, so the final freshness guard blocked
        # every qualified entry as INDEX_CANDLE_STALE.
        df = df.sort_values("time").reset_index(drop=True)
'''
if old in text:
    text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
elif "made the AUTO engine read the morning candle" not in text:
    raise SystemExit("Expected Upstox double-reversal block not found")

test_path = Path("tests/test_upstox_candle_order_entry_v6.py")
test_path.write_text(
    '''from datetime import datetime, timedelta, timezone

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
''',
    encoding="utf-8",
)

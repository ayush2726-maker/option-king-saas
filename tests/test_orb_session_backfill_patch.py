from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.orb_session_backfill_patch import (
    calculate_orb_levels_resilient,
    ensure_angel_orb,
    ensure_multi_orb,
)


IST = timezone(timedelta(hours=5, minutes=30))


def _rows(day, start_hour, start_minute, count, base=100.0, tz=IST):
    start = datetime(
        day.year,
        day.month,
        day.day,
        start_hour,
        start_minute,
        tzinfo=tz,
    )
    rows = []
    for index in range(count):
        stamp = start + timedelta(minutes=index)
        price = base + index
        rows.append(
            [
                stamp.isoformat(),
                price,
                price + 2.0,
                price - 2.0,
                price + 0.5,
                0,
            ]
        )
    return rows


def _frame(rows):
    return pd.DataFrame(
        rows,
        columns=["time", "open", "high", "low", "close", "volume"],
    )


def test_orb_levels_use_latest_session_and_convert_utc_to_ist():
    day = datetime(2026, 7, 31, tzinfo=IST).date()
    previous_day = day - timedelta(days=1)
    previous = _rows(previous_day, 9, 15, 20, base=50.0)

    # 03:45 UTC is 09:15 IST.
    current_utc = _rows(
        day,
        3,
        45,
        20,
        base=200.0,
        tz=timezone.utc,
    )
    late = _rows(day, 10, 0, 3, base=250.0)
    high, low = calculate_orb_levels_resilient(
        _frame(previous + current_utc + late)
    )

    assert high == 217.0
    assert low == 198.0


class _FakeAngel:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def getCandleData(self, params):
        self.calls.append(dict(params))
        return {"status": True, "data": self.rows}


def test_angel_late_start_backfills_and_caches_opening_range():
    now = datetime(2026, 8, 3, 10, 5, tzinfo=IST)
    morning = _rows(now.date(), 9, 15, 17, base=500.0)
    late = _frame(_rows(now.date(), 10, 0, 8, base=550.0))

    broker = _FakeAngel(morning)
    merged = ensure_angel_orb(
        late,
        broker,
        "26009",
        "NSE",
        now_ist=now,
    )
    high, low = calculate_orb_levels_resilient(merged)

    assert (high, low) == (517.0, 498.0)
    assert len(broker.calls) == 1
    assert broker.calls[0]["fromdate"].endswith("09:15")
    assert broker.calls[0]["todate"].endswith("09:31")

    # A restart later on the same day should reuse cached morning candles.
    second = _FakeAngel([])
    merged_again = ensure_angel_orb(
        late,
        second,
        "26009",
        "NSE",
        now_ist=now.replace(hour=11),
    )
    assert calculate_orb_levels_resilient(merged_again) == (517.0, 498.0)
    assert second.calls == []


class _FakeMulti:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_candles(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {"success": True, "candles": self.rows}


def test_upstox_late_start_uses_current_day_intraday_history():
    now = datetime(2026, 8, 4, 10, 10, tzinfo=IST)
    morning = list(reversed(_rows(now.date(), 9, 15, 17, base=1000.0)))
    late = _frame(_rows(now.date(), 10, 0, 5, base=1050.0))
    broker = _FakeMulti(morning)

    merged = ensure_multi_orb(
        late,
        "upstox",
        broker,
        "NIFTY",
        upstox_keys={"NIFTY": "NSE_INDEX|Nifty 50"},
        zerodha_tokens={"NIFTY": 256265},
        now_ist=now,
    )

    assert calculate_orb_levels_resilient(merged) == (1017.0, 998.0)
    assert len(broker.calls) == 1
    assert broker.calls[0]["from_date"] == "2026-08-04"
    assert broker.calls[0]["to_date"] == "2026-08-04"
    assert broker.calls[0]["interval"] == "1m"


def test_zerodha_late_start_requests_only_opening_window():
    now = datetime(2026, 8, 5, 10, 20, tzinfo=IST)
    rows = [
        {
            "date": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
        }
        for row in _rows(now.date(), 9, 15, 17, base=1500.0)
    ]
    late = _frame(_rows(now.date(), 10, 0, 5, base=1550.0))
    broker = _FakeMulti(rows)

    merged = ensure_multi_orb(
        late,
        "zerodha",
        broker,
        "BANKNIFTY",
        upstox_keys={"BANKNIFTY": "NSE_INDEX|Nifty Bank"},
        zerodha_tokens={"BANKNIFTY": 260105},
        now_ist=now,
    )

    assert calculate_orb_levels_resilient(merged) == (1517.0, 1498.0)
    assert len(broker.calls) == 1
    assert broker.calls[0]["from_date"].endswith("09:15:00")
    assert broker.calls[0]["to_date"].endswith("09:31:00")
    assert broker.calls[0]["interval"] == "minute"

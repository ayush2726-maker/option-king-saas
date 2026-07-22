from bot.brokers.upstox import UpstoxBroker


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "status": "success",
            "data": {
                "candles": [
                    [
                        "2026-07-03T09:15:00+05:30",
                        24000,
                        24010,
                        23990,
                        24005,
                        0,
                        0,
                    ]
                ]
            },
        }


def broker():
    return UpstoxBroker("client", "secret", "access-token")


def test_past_single_day_uses_historical_v3(monkeypatch):
    called = []

    def fake_get(url, **kwargs):
        called.append(url)
        return FakeResponse()

    monkeypatch.setattr(
        "bot.brokers.upstox.requests.get",
        fake_get,
    )
    monkeypatch.setattr(
        UpstoxBroker,
        "_today_ist",
        staticmethod(lambda: "2026-07-22"),
    )

    result = broker().get_candles(
        symbol="NSE_INDEX|Nifty 50",
        interval="1m",
        from_date="2026-07-03",
        to_date="2026-07-03",
    )

    assert result["success"] is True
    assert result["request_mode"] == "HISTORICAL_V3"
    assert "/historical-candle/intraday/" not in called[0]
    assert called[0].endswith(
        "/minutes/1/2026-07-03/2026-07-03"
    )


def test_current_single_day_uses_intraday_v3(monkeypatch):
    called = []

    def fake_get(url, **kwargs):
        called.append(url)
        return FakeResponse()

    monkeypatch.setattr(
        "bot.brokers.upstox.requests.get",
        fake_get,
    )
    monkeypatch.setattr(
        UpstoxBroker,
        "_today_ist",
        staticmethod(lambda: "2026-07-22"),
    )

    result = broker().get_candles(
        symbol="NSE_INDEX|Nifty 50",
        interval="1m",
        from_date="2026-07-22",
        to_date="2026-07-22",
    )

    assert result["success"] is True
    assert result["request_mode"] == "INTRADAY_CURRENT_DAY"
    assert "/historical-candle/intraday/" in called[0]

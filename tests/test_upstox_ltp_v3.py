from bot.brokers.upstox import UpstoxBroker


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def broker():
    return UpstoxBroker("client", "secret", "access-token")


def test_get_ltps_uses_v3_and_maps_returned_instrument_tokens(monkeypatch):
    called = []

    def fake_get(url, **kwargs):
        called.append((url, kwargs["params"]["instrument_key"]))
        return FakeResponse(
            {
                "status": "success",
                "data": {
                    "NSE_FO:BANKNIFTY2682557600CE": {
                        "last_price": 592.35,
                        "instrument_token": "NSE_FO|50123",
                    },
                    "BSE_FO:SENSEX2682077700CE": {
                        "last_price": 498.10,
                        "instrument_token": "BSE_FO|81234",
                    },
                },
            }
        )

    monkeypatch.setattr("bot.brokers.upstox.requests.get", fake_get)

    result = broker().get_ltps(["NSE_FO|50123", "BSE_FO|81234"])

    assert result["success"] is True
    assert result["quotes"]["NSE_FO|50123"]["ltp"] == 592.35
    assert result["quotes"]["BSE_FO|81234"]["ltp"] == 498.10
    assert result["quote_source"] == "UPSTOX_LTP_V3"
    assert called == [
        (
            "https://api.upstox.com/v3/market-quote/ltp",
            "NSE_FO|50123,BSE_FO|81234",
        )
    ]


def test_get_ltp_falls_back_to_v2_only_when_v3_has_no_quote(monkeypatch):
    called = []

    def fake_get(url, **kwargs):
        called.append(url)
        if "/v3/" in url:
            return FakeResponse({"status": "error", "errors": [{"errorCode": "TEMP"}]}, 503)
        return FakeResponse(
            {
                "status": "success",
                "data": {
                    "NSE_FO:BANKNIFTY2682557600CE": {
                        "last_price": 590.0,
                        "instrument_token": "NSE_FO|50123",
                    }
                },
            }
        )

    monkeypatch.setattr("bot.brokers.upstox.requests.get", fake_get)

    result = broker().get_ltp("NSE_FO|50123")

    assert result["success"] is True
    assert result["ltp"] == 590.0
    assert result["quote_source"] == "UPSTOX_LTP_V2_FALLBACK"
    assert called == [
        "https://api.upstox.com/v3/market-quote/ltp",
        "https://api.upstox.com/v2/market-quote/ltp",
    ]


def test_sensex_symbol_fallback_uses_bse_fo_segment():
    assert (
        UpstoxBroker._quote_instrument("SENSEX2682077700CE", "BSE_FO")
        == "BSE_FO|SENSEX2682077700CE"
    )

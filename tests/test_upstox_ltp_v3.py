from bot.brokers.upstox import UpstoxBroker


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

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


def test_get_ltps_does_not_double_hit_quota_after_429(monkeypatch):
    called = []

    def fake_get(url, **kwargs):
        called.append(url)
        return FakeResponse(
            {
                "status": "error",
                "errors": [
                    {"errorCode": "UDAPI10005", "message": "Too Many Request Sent"}
                ],
            },
            429,
        )

    monkeypatch.setattr("bot.brokers.upstox.requests.get", fake_get)

    result = broker().get_ltps(["NSE_FO|50123", "NSE_FO|50124"])

    assert result["success"] is False
    assert result["rate_limited"] is True
    assert result["retry_after_seconds"] == 45
    assert called == ["https://api.upstox.com/v3/market-quote/ltp"]


def test_sensex_symbol_fallback_uses_bse_fo_segment():
    assert (
        UpstoxBroker._quote_instrument("SENSEX2682077700CE", "BSE_FO")
        == "BSE_FO|SENSEX2682077700CE"
    )


def test_login_reports_rate_limit_without_calling_token_invalid(monkeypatch):
    calls = []
    limited_broker = UpstoxBroker("client", "secret", "rate-limit-login-token")

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse(
            {
                "status": "error",
                "errors": [
                    {"errorCode": "UDAPI10005", "message": "Too Many Request Sent"}
                ],
            },
            429,
            {"Retry-After": "17"},
        )

    monkeypatch.setattr("bot.brokers.upstox.requests.get", fake_get)

    first = limited_broker.login()
    second = limited_broker.login()

    assert first["success"] is False
    assert first["status"] == "rate_limited"
    assert first["rate_limited"] is True
    assert first["retry_after_seconds"] == 17
    assert "Invalid token" not in first["message"]
    assert second["cached"] is True
    assert calls == ["https://api.upstox.com/v2/user/profile"]


def test_get_funds_propagates_rate_limit(monkeypatch):
    def fake_get(url, **kwargs):
        return FakeResponse(
            {
                "status": "error",
                "errors": [
                    {"errorCode": "UDAPI10005", "message": "Too Many Request Sent"}
                ],
            },
            429,
        )

    monkeypatch.setattr("bot.brokers.upstox.requests.get", fake_get)

    result = UpstoxBroker("client", "secret", "funds-limit-token").get_funds()

    assert result["success"] is False
    assert result["rate_limited"] is True
    assert result["retry_after_seconds"] == 45

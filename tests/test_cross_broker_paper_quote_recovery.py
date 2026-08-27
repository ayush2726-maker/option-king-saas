from bot import option_chain
from bot import paper_quote_multi_broker_recovery_v1 as recovery


class FakeAngel:
    def __init__(self):
        self.calls = []

    def ltpData(self, exchange, symbol, token):
        self.calls.append((exchange, symbol, token))
        if token == "ANGEL-991":
            return {"status": True, "data": {"ltp": 80.10}}
        raise RuntimeError("Invalid Angel symbol token")


def test_exact_angel_contract_resolver_keeps_expiry_strike_and_side(monkeypatch):
    monkeypatch.setattr(
        option_chain,
        "_load_cache",
        lambda: [
            {
                "name": "NIFTY",
                "instrumenttype": "OPTIDX",
                "expiry": "01SEP2026",
                "strike": "2415000",
                "symbol": "NIFTY01SEP2624150PE",
                "token": "ANGEL-991",
                "exch_seg": "NFO",
                "lotsize": "65",
            },
            {
                "name": "NIFTY",
                "instrumenttype": "OPTIDX",
                "expiry": "01SEP2026",
                "strike": "2420000",
                "symbol": "NIFTY01SEP2624200PE",
                "token": "WRONG-STRIKE",
                "exch_seg": "NFO",
            },
            {
                "name": "NIFTY",
                "instrumenttype": "OPTIDX",
                "expiry": "08SEP2026",
                "strike": "2415000",
                "symbol": "NIFTY08SEP2624150PE",
                "token": "WRONG-EXPIRY",
                "exch_seg": "NFO",
            },
        ],
    )

    resolved = option_chain.resolve_exact_option(
        "NIFTY", "2026-09-01", 24150, "PE"
    )

    assert resolved["token"] == "ANGEL-991"
    assert resolved["symbol"] == "NIFTY01SEP2624150PE"
    assert resolved["selection"] == "EXACT_ACTIVE_ANGEL_CONTRACT"


def test_angel_stale_quote_translates_old_broker_contract(monkeypatch):
    monkeypatch.setattr(
        option_chain,
        "_load_cache",
        lambda: [
            {
                "name": "NIFTY",
                "instrumenttype": "OPTIDX",
                "expiry": "01SEP2026",
                "strike": "2415000",
                "symbol": "NIFTY01SEP2624150PE",
                "token": "ANGEL-991",
                "exch_seg": "NFO",
                "lotsize": "65",
            }
        ],
    )
    trade = {
        "exch_seg": "NSE_FO",
        "symbol": "NIFTY 24150 PE 01 SEP 26",
        "token": "NSE_FO|OLD-UPSTOX-TOKEN",
        "underlying": "NIFTY",
        "expiry": "2026-09-01",
        "strike": 24150,
        "side": "PE",
    }
    angel = FakeAngel()

    ltp = recovery._quote(angel, "angelone", trade)

    assert ltp == 80.10
    assert angel.calls == [
        ("NSE_FO", "NIFTY 24150 PE 01 SEP 26", "NSE_FO|OLD-UPSTOX-TOKEN"),
        ("NFO", "NIFTY01SEP2624150PE", "ANGEL-991"),
    ]

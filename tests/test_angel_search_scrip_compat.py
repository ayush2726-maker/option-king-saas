import time

import sitecustomize as compat
from SmartApi import SmartConnect


def test_smartconnect_exposes_search_scrip_compatibility():
    assert hasattr(SmartConnect, "searchScrip")


def test_exact_equity_symbol_is_ranked_first(monkeypatch):
    monkeypatch.setattr(
        compat,
        "_master_rows",
        [
            {
                "exchange": "NSE",
                "tradingsymbol": "RELIANCE-EQ",
                "symboltoken": "2885",
                "name": "RELIANCE INDUSTRIES",
            },
            {
                "exchange": "NSE",
                "tradingsymbol": "RELIANCEPP-EQ",
                "symboltoken": "12345",
                "name": "RELIANCE PARTLY PAID",
            },
        ],
    )
    monkeypatch.setattr(compat, "_master_loaded_at", time.monotonic())

    matches = compat._search_master("NSE", "RELIANCE")

    assert matches[0] == {
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE-EQ",
        "symboltoken": "2885",
    }


def test_lookup_is_exchange_scoped(monkeypatch):
    monkeypatch.setattr(
        compat,
        "_master_rows",
        [
            {
                "exchange": "NSE",
                "tradingsymbol": "SBIN-EQ",
                "symboltoken": "3045",
                "name": "STATE BANK OF INDIA",
            },
            {
                "exchange": "BSE",
                "tradingsymbol": "SBIN-A",
                "symboltoken": "500112",
                "name": "STATE BANK OF INDIA",
            },
        ],
    )
    monkeypatch.setattr(compat, "_master_loaded_at", time.monotonic())

    matches = compat._search_master("NSE", "SBIN")

    assert len(matches) == 1
    assert matches[0]["symboltoken"] == "3045"

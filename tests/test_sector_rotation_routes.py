from bot.sector_rotation_routes import (
    _build_payload,
    _normalize_sector,
    _rotation_label,
)


def _stock(symbol, sector, change_percent):
    return {
        "symbol": symbol,
        "name": symbol,
        "sector": sector,
        "ltp": 100.0 + change_percent,
        "previous_close": 100.0,
        "change": change_percent,
        "change_percent": change_percent,
        "status": "connected",
    }


def test_rotation_payload_is_display_only_and_ranked():
    universe = [
        {"symbol": "BANK1", "name": "Bank 1", "sector": "Financial Services"},
        {"symbol": "BANK2", "name": "Bank 2", "sector": "Financial Services"},
        {"symbol": "IT1", "name": "IT 1", "sector": "Information Technology"},
        {"symbol": "IT2", "name": "IT 2", "sector": "Information Technology"},
    ]
    quotes = {
        "BANK1": _stock("BANK1", "Financial Services", 1.4),
        "BANK2": _stock("BANK2", "Financial Services", 0.8),
        "IT1": _stock("IT1", "Information Technology", -0.4),
        "IT2": _stock("IT2", "Information Technology", -1.0),
    }

    payload = _build_payload(
        "NIFTY",
        "test-broker",
        "TEST_UNIVERSE",
        universe,
        quotes,
    )

    assert payload["success"] is True
    assert payload["display_only"] is True
    assert payload["trade_blocking"] is False
    assert payload["order_execution"] is False
    assert payload["summary"]["advancers"] == 2
    assert payload["summary"]["decliners"] == 2
    assert payload["summary"]["strongest_sector"] == "Financial Services"
    assert payload["summary"]["weakest_sector"] == "Information Technology"
    assert payload["sectors"][0]["stocks"][0]["symbol"] == "BANK1"
    assert payload["top_losers"][0]["symbol"] == "IT2"


def test_rotation_labels_cover_positive_negative_and_mixed():
    assert _rotation_label(0.5, 70) == "BROAD_POSITIVE"
    assert _rotation_label(-0.5, 20) == "BROAD_NEGATIVE"
    assert _rotation_label(0.1, 50) == "POSITIVE_BIAS"
    assert _rotation_label(-0.1, 50) == "NEGATIVE_BIAS"
    assert _rotation_label(0.0, 50) == "MIXED"


def test_sector_normalization_is_stable():
    assert _normalize_sector("IT - Software") == "Information Technology"
    assert _normalize_sector("Banks") == "Financial Services"
    assert _normalize_sector("Pharmaceuticals") == "Healthcare"

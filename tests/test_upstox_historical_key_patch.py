from backtest import routes
from backtest.upstox_historical_key_patch import (
    CANONICAL_UPSTOX_INDEX_KEYS,
    _install_key_maps,
    _normalise_instrument,
)


def test_canonical_key_map_never_returns_none():
    previous = getattr(routes, "UPSTOX_INDEX_KEYS", None)
    try:
        routes.UPSTOX_INDEX_KEYS = {}
        installed = _install_key_maps()
        assert installed == CANONICAL_UPSTOX_INDEX_KEYS
        assert all(installed[name] for name in ("NIFTY", "BANKNIFTY", "SENSEX"))
    finally:
        if previous is None:
            delattr(routes, "UPSTOX_INDEX_KEYS")
        else:
            routes.UPSTOX_INDEX_KEYS = previous


def test_index_aliases_normalise_for_auto_backtest():
    assert _normalise_instrument("NIFTY 50") == "NIFTY"
    assert _normalise_instrument("nifty_bank") == "BANKNIFTY"
    assert _normalise_instrument("BSE SENSEX") == "SENSEX"

"""Guarantee valid Upstox index keys reach the historical backtest fetcher.

The backtest fetch function is installed by live_frequency_portfolio_patch and
looks up ``backtest.routes.UPSTOX_INDEX_KEYS`` at runtime.  On some deploys that
module-level mapping was missing/empty, so every AUTO leg called
``UpstoxBroker.get_candles(symbol=None, ...)`` and all monthly days were skipped
with UPSTOX_INSTRUMENT_KEY_MISSING.

This patch owns only key normalisation.  Endpoint selection and the V3 -> V2
fallback remain inside UpstoxBroker.get_candles.
"""

from backtest import routes
from bot import angel_fetcher


CANONICAL_UPSTOX_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}

INSTRUMENT_ALIASES = {
    "NIFTY": "NIFTY",
    "NIFTY50": "NIFTY",
    "NIFTY 50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "BANK NIFTY": "BANKNIFTY",
    "NIFTY BANK": "BANKNIFTY",
    "SENSEX": "SENSEX",
    "BSE SENSEX": "SENSEX",
}


def _normalise_instrument(value):
    text = " ".join(str(value or "").upper().replace("_", " ").split())
    compact = text.replace(" ", "")
    return INSTRUMENT_ALIASES.get(text) or INSTRUMENT_ALIASES.get(compact) or text


def _install_key_maps():
    route_keys = dict(getattr(routes, "UPSTOX_INDEX_KEYS", {}) or {})
    fetcher_keys = dict(getattr(angel_fetcher, "UPSTOX_INDEX_KEYS", {}) or {})

    for instrument, key in CANONICAL_UPSTOX_INDEX_KEYS.items():
        route_keys[instrument] = str(route_keys.get(instrument) or key)
        fetcher_keys[instrument] = str(fetcher_keys.get(instrument) or key)

    routes.UPSTOX_INDEX_KEYS = route_keys
    angel_fetcher.UPSTOX_INDEX_KEYS = fetcher_keys
    return route_keys


def apply_upstox_historical_key_patch():
    if getattr(routes, "_okai_upstox_historical_key_patch_v1", False):
        return

    _install_key_maps()
    original_fetch = routes.fetch_backtest_candles

    def patched_fetch_backtest_candles(
        broker_name,
        obj,
        instrument,
        date_str,
    ):
        if str(broker_name or "").lower() != "upstox":
            return original_fetch(
                broker_name,
                obj,
                instrument,
                date_str,
            )

        normalised = _normalise_instrument(instrument)
        key_map = _install_key_maps()
        key = key_map.get(normalised)
        if not key:
            raise RuntimeError(
                "UPSTOX_INDEX_KEY_NOT_CONFIGURED: "
                + str(instrument or "<blank>")
            )

        # The wrapped fetcher performs the actual broker request and diagnostics.
        # It now receives a canonical instrument name whose map entry cannot be
        # None, while UpstoxBroker still handles invalid-key re-resolution and
        # V2 historical fallback.
        return original_fetch(
            broker_name,
            obj,
            normalised,
            date_str,
        )

    routes.fetch_backtest_candles = patched_fetch_backtest_candles
    routes._okai_upstox_historical_key_patch_v1 = True

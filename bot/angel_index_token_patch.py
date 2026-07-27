"""Angel One current index-token compatibility.

Angel deprecated the old NIFTY/BANKNIFTY index tokens 26000/26009 for
historical OHLC. The current AMXIDX tokens are 99926000/99926009.
"""

from bot import angel_fetcher


_CURRENT_TOKENS = {
    "NIFTY": "99926000",
    "BANKNIFTY": "99926009",
    "SENSEX": "99919000",
}
_OLD_TO_CURRENT = {
    "26000": "99926000",
    "26009": "99926009",
}


def apply_angel_index_token_patch():
    if getattr(angel_fetcher, "_okai_angel_index_token_patch_v1", False):
        return

    # Mutate the existing dictionary in place because history_provider imports
    # the same object. This immediately fixes AUTO scan, graph and history fetches.
    angel_fetcher.INDEX_TOKENS.update(_CURRENT_TOKENS)
    angel_fetcher.NIFTY_TOKEN = _CURRENT_TOKENS["NIFTY"]
    angel_fetcher.BANK_TOKEN = _CURRENT_TOKENS["BANKNIFTY"]

    original_get_candles = angel_fetcher.get_candles

    def patched_get_candles(
        obj,
        token,
        interval="ONE_MINUTE",
        exchange="NSE",
    ):
        safe_token = _OLD_TO_CURRENT.get(str(token), str(token))
        return original_get_candles(
            obj,
            safe_token,
            interval=interval,
            exchange=exchange,
        )

    angel_fetcher.get_candles = patched_get_candles

    # A running process may already have cached an empty/error response for the
    # deprecated tokens. Clear only the in-memory historical caches after patch.
    try:
        from bot import history_provider

        history_provider._CACHE.clear()
        history_provider._ERROR_CACHE.clear()
    except Exception:
        pass

    angel_fetcher._okai_angel_index_token_patch_v1 = True

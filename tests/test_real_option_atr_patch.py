from backtest.real_option_atr_patch import _option_atr_before_entry


def test_option_atr_uses_only_candles_before_entry():
    bars = {}
    keys = []
    for minute in range(100, 117):
        keys.append(minute)
        bars[minute] = {
            "high": 110 + minute - 100,
            "low": 100 + minute - 100,
            "close": 105 + minute - 100,
        }

    # A very large entry-candle range must not affect the pre-entry ATR.
    bars[117] = {"high": 500, "low": 1, "close": 250}
    keys.append(117)

    result = {
        "bars": bars,
        "bar_keys": keys,
        "entry_minute": 117,
    }
    atr, samples = _option_atr_before_entry(result)
    assert samples == 14
    assert atr == 10.0

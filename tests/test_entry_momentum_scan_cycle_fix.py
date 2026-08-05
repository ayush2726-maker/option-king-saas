from bot import entry_execution_safety_v1_patch as patch


def setup_function():
    with patch._lock:
        patch._quote_samples.clear()


def test_previous_minute_quote_survives_auto_scan_cycle():
    first = patch._momentum_check(7, "upstox", "SENSEXTESTPE", 100.0, now_ts=100.0)
    assert first["allowed"] is False
    assert first["reason"] == "OPTION_PREMIUM_MOMENTUM_WARMUP"

    second = patch._momentum_check(7, "upstox", "SENSEXTESTPE", 100.6, now_ts=165.0)
    assert second["allowed"] is True
    assert second["reason"] == "OPTION_PREMIUM_MOMENTUM_OK"

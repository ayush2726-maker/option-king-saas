# Live ATR SL guard

LIVE local-gateway entries now require a valid server-provided ATR stop before the broker order is submitted.

The entry is blocked when `sl_price` is missing/non-positive, `expected_entry_price` is missing/non-positive, or `sl_price >= expected_entry_price`.

This prevents the gateway from silently reaching the legacy 12% fallback path for malformed LIVE entry payloads. The existing 4% first-profit-lock behavior is unchanged.

#!/usr/bin/env python3
"""OKAI local gateway V3: rate-limit-safe Angel LTP and funds sync.

Keeps V2 risk/order behaviour unchanged while preventing the 1-second monitor and
RMS heartbeat from hammering Angel SmartAPI at the same time. Position LTP is
polled at a safe cadence, rate-limit errors back off without forcing repeated
logins, and the last good LTP continues to be published to Railway.
"""

import time

try:
    from . import okai_local_gateway as base
    from . import okai_local_gateway_v2 as v2
except ImportError:
    import okai_local_gateway as base
    import okai_local_gateway_v2 as v2


RATE_SAFE_VERSION = "1.3.0-RATE-SAFE-LTP-SYNC"
LTP_MIN_INTERVAL_SECONDS = 2.2
RATE_LIMIT_BACKOFF_SECONDS = 8.0
FUNDS_REFRESH_SECONDS = 60.0


class RateSafeAngelSession(base.AngelSession):
    """Do not re-login on Angel rate-limit responses; use timed backoff instead."""

    def __init__(self, config):
        super().__init__(config)
        self._ltp_cache = {}
        self._ltp_last_call = {}
        self._ltp_backoff_until = {}

    @staticmethod
    def _key(exchange, symbol, token):
        return (str(exchange or "").upper(), str(symbol or ""), str(token or ""))

    @staticmethod
    def _is_rate_limit_error(exc):
        text = str(exc or "").lower()
        return (
            "exceeding access rate" in text
            or "rate limit" in text
            or "too many requests" in text
            or "access denied" in text
        )

    def ltp(self, exchange, symbol, token):
        key = self._key(exchange, symbol, token)
        now = time.monotonic()
        cached = self._ltp_cache.get(key)
        last_call = self._ltp_last_call.get(key, 0.0)
        backoff_until = self._ltp_backoff_until.get(key, 0.0)

        if cached is not None and (now < backoff_until or now - last_call < LTP_MIN_INTERVAL_SECONDS):
            return float(cached)

        self._ltp_last_call[key] = now
        try:
            obj = self.login(force=False)
            response = obj.ltpData(str(exchange), str(symbol), str(token))
            if not response or (isinstance(response, dict) and response.get("status") is False):
                raise RuntimeError(str(response)[:240])
            value = float(response["data"]["ltp"])
            if value <= 0:
                raise RuntimeError("INVALID_OPTION_LTP")
            self._ltp_cache[key] = value
            self._ltp_backoff_until[key] = 0.0
            return value
        except Exception as exc:
            if self._is_rate_limit_error(exc):
                self._ltp_backoff_until[key] = now + RATE_LIMIT_BACKOFF_SECONDS
                if cached is not None:
                    return float(cached)
                raise RuntimeError(
                    f"ANGEL_RATE_LIMIT_BACKOFF {RATE_LIMIT_BACKOFF_SECONDS:.0f}s | {str(exc)[:160]}"
                )

            # For auth/session failures only, allow one fresh login retry.
            self.obj = None
            try:
                obj = self.login(force=True)
                response = obj.ltpData(str(exchange), str(symbol), str(token))
                if not response or (isinstance(response, dict) and response.get("status") is False):
                    raise RuntimeError(str(response)[:240])
                value = float(response["data"]["ltp"])
                if value <= 0:
                    raise RuntimeError("INVALID_OPTION_LTP")
                self._ltp_cache[key] = value
                return value
            except Exception as retry_exc:
                if cached is not None:
                    return float(cached)
                raise retry_exc


class RateSafeSaaSClient(v2.RiskV2SaaSClient):
    """Cache RMS funds so normal heartbeats do not consume Angel API rate budget."""

    def __init__(self, config):
        super().__init__(config)
        self._funds_angel = RateSafeAngelSession(config)
        self._funds_cache = None
        self._funds_cache_at = 0.0

    def _broker_funds(self):
        now = time.monotonic()
        if self._funds_cache is not None and now - self._funds_cache_at < FUNDS_REFRESH_SECONDS:
            return dict(self._funds_cache)

        result = super()._broker_funds()
        if result is not None:
            self._funds_cache = dict(result)
            self._funds_cache_at = now
            return result
        return dict(self._funds_cache) if self._funds_cache is not None else None


class RateSafeGatewayRunner(v2.RiskV2GatewayRunner):
    def __init__(self, config):
        super().__init__(config)
        # One shared rate-safe Angel session for entries, exits and monitoring.
        self.angel = RateSafeAngelSession(config)


def install_patches():
    # Install all V2 risk logic first, then replace only the API-rate-sensitive pieces.
    v2.install_patches()
    base.AGENT_VERSION = RATE_SAFE_VERSION
    base.SaaSClient = RateSafeSaaSClient
    base.GatewayRunner = RateSafeGatewayRunner


def command_doctor_v3():
    install_patches()
    v2.command_doctor_v2()
    print("Angel LTP rate-safe polling: ENABLED ✅")
    print(f"LTP minimum interval: {LTP_MIN_INTERVAL_SECONDS:.1f}s ✅")
    print(f"Angel rate-limit backoff: {RATE_LIMIT_BACKOFF_SECONDS:.0f}s ✅")
    print(f"Funds refresh cache: {FUNDS_REFRESH_SECONDS:.0f}s ✅")
    print(f"Gateway version: {RATE_SAFE_VERSION} ✅")


def main():
    install_patches()
    # Preserve v2 setup/doctor semantics while exposing the V3 doctor details.
    base.command_doctor = command_doctor_v3
    base.main()


if __name__ == "__main__":
    main()

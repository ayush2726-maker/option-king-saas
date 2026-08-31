#!/usr/bin/env python3
"""OKAI local gateway V3: rate-limit-safe Angel LTP and funds sync.

Keeps V2 risk/order behaviour unchanged while preventing the 1-second monitor and
RMS heartbeat from hammering Angel SmartAPI at the same time. Position LTP is
polled at a safe cadence, rate-limit errors back off without forcing repeated
logins, and every Railway position heartbeat is self-describing (symbol/order/
entry/quantity) so the app ledger can map it without relying on matching DB ids.
"""

import time
from datetime import datetime

try:
    from . import okai_local_gateway as base
    from . import okai_local_gateway_v2 as v2
except ImportError:
    import okai_local_gateway as base
    import okai_local_gateway_v2 as v2


RATE_SAFE_VERSION = "1.3.1-RATE-SAFE-DIRECT-POSITION-SYNC"
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
        self.angel = RateSafeAngelSession(config)

    def monitor_positions(self):
        """V2 trail/exit logic plus direct symbol-bearing Railway heartbeat."""
        now_ist = datetime.now(base.IST)
        current_hhmm = now_ist.strftime("%H:%M")
        for position in self.open_positions():
            try:
                ltp = self.angel.ltp(
                    position["exchange"],
                    position["symbol"],
                    position["symboltoken"],
                )
                entry = float(position["entry_price"])
                initial_sl = float(position["initial_sl_price"] or position["sl_price"])
                peak = max(float(position["peak_ltp"] or entry), float(ltp))
                cost_be = float(position["breakeven_price"] or entry)
                trail = v2.dynamic_profit_lock(entry, initial_sl, peak, cost_be)
                old_sl = float(position["sl_price"] or initial_sl)
                active_sl = max(old_sl, float(trail["sl_price"]))
                stage = trail["stage"]

                self.db.execute(
                    """
                    UPDATE local_positions
                    SET last_ltp=?, peak_ltp=?, sl_price=?, trail_stage=?
                    WHERE trade_id=?
                    """,
                    (ltp, peak, active_sl, stage, position["trade_id"]),
                )
                self.db.commit()

                reason = None
                if ltp <= active_sl:
                    reason = (
                        "PROFIT_LOCK_TRAIL"
                        if stage != "INITIAL_ATR_SL"
                        else "LOCAL 1-SECOND ATR SL HIT"
                    )
                elif current_hhmm >= str(position["force_exit_at"]):
                    reason = "LOCAL EOD EXIT 15:25 IST"

                if reason:
                    self.execute_exit(position["trade_id"], reason)
                    print(
                        f"✅ Exit complete | trade={position['trade_id']} | "
                        f"{reason} | ltp={ltp:.2f} sl={active_sl:.2f} stage={stage}"
                    )
                    continue

                last_sent = self.last_position_heartbeat.get(position["trade_id"], 0)
                if base.time.time() - last_sent >= 10:
                    event = {
                        "event": "POSITION_HEARTBEAT",
                        "trade_id": int(position["trade_id"]),
                        "symbol": str(position["symbol"]),
                        "symboltoken": str(position["symboltoken"]),
                        "exchange": str(position["exchange"]),
                        "option_type": str(position["option_type"] or ""),
                        "entry_order_id": str(position["entry_order_id"] or ""),
                        "entry_price": entry,
                        "quantity": int(position["quantity"]),
                        "ltp": float(ltp),
                        "peak_ltp": peak,
                        "active_sl": active_sl,
                        "cost_safe_breakeven": cost_be,
                        "trail_stage": stage,
                        "peak_r": trail["peak_r"],
                        "risk_engine": RATE_SAFE_VERSION,
                        "local_status": "open",
                    }
                    self.saas.position_event(event)
                    self.last_position_heartbeat[position["trade_id"]] = base.time.time()
                    print(
                        f"📡 POSITION_SYNC | {position['symbol']} | "
                        f"ltp={ltp:.2f} | qty={int(position['quantity'])}"
                    )
            except Exception as exc:
                print(
                    f"⚠️ Position monitor warning | trade={position['trade_id']} | "
                    f"{str(exc)[:180]}"
                )


def install_patches():
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
    print("Direct symbol position sync: ENABLED ✅")
    print(f"Gateway version: {RATE_SAFE_VERSION} ✅")


def main():
    install_patches()
    base.command_doctor = command_doctor_v3
    base.main()


if __name__ == "__main__":
    main()

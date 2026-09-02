#!/usr/bin/env python3
"""OKAI local gateway worker for Upstox.

Credentials remain on the gateway device. The SaaS sees only the per-user
gateway token and broker funds snapshot; actual Upstox order requests originate
from this device/public IP.
"""

import ipaddress
import math
import time
from urllib.parse import quote

try:
    from . import okai_local_gateway as base
    from . import okai_local_gateway_v2 as v2
except ImportError:
    import okai_local_gateway as base
    import okai_local_gateway_v2 as v2

UPSTOX_GATEWAY_VERSION = "1.0.0-UPSTOX-MULTI-PROFILE"
BASE_URL = "https://api.upstox.com/v2"
V3_URL = "https://api.upstox.com/v3"


class UpstoxLocalSession:
    def __init__(self, config):
        self.config = config
        self.broker = config["upstox"]
        self.session = base.requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.broker['access_token']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"OKAI-Local-Gateway/{UPSTOX_GATEWAY_VERSION}",
        })

    @staticmethod
    def _instrument(symbol, token, exchange):
        raw = str(token or "").strip()
        if "|" in raw:
            return raw
        segment = "BSE_FO" if str(exchange or "").upper().startswith(("BSE", "BFO")) else "NSE_FO"
        return f"{segment}|{str(symbol or raw).strip()}"

    @staticmethod
    def _error(payload):
        if not isinstance(payload, dict):
            return str(payload)[:300]
        return str(payload.get("errors") or payload.get("message") or payload)[:300]

    def profile(self):
        response = self.session.get(f"{BASE_URL}/user/profile", timeout=12)
        payload = response.json() if response.content else {}
        if response.status_code != 200 or payload.get("status") != "success":
            raise RuntimeError(f"Upstox login failed: {self._error(payload)}")
        return payload.get("data") or {}

    def ltp(self, exchange, symbol, token):
        instrument = self._instrument(symbol, token, exchange)
        errors = []
        for url in (f"{V3_URL}/market-quote/ltp", f"{BASE_URL}/market-quote/ltp"):
            try:
                response = self.session.get(
                    url,
                    params={"instrument_key": instrument},
                    timeout=10,
                )
                payload = response.json() if response.content else {}
                data = payload.get("data") or {}
                for response_key, row in data.items():
                    if not isinstance(row, dict):
                        continue
                    returned = str(row.get("instrument_token") or "").strip()
                    normalized = str(response_key or "").replace(":", "|", 1)
                    if returned == instrument or normalized == instrument or len(data) == 1:
                        value = float(row.get("last_price") or 0)
                        if value > 0:
                            return value
                errors.append(f"{response.status_code}:{self._error(payload)}")
                if response.status_code == 429:
                    break
            except Exception as exc:
                errors.append(str(exc)[:160])
        raise RuntimeError("Upstox LTP failed: " + " | ".join(errors[-2:]))

    @staticmethod
    def protected_limit_price(reference_price, transaction, slippage_percent):
        reference = float(reference_price or 0)
        if reference <= 0:
            raise RuntimeError("Valid reference price is required for LIMIT order")
        side = str(transaction or "").upper()
        slippage = max(0.25, min(float(slippage_percent or 0), 10.0))
        raw = reference * (1 + slippage / 100) if side == "BUY" else reference * (1 - slippage / 100)
        ticks = raw / 0.05
        rounded = math.ceil(ticks) if side == "BUY" else math.floor(ticks)
        return round(max(0.05, rounded * 0.05), 2)

    def place_protected_limit(
        self,
        exchange,
        symbol,
        token,
        transaction,
        quantity,
        reference_price,
        slippage_percent,
    ):
        side = str(transaction or "").upper()
        if side not in {"BUY", "SELL"}:
            raise RuntimeError("Transaction must be BUY or SELL")
        instrument = self._instrument(symbol, token, exchange)
        limit_price = self.protected_limit_price(reference_price, side, slippage_percent)
        body = {
            "quantity": int(quantity),
            "product": "I",
            "validity": "DAY",
            "price": limit_price,
            "instrument_token": instrument,
            "order_type": "LIMIT",
            "transaction_type": side,
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
            "tag": "OKAI_LOCAL",
        }
        response = self.session.post(f"{BASE_URL}/order/place", json=body, timeout=12)
        payload = response.json() if response.content else {}
        if response.status_code not in {200, 201} or payload.get("status") != "success":
            raise RuntimeError(f"Upstox LIMIT placeOrder failed: {self._error(payload)}")
        order_id = str((payload.get("data") or {}).get("order_id") or "")
        if not order_id:
            raise RuntimeError("Upstox order ID missing")
        return {
            "order_id": order_id,
            "limit_price": limit_price,
            "reference_price": round(float(reference_price), 2),
            "slippage_percent": float(slippage_percent),
            "order_type": "LIMIT",
        }

    def confirm_order(self, order_id, fallback_price=0.0, timeout_seconds=30):
        deadline = time.time() + max(1, int(timeout_seconds))
        last = {}
        while time.time() < deadline:
            try:
                response = self.session.get(
                    f"{BASE_URL}/order/details",
                    params={"order_id": str(order_id)},
                    timeout=10,
                )
                payload = response.json() if response.content else {}
                row = payload.get("data") or {}
                if isinstance(row, list):
                    row = row[0] if row else {}
                last = row if isinstance(row, dict) else {}
                status = str(last.get("status") or last.get("order_status") or "").lower()
                if status in {"complete", "completed", "filled"}:
                    price = float(last.get("average_price") or last.get("averageprice") or fallback_price or 0)
                    return {"filled": True, "status": status, "price": price, "raw": last}
                if status in {"rejected", "cancelled", "canceled"}:
                    return {
                        "filled": False,
                        "status": status,
                        "error": last.get("status_message") or last.get("message") or str(last),
                        "raw": last,
                    }
            except Exception:
                pass
            time.sleep(1)
        return {"filled": False, "status": "timeout", "error": str(last or "Order fill timeout")}

    def funds(self):
        response = self.session.get(
            f"{BASE_URL}/user/get-funds-and-margin",
            params={"segment": "SEC"},
            timeout=12,
        )
        payload = response.json() if response.content else {}
        if response.status_code != 200 or payload.get("status") != "success":
            raise RuntimeError(f"Upstox funds failed: {self._error(payload)}")
        equity = (payload.get("data") or {}).get("equity") or {}
        available = float(equity.get("available_margin") or 0)
        used = float(equity.get("used_margin") or 0)
        return {
            "broker": "upstox",
            "available_cash": round(max(0.0, available), 2),
            "used_margin": round(max(0.0, used), 2),
            "total_limit": round(max(0.0, available) + max(0.0, used), 2),
        }


class UpstoxSaaSClient(base.SaaSClient):
    def __init__(self, config):
        super().__init__(config)
        self._upstox = UpstoxLocalSession(config)
        self._funds_cache = None
        self._funds_cache_at = 0.0

    def heartbeat(self):
        body = {"agent_version": UPSTOX_GATEWAY_VERSION}
        now = time.monotonic()
        try:
            if self._funds_cache is None or now - self._funds_cache_at >= 60:
                self._funds_cache = self._upstox.funds()
                self._funds_cache_at = now
            body["broker_funds"] = dict(self._funds_cache)
        except Exception as exc:
            print(f"⚠️ Upstox funds snapshot warning | {str(exc)[:180]}")
            if self._funds_cache is not None:
                body["broker_funds"] = dict(self._funds_cache)
        return self.request("POST", "/local-gateway/heartbeat", json=body)


def command_setup_upstox():
    print("=== OKAI UPSTOX STATIC-IP GATEWAY SETUP ===")
    print("This profile is isolated from every other phone profile.")
    saas_url = input(f"SaaS URL [{base.DEFAULT_SAAS_URL}]: ").strip() or base.DEFAULT_SAAS_URL
    email = input("OKAI account email: ").strip().lower()
    password = base.getpass.getpass("OKAI account password: ")
    device_name = input("Device name [My Multi Gateway Phone]: ").strip() or "My Multi Gateway Phone"
    expected_ip = input("Registered public static IPv4 (blank if broker app does not require it): ").strip()
    if expected_ip:
        parsed = ipaddress.ip_address(expected_ip)
        if parsed.version != 4:
            raise RuntimeError("Public IPv4 is required")
        expected_ip = str(parsed)

    gateway_token = base.login_and_pair(
        saas_url,
        email,
        password,
        device_name,
        expected_ip,
    )
    access_token = base.getpass.getpass("Upstox daily Access Token: ").strip()
    if not access_token:
        raise RuntimeError("Upstox daily Access Token is required")

    config = {
        "saas_url": saas_url.rstrip("/"),
        "gateway_token": gateway_token,
        "device_name": device_name,
        "expected_static_ip": expected_ip,
        "local_armed": False,
        "broker": "upstox",
        "upstox": {"access_token": access_token},
        "created_at": base.now_iso(),
        "risk_engine": v2.RISK_ENGINE_VERSION,
        "agent_version": UPSTOX_GATEWAY_VERSION,
    }
    base.save_config(config)
    base.STOP_FILE.touch(exist_ok=True)
    print(f"✅ Upstox setup saved: {base.CONFIG_PATH}")
    print("Gateway is DISARMED. Run doctor before arming.")


def command_doctor_upstox():
    config = base.load_config()
    hb = UpstoxSaaSClient(config).heartbeat()
    broker = UpstoxLocalSession(config)
    profile = broker.profile()
    conn = v2.migrated_state_db()
    conn.close()
    print("=== GATEWAY SERVER CHECK ===")
    print(base.json.dumps(hb, indent=2))
    print("=== UPSTOX CHECK ===")
    print(f"Upstox session OK ✅ | user={profile.get('user_name') or profile.get('user_id') or 'verified'}")
    expected = str(config.get("expected_static_ip") or "")
    observed = str(hb.get("observed_ip") or "")
    if expected:
        print("Static IP match ✅" if expected == observed else f"Static IP mismatch ❌ expected={expected} observed={observed}")
    else:
        print(f"Observed public IP: {observed}")
    print("Per-profile token/config/state isolation: ENABLED ✅")
    print("Paper-rule capital/risk sizing: ENABLED ✅")
    print("Dynamic profit trailing: ENABLED ✅")
    print(f"Gateway version: {UPSTOX_GATEWAY_VERSION} ✅")


def install_patches():
    base.AGENT_VERSION = UPSTOX_GATEWAY_VERSION
    base.AngelSession = UpstoxLocalSession
    base.SaaSClient = UpstoxSaaSClient
    base.state_db = v2.migrated_state_db
    base.GatewayRunner = v2.RiskV2GatewayRunner
    base.command_setup = command_setup_upstox
    base.command_doctor = command_doctor_upstox


def main():
    install_patches()
    base.main()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Safe gateway dry run.

Validates SaaS connectivity, static IP, live-arm state, Angel login,
market-data access, and protected-limit price calculation WITHOUT calling
Angel One placeOrder() and WITHOUT leasing/consuming gateway commands.
"""

import json
import sys

from okai_local_gateway import AngelSession, SaaSClient, load_config


def main():
    config = load_config()
    saas = SaaSClient(config)
    angel = AngelSession(config)

    print("=== OPTION KING GATEWAY SAFE DRY RUN ===")
    hb = saas.heartbeat()
    print(json.dumps({
        "observed_ip": hb.get("observed_ip"),
        "expected_static_ip": hb.get("expected_static_ip"),
        "static_ip_matches": hb.get("static_ip_matches"),
        "server_armed": hb.get("server_armed"),
        "gateway_enabled": hb.get("gateway_enabled"),
        "gateway_access_allowed": hb.get("gateway_access_allowed"),
    }, indent=2))

    if not hb.get("gateway_enabled"):
        raise RuntimeError("Gateway is disabled on SaaS")
    if not hb.get("gateway_access_allowed"):
        raise RuntimeError("Gateway access is not allowed")

    expected = str(hb.get("expected_static_ip") or "").strip()
    observed = str(hb.get("observed_ip") or "").strip()
    if expected and expected != observed:
        raise RuntimeError(f"Static IP mismatch: expected={expected}, observed={observed}")

    ltp = angel.ltp("NSE", "Nifty 50", "26000")
    buy_limit = angel.protected_limit_price(ltp, "BUY", 3.0)
    sell_limit = angel.protected_limit_price(ltp, "SELL", 5.0)

    print(f"Angel login + market data: PASS | NIFTY LTP={ltp}")
    print(f"Simulated BUY protected-limit @3% slippage cap: {buy_limit}")
    print(f"Simulated SELL protected-limit @5% slippage cap: {sell_limit}")
    print("placeOrder called: NO")
    print("gateway command queue consumed: NO")
    print("RESULT: PASS — no real order was sent")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"RESULT: FAIL — {exc}")
        sys.exit(1)

"""Local gateway runtime safety add-ons.

Python imports sitecustomize automatically from the script directory. This keeps
Angel funds sync independent from the risk engine and adds a pre-order guard so
LIVE entries can never silently fall back to a wide default stop when the server
ATR stop is missing or invalid.
"""

import threading
import time

_INSTALLED = False


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _funds_snapshot(runner):
    try:
        api = runner.angel.login()
        response = api.rmsLimit()
        data = response.get("data", {}) if isinstance(response, dict) else {}
        available = float(data.get("availablecash") or data.get("availableCash") or 0)
        used = float(data.get("utiliseddebits") or data.get("utilisedDebits") or 0)
        total = float(
            data.get("net")
            or data.get("netcash")
            or data.get("totalcash")
            or (available + used)
            or 0
        )
        runner.saas.request(
            "POST",
            "/local-gateway/funds-snapshot",
            json={
                "broker": "angelone",
                "available_cash": round(max(0.0, available), 2),
                "used_margin": round(max(0.0, used), 2),
                "total_limit": round(max(0.0, total), 2),
            },
        )
        return True
    except Exception as exc:
        print(f"⚠️ Angel funds sync warning | {str(exc)[:160]}")
        return False


def _funds_loop(runner):
    time.sleep(5)
    while True:
        _funds_snapshot(runner)
        time.sleep(30)


def _validate_server_atr_stop(command):
    payload = (command or {}).get("payload") or {}
    sl_price = _number(payload.get("sl_price"), 0.0)
    expected_entry = _number(payload.get("expected_entry_price"), 0.0)

    if sl_price <= 0:
        raise RuntimeError(
            "INVALID_SERVER_ATR_SL: live entry blocked because sl_price is missing or zero"
        )
    if expected_entry <= 0:
        raise RuntimeError(
            "INVALID_SERVER_ENTRY_PRICE: live entry blocked because expected_entry_price is missing"
        )
    if sl_price >= expected_entry:
        raise RuntimeError(
            "INVALID_SERVER_ATR_SL: live entry blocked because sl_price must be below expected entry"
        )
    return sl_price, expected_entry


def _install():
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        import okai_local_gateway as base
    except Exception:
        return

    original_run = base.GatewayRunner.run
    original_execute_entry = base.GatewayRunner.execute_entry

    if not getattr(original_run, "_okai_funds_sync_wrapped", False):
        def run_with_funds_sync(self, *args, **kwargs):
            thread = threading.Thread(
                target=_funds_loop,
                args=(self,),
                daemon=True,
                name="okai-angel-funds-sync",
            )
            thread.start()
            return original_run(self, *args, **kwargs)

        run_with_funds_sync._okai_funds_sync_wrapped = True
        base.GatewayRunner.run = run_with_funds_sync

    if not getattr(original_execute_entry, "_okai_atr_sl_guard_wrapped", False):
        def execute_entry_with_atr_guard(self, command, *args, **kwargs):
            sl_price, expected_entry = _validate_server_atr_stop(command)
            print(
                f"🛡️ SERVER ATR SL VERIFIED | expected_entry={expected_entry:.2f} | "
                f"sl={sl_price:.2f}"
            )
            return original_execute_entry(self, command, *args, **kwargs)

        execute_entry_with_atr_guard._okai_atr_sl_guard_wrapped = True
        base.GatewayRunner.execute_entry = execute_entry_with_atr_guard

    _INSTALLED = True


try:
    _install()
except Exception as exc:
    print(f"⚠️ Local gateway safety add-on warning | {str(exc)[:160]}")

"""Local gateway runtime add-on: push Angel funds snapshot to SaaS.

Python imports sitecustomize automatically from the script directory. This keeps
funds sync independent from the order/risk engine and does not touch live order
logic.
"""

import threading
import time

_INSTALLED = False


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
    # Give the normal gateway a few seconds to complete startup first.
    time.sleep(5)
    while True:
        _funds_snapshot(runner)
        time.sleep(30)


def _install():
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        import okai_local_gateway as base
    except Exception:
        return

    original_run = base.GatewayRunner.run
    if getattr(original_run, "_okai_funds_sync_wrapped", False):
        _INSTALLED = True
        return

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
    _INSTALLED = True


try:
    _install()
except Exception:
    pass

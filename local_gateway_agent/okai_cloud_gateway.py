#!/usr/bin/env python3
"""Self-bootstrapping dedicated cloud gateway supervisor.

Runs only on a per-customer AWS worker. It fetches the customer's selected
broker configuration through the authenticated gateway bootstrap endpoint,
writes the local 0600 config expected by the battle-tested gateway agents, and
restarts the child automatically when broker credentials/token change.
"""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

API = os.getenv("OKAI_API_BASE", "https://option-king-saas-production.up.railway.app").rstrip("/")
GATEWAY_TOKEN = str(os.getenv("OKAI_GATEWAY_TOKEN") or "").strip()
ROOT = Path(__file__).resolve().parent
HOME = Path.home() / ".okai"
CONFIG_PATH = HOME / "local_gateway.json"
STOP_FILE = HOME / "STOP_NEW_ENTRIES"
REFRESH_SECONDS = 45


def _bootstrap():
    if not GATEWAY_TOKEN:
        raise RuntimeError("OKAI_GATEWAY_TOKEN is missing")
    response = requests.get(
        API + "/local-gateway/provision/bootstrap",
        headers={
            "X-Gateway-Token": GATEWAY_TOKEN,
            "User-Agent": "OKAI-Cloud-Gateway/1.0",
        },
        timeout=25,
    )
    try:
        data = response.json()
    except Exception:
        data = {}
    if not response.ok:
        raise RuntimeError(data.get("detail") or response.text[:220] or "bootstrap failed")
    return data


def _config_from_bootstrap(data):
    broker = str(data.get("broker") or "").strip().lower()
    cfg = {
        "saas_url": API,
        "gateway_token": GATEWAY_TOKEN,
        "device_name": str(data.get("device_name") or "OKAI AWS Dedicated Gateway"),
        "expected_static_ip": str(data.get("expected_static_ip") or "").strip(),
        # Dedicated cloud worker is locally ready at boot. Real entries are still
        # gated by server_armed, which requires the user's explicit Live action.
        "local_armed": True,
        "broker": broker,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cloud_managed": True,
    }
    broker_cfg = data.get("broker_config") or {}
    if broker == "angelone":
        cfg["angel"] = {
            "api_key": str(broker_cfg.get("api_key") or ""),
            "client_id": str(broker_cfg.get("client_id") or ""),
            "password": str(broker_cfg.get("password") or ""),
            "totp_secret": str(broker_cfg.get("totp_secret") or ""),
        }
    elif broker == "upstox":
        cfg["upstox"] = {"access_token": str(broker_cfg.get("access_token") or "")}
    else:
        raise RuntimeError(f"Unsupported cloud gateway broker: {broker}")
    script = str(data.get("agent_script") or "").strip()
    if script not in {"okai_local_gateway_v3.py", "okai_local_gateway_upstox.py"}:
        raise RuntimeError("Invalid cloud gateway agent script")
    return cfg, script


def _fingerprint(config, script):
    raw = json.dumps({"config": config, "script": script}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_config(config):
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
    try:
        STOP_FILE.unlink()
    except FileNotFoundError:
        pass


def _start(script):
    path = ROOT / script
    if not path.exists():
        raise RuntimeError(f"Gateway agent missing: {path.name}")
    return subprocess.Popen(
        [sys.executable, "-u", str(path), "run"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )


def _stop(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def run_forever():
    proc = None
    active_fingerprint = None
    stopping = False

    def handle_stop(*_args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    while not stopping:
        try:
            data = _bootstrap()
            config, script = _config_from_bootstrap(data)
            fp = _fingerprint(config, script)
            child_dead = proc is None or proc.poll() is not None
            changed = fp != active_fingerprint
            if changed or child_dead:
                _stop(proc)
                _write_config(config)
                proc = _start(script)
                active_fingerprint = fp
                print(
                    f"CLOUD_GATEWAY_STARTED broker={config['broker']} "
                    f"ip={config.get('expected_static_ip')} pid={proc.pid}",
                    flush=True,
                )
        except Exception as exc:
            print(f"CLOUD_GATEWAY_BOOTSTRAP_WARNING {str(exc)[:240]}", flush=True)
            if proc is not None and proc.poll() is not None:
                proc = None
        for _ in range(REFRESH_SECONDS):
            if stopping:
                break
            time.sleep(1)

    _stop(proc)


if __name__ == "__main__":
    run_forever()

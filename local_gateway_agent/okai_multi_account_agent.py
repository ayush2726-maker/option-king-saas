#!/usr/bin/env python3
"""Run multiple isolated OKAI gateway accounts from one phone.

Each profile is executed as its own child process with a private HOME directory.
That keeps gateway token, broker credentials, SQLite state, STOP file, command
idempotency and local positions isolated even when two accounts trade at once.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MULTI_HOME = Path.home() / ".okai_multi"
MANIFEST_PATH = MULTI_HOME / "accounts.json"
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SUPPORTED_BROKERS = {
    "angelone": "okai_local_gateway_v3.py",
    "upstox": "okai_local_gateway_upstox.py",
}


def _load_manifest():
    if not MANIFEST_PATH.exists():
        return {"version": 1, "accounts": {}}
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Invalid multi-account manifest")
    data.setdefault("version", 1)
    data.setdefault("accounts", {})
    return data


def _save_manifest(data):
    MULTI_HOME.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(MANIFEST_PATH, 0o600)
    except OSError:
        pass


def _profile_name(value):
    name = str(value or "").strip().lower()
    if not PROFILE_RE.fullmatch(name):
        raise RuntimeError("Profile name: letters/numbers/_/- only, max 32 chars")
    return name


def _profile_home(name):
    return MULTI_HOME / "profiles" / name


def _worker_env(name):
    env = os.environ.copy()
    env["HOME"] = str(_profile_home(name))
    env["OKAI_MULTI_PROFILE"] = name
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _worker_command(account, action):
    broker = str(account.get("broker") or "").lower()
    script = SUPPORTED_BROKERS.get(broker)
    if not script:
        raise RuntimeError(f"Unsupported broker for local gateway: {broker}")
    return [sys.executable, "-u", str(ROOT / script), action]


def add_profile(name, broker):
    name = _profile_name(name)
    broker = str(broker or "").strip().lower()
    if broker not in SUPPORTED_BROKERS:
        raise RuntimeError("Broker must be angelone or upstox")
    manifest = _load_manifest()
    manifest["accounts"][name] = {"broker": broker, "enabled": True}
    _profile_home(name).mkdir(parents=True, exist_ok=True)
    _save_manifest(manifest)
    print(f"✅ Profile added: {name} | broker={broker}")
    print(f"Next: python okai_multi_account_agent.py setup {name}")


def remove_profile(name):
    name = _profile_name(name)
    manifest = _load_manifest()
    if name not in manifest["accounts"]:
        raise RuntimeError("Profile not found")
    del manifest["accounts"][name]
    _save_manifest(manifest)
    print(f"✅ Profile removed from launcher: {name}")
    print("Local credentials/state were intentionally not deleted.")


def run_one_action(name, action):
    name = _profile_name(name)
    manifest = _load_manifest()
    account = manifest["accounts"].get(name)
    if not account:
        raise RuntimeError("Profile not found. Add it first.")
    if not account.get("enabled", True) and action == "run":
        raise RuntimeError("Profile is disabled")
    result = subprocess.run(
        _worker_command(account, action),
        cwd=str(ROOT),
        env=_worker_env(name),
    )
    if result.returncode:
        raise SystemExit(result.returncode)


def list_profiles():
    manifest = _load_manifest()
    accounts = manifest["accounts"]
    if not accounts:
        print("No profiles configured.")
        return
    for name, account in sorted(accounts.items()):
        config_path = _profile_home(name) / ".okai" / "local_gateway.json"
        state = "ready" if config_path.exists() else "setup-needed"
        enabled = "enabled" if account.get("enabled", True) else "disabled"
        print(f"{name:16} {account.get('broker','?'):10} {enabled:8} {state}")


def run_all():
    manifest = _load_manifest()
    selected = [
        (name, account)
        for name, account in sorted(manifest["accounts"].items())
        if account.get("enabled", True)
    ]
    if not selected:
        raise RuntimeError("No enabled profiles configured")

    children = {}
    log_handles = {}
    stopping = False

    def stop_children(*_args):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        for proc in children.values():
            if proc.poll() is None:
                proc.terminate()

    signal.signal(signal.SIGINT, stop_children)
    signal.signal(signal.SIGTERM, stop_children)

    for name, account in selected:
        config_path = _profile_home(name) / ".okai" / "local_gateway.json"
        if not config_path.exists():
            raise RuntimeError(f"Profile {name} needs setup before run-all")
        log_dir = _profile_home(name) / ".okai"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_dir / "multi_gateway.log", "a", encoding="utf-8", buffering=1)
        proc = subprocess.Popen(
            _worker_command(account, "run"),
            cwd=str(ROOT),
            env=_worker_env(name),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        children[name] = proc
        log_handles[name] = log_handle
        print(f"✅ STARTED | {name} | {account['broker']} | pid={proc.pid}")

    print("Single-phone multi-account gateway running. Ctrl+C stops all workers.")
    try:
        while children and not stopping:
            time.sleep(2)
            for name, proc in list(children.items()):
                code = proc.poll()
                if code is None:
                    continue
                print(f"⚠️ WORKER EXITED | {name} | code={code}")
                log_handles.pop(name).close()
                del children[name]
    finally:
        stop_children()
        deadline = time.time() + 8
        for proc in children.values():
            remaining = max(0.0, deadline - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
        for handle in log_handles.values():
            handle.close()


def main():
    parser = argparse.ArgumentParser(description="OKAI single-phone multi-account gateway")
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add")
    add.add_argument("profile")
    add.add_argument("--broker", required=True, choices=sorted(SUPPORTED_BROKERS))

    remove = sub.add_parser("remove")
    remove.add_argument("profile")

    sub.add_parser("list")
    sub.add_parser("run-all")

    for action in ("setup", "doctor", "arm", "disarm", "run"):
        item = sub.add_parser(action)
        item.add_argument("profile")

    args = parser.parse_args()
    try:
        if args.command == "add":
            add_profile(args.profile, args.broker)
        elif args.command == "remove":
            remove_profile(args.profile)
        elif args.command == "list":
            list_profiles()
        elif args.command == "run-all":
            run_all()
        else:
            run_one_action(args.profile, args.command)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

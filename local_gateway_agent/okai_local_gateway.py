#!/usr/bin/env python3
"""OKAI personal static-IP local order gateway.

The SaaS creates signed order commands. This process, running on the owner's
Wi-Fi/server phone, sends the actual Angel One order from the registered static
IPv4 and monitors open option positions every second for SL/target/EOD exit.
"""

import argparse
import getpass
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pyotp
import requests
from SmartApi import SmartConnect


AGENT_VERSION = "1.0.0"
DEFAULT_SAAS_URL = "https://option-king-saas-production.up.railway.app"
HOME = Path.home() / ".okai"
CONFIG_PATH = HOME / "local_gateway.json"
DB_PATH = HOME / "local_gateway_state.db"
STOP_FILE = HOME / "STOP_NEW_ENTRIES"
IST = timezone(timedelta(hours=5, minutes=30))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError("Gateway setup nahi hua. Pehle: python okai_local_gateway.py setup")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config):
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def state_db():
    HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_commands (
            command_id INTEGER PRIMARY KEY,
            action TEXT,
            status TEXT,
            result_json TEXT,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS local_positions (
            trade_id INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            symboltoken TEXT NOT NULL,
            exchange TEXT NOT NULL,
            option_type TEXT,
            quantity INTEGER NOT NULL,
            entry_order_id TEXT,
            entry_price REAL NOT NULL,
            sl_price REAL NOT NULL,
            target_price REAL NOT NULL,
            force_exit_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            last_ltp REAL,
            opened_at TEXT,
            closed_at TEXT,
            exit_order_id TEXT,
            exit_price REAL,
            exit_reason TEXT
        )
        """
    )
    conn.commit()
    return conn


class SaaSClient:
    def __init__(self, config):
        self.base = config["saas_url"].rstrip("/")
        self.gateway_token = config["gateway_token"]
        self.session = requests.Session()
        self.session.headers.update({
            "X-Gateway-Token": self.gateway_token,
            "User-Agent": f"OKAI-Local-Gateway/{AGENT_VERSION}",
        })

    def request(self, method, path, **kwargs):
        response = self.session.request(
            method,
            self.base + path,
            timeout=25,
            **kwargs,
        )
        try:
            data = response.json()
        except Exception:
            data = {}
        if not response.ok:
            raise RuntimeError(data.get("detail") or data.get("message") or response.text[:200])
        return data

    def heartbeat(self):
        return self.request("POST", "/local-gateway/heartbeat", json={"agent_version": AGENT_VERSION})

    def poll(self):
        return self.request("GET", "/local-gateway/poll?limit=5")

    def result(self, command, success, result=None, error=""):
        return self.request(
            "POST",
            f"/local-gateway/commands/{command['id']}/result",
            json={
                "lease_token": command["lease_token"],
                "success": bool(success),
                "result": result or {},
                "error": str(error or "")[:500],
            },
        )

    def position_event(self, event):
        return self.request("POST", "/local-gateway/position-event", json=event)

    def set_server_arm(self, armed):
        return self.request(
            "POST",
            "/local-gateway/gateway-arm",
            json={
                "armed": bool(armed),
                "confirmation": "ARM LIVE ORDERS" if armed else "",
            },
        )


class AngelSession:
    def __init__(self, config):
        self.config = config
        self.obj = None
        self.login_at = 0

    def login(self, force=False):
        if self.obj is not None and not force and time.time() - self.login_at < 6 * 3600:
            return self.obj
        broker = self.config["angel"]
        obj = SmartConnect(api_key=broker["api_key"])
        session = obj.generateSession(
            broker["client_id"],
            broker["password"],
            pyotp.TOTP(broker["totp_secret"]).now(),
        )
        if not session or session.get("status") is False:
            raise RuntimeError(f"Angel login failed: {session}")
        self.obj = obj
        self.login_at = time.time()
        return obj

    def ltp(self, exchange, symbol, token):
        for attempt in range(2):
            try:
                response = self.login(force=attempt > 0).ltpData(exchange, symbol, str(token))
                if not response or response.get("status") is False:
                    raise RuntimeError(str(response)[:200])
                return float(response["data"]["ltp"])
            except Exception:
                self.obj = None
                if attempt:
                    raise
                time.sleep(1)

    def place_market(self, exchange, symbol, token, transaction, quantity):
        params = {
            "variety": "NORMAL",
            "tradingsymbol": str(symbol),
            "symboltoken": str(token),
            "transactiontype": str(transaction).upper(),
            "exchange": str(exchange).upper(),
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(quantity)),
        }
        last_error = None
        for attempt in range(2):
            try:
                result = self.login(force=attempt > 0).placeOrder(params)
                if isinstance(result, dict):
                    if result.get("status") is False:
                        raise RuntimeError(str(result)[:240])
                    order_id = result.get("data", {}).get("orderid") or result.get("orderid")
                else:
                    order_id = result
                if not order_id:
                    raise RuntimeError(f"Angel order ID missing: {result}")
                return str(order_id)
            except Exception as exc:
                last_error = exc
                self.obj = None
                if attempt == 0:
                    time.sleep(1)
        raise RuntimeError(f"Angel placeOrder failed: {last_error}")

    def confirm_order(self, order_id, fallback_price=0.0, timeout_seconds=30):
        deadline = time.time() + timeout_seconds
        last = {}
        while time.time() < deadline:
            try:
                response = self.login().orderBook()
                rows = response.get("data") or [] if isinstance(response, dict) else []
                for row in rows:
                    if str(row.get("orderid")) != str(order_id):
                        continue
                    last = row
                    status = str(row.get("orderstatus") or row.get("status") or "").lower()
                    if status in {"complete", "completed", "filled"}:
                        price = float(row.get("averageprice") or row.get("price") or fallback_price or 0)
                        return {"filled": True, "status": status, "price": price, "raw": row}
                    if status in {"rejected", "cancelled", "canceled"}:
                        return {
                            "filled": False,
                            "status": status,
                            "error": row.get("text") or row.get("rejectionreason") or str(row),
                            "raw": row,
                        }
            except Exception:
                self.obj = None
            time.sleep(1)
        return {"filled": False, "status": "timeout", "error": str(last or "Order fill timeout")}


class GatewayRunner:
    def __init__(self, config):
        self.config = config
        self.saas = SaaSClient(config)
        self.angel = AngelSession(config)
        self.db = state_db()
        self.last_heartbeat = 0
        self.last_position_heartbeat = {}

    def local_armed(self):
        return bool(self.config.get("local_armed")) and not STOP_FILE.exists()

    def remember_command(self, command_id, action, status, result):
        self.db.execute(
            """
            INSERT OR REPLACE INTO processed_commands(
                command_id, action, status, result_json, completed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (command_id, action, status, json.dumps(result or {}), now_iso()),
        )
        self.db.commit()

    def processed(self, command_id):
        return self.db.execute(
            "SELECT * FROM processed_commands WHERE command_id=?",
            (int(command_id),),
        ).fetchone()

    def open_positions(self):
        return self.db.execute(
            "SELECT * FROM local_positions WHERE status='open' ORDER BY trade_id"
        ).fetchall()

    def save_position(self, payload, order_id, entry_price, sl_price, target_price):
        self.db.execute(
            """
            INSERT OR REPLACE INTO local_positions(
                trade_id, symbol, symboltoken, exchange, option_type, quantity,
                entry_order_id, entry_price, sl_price, target_price,
                force_exit_at, status, opened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                int(payload["trade_id"]),
                payload["symbol"],
                str(payload["symboltoken"]),
                payload["exchange"],
                payload.get("option_type"),
                int(payload["quantity"]),
                order_id,
                float(entry_price),
                float(sl_price),
                float(target_price),
                str(payload.get("force_exit_at") or "15:25"),
                now_iso(),
            ),
        )
        self.db.commit()

    def close_position(self, trade_id, order_id, exit_price, reason):
        row = self.db.execute(
            "SELECT * FROM local_positions WHERE trade_id=?",
            (int(trade_id),),
        ).fetchone()
        if not row:
            return None
        pnl = round((float(exit_price) - float(row["entry_price"])) * int(row["quantity"]), 2)
        self.db.execute(
            """
            UPDATE local_positions
            SET status='closed', closed_at=?, exit_order_id=?, exit_price=?, exit_reason=?
            WHERE trade_id=?
            """,
            (now_iso(), order_id, float(exit_price), str(reason), int(trade_id)),
        )
        self.db.commit()
        return pnl

    def execute_entry(self, command):
        payload = command["payload"]
        if not self.local_armed():
            raise RuntimeError("LOCAL GATEWAY DISARMED — new entry blocked")
        now_ist = datetime.now(IST)
        minute = now_ist.hour * 60 + now_ist.minute
        if minute < 9 * 60 + 15 or minute >= 15 * 60 + 25:
            raise RuntimeError("New entry outside 09:15–15:25 IST")
        quantity = int(payload.get("quantity") or 0)
        if quantity <= 0:
            raise RuntimeError("Invalid entry quantity")

        expected = float(payload.get("expected_entry_price") or 0)
        order_id = self.angel.place_market(
            payload["exchange"], payload["symbol"], payload["symboltoken"], "BUY", quantity
        )
        fill = self.angel.confirm_order(order_id, expected)
        if not fill.get("filled"):
            raise RuntimeError(f"Entry order not filled: {fill.get('error') or fill.get('status')}")
        entry_price = float(fill["price"] or expected)
        if entry_price <= 0:
            entry_price = self.angel.ltp(payload["exchange"], payload["symbol"], payload["symboltoken"])
        sl_percent = max(1.0, float(payload.get("sl_percent") or 12))
        target_percent = max(1.0, float(payload.get("target_percent") or 24))
        sl_price = round(entry_price * (1 - sl_percent / 100), 2)
        target_price = round(entry_price * (1 + target_percent / 100), 2)
        self.save_position(payload, order_id, entry_price, sl_price, target_price)
        event = {
            "event": "ENTRY_FILLED",
            "trade_id": int(payload["trade_id"]),
            "broker_order_id": order_id,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "target_price": target_price,
            "entry_time": now_iso(),
        }
        self.saas.position_event(event)
        return {**event, "quantity": quantity}

    def execute_exit(self, trade_id, reason):
        position = self.db.execute(
            "SELECT * FROM local_positions WHERE trade_id=? AND status='open'",
            (int(trade_id),),
        ).fetchone()
        if not position:
            raise RuntimeError("Local open position not found")
        order_id = self.angel.place_market(
            position["exchange"], position["symbol"], position["symboltoken"],
            "SELL", position["quantity"],
        )
        last_ltp = float(position["last_ltp"] or position["entry_price"])
        fill = self.angel.confirm_order(order_id, last_ltp)
        if not fill.get("filled"):
            raise RuntimeError(f"Exit order not filled: {fill.get('error') or fill.get('status')}")
        exit_price = float(fill["price"] or last_ltp)
        pnl = self.close_position(trade_id, order_id, exit_price, reason)
        event = {
            "event": "EXIT_FILLED",
            "trade_id": int(trade_id),
            "broker_order_id": order_id,
            "exit_price": exit_price,
            "pnl": pnl,
            "reason": reason,
            "exit_time": now_iso(),
        }
        self.saas.position_event(event)
        return event

    def process_command(self, command):
        cached = self.processed(command["id"])
        if cached and cached["status"] == "succeeded":
            result = json.loads(cached["result_json"] or "{}")
            self.saas.result(command, True, result)
            return
        try:
            if command["action"] == "PLACE_ENTRY":
                result = self.execute_entry(command)
            elif command["action"] == "EXIT_POSITION":
                result = self.execute_exit(
                    command["payload"]["trade_id"],
                    command["payload"].get("reason") or "REMOTE MANUAL EXIT",
                )
            else:
                raise RuntimeError(f"Unsupported gateway action: {command['action']}")
            self.remember_command(command["id"], command["action"], "succeeded", result)
            self.saas.result(command, True, result)
            print(f"✅ {command['action']} success | command={command['id']} | trade={command.get('trade_id')}")
        except Exception as exc:
            error = str(exc)[:500]
            self.remember_command(command["id"], command["action"], "failed", {"error": error})
            try:
                self.saas.result(command, False, {}, error)
            except Exception:
                pass
            print(f"❌ {command['action']} failed | {error}")

    def monitor_positions(self):
        now_ist = datetime.now(IST)
        current_hhmm = now_ist.strftime("%H:%M")
        for position in self.open_positions():
            try:
                ltp = self.angel.ltp(
                    position["exchange"], position["symbol"], position["symboltoken"]
                )
                self.db.execute(
                    "UPDATE local_positions SET last_ltp=? WHERE trade_id=?",
                    (ltp, position["trade_id"]),
                )
                self.db.commit()
                reason = None
                if ltp <= float(position["sl_price"]):
                    reason = "LOCAL 1-SECOND SL HIT"
                elif ltp >= float(position["target_price"]):
                    reason = "LOCAL 1-SECOND TARGET HIT"
                elif current_hhmm >= str(position["force_exit_at"]):
                    reason = "LOCAL EOD EXIT 15:25 IST"
                if reason:
                    self.execute_exit(position["trade_id"], reason)
                    print(f"✅ Exit complete | trade={position['trade_id']} | {reason}")
                    continue
                last_sent = self.last_position_heartbeat.get(position["trade_id"], 0)
                if time.time() - last_sent >= 15:
                    self.saas.position_event({
                        "event": "POSITION_HEARTBEAT",
                        "trade_id": int(position["trade_id"]),
                        "ltp": ltp,
                        "local_status": "open",
                    })
                    self.last_position_heartbeat[position["trade_id"]] = time.time()
            except Exception as exc:
                print(f"⚠️ Position monitor warning | trade={position['trade_id']} | {str(exc)[:160]}")

    def run(self):
        print("OKAI Local Static-IP Gateway started")
        print(f"Config: {CONFIG_PATH}")
        while True:
            try:
                if time.time() - self.last_heartbeat >= 10:
                    hb = self.saas.heartbeat()
                    self.last_heartbeat = time.time()
                    print(
                        f"HEARTBEAT | IP={hb.get('observed_ip')} | "
                        f"server_armed={hb.get('server_armed')} | local_armed={self.local_armed()}"
                    )
                self.monitor_positions()
                poll = self.saas.poll()
                for command in poll.get("commands") or []:
                    self.process_command(command)
            except KeyboardInterrupt:
                print("Gateway stopped by user")
                return
            except Exception as exc:
                print(f"⚠️ Gateway loop warning: {str(exc)[:200]}")
            time.sleep(1)


def login_and_pair(saas_url, email, password, device_name, expected_static_ip):
    response = requests.post(
        saas_url.rstrip("/") + "/auth/login",
        json={"email": email, "password": password},
        timeout=25,
    )
    data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if not response.ok:
        raise RuntimeError(data.get("detail") or "SaaS login failed")
    token = data.get("token")
    response = requests.post(
        saas_url.rstrip("/") + "/local-gateway/pair",
        headers={"Authorization": f"Bearer {token}"},
        json={"device_name": device_name, "expected_static_ip": expected_static_ip},
        timeout=25,
    )
    pair_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    if not response.ok:
        raise RuntimeError(pair_data.get("detail") or "Gateway pairing failed")
    return pair_data["gateway_token"]


def command_setup():
    print("=== OKAI PERSONAL STATIC-IP GATEWAY SETUP ===")
    saas_url = input(f"SaaS URL [{DEFAULT_SAAS_URL}]: ").strip() or DEFAULT_SAAS_URL
    email = input("OKAI admin email: ").strip().lower()
    password = getpass.getpass("OKAI password: ")
    device_name = input("Device name [Ayush Server Phone]: ").strip() or "Ayush Server Phone"
    expected_ip = input("Registered public static IPv4 (blank allowed): ").strip()
    gateway_token = login_and_pair(saas_url, email, password, device_name, expected_ip)

    print("\nAngel One credentials local phone par encrypted nahi, permission-protected JSON me save honge.")
    angel = {
        "api_key": getpass.getpass("Angel SmartAPI API key: ").strip(),
        "client_id": input("Angel client ID: ").strip(),
        "password": getpass.getpass("Angel PIN/password: ").strip(),
        "totp_secret": getpass.getpass("Angel TOTP secret: ").replace(" ", "").strip(),
    }
    if not all(angel.values()):
        raise RuntimeError("All Angel credentials are required")
    config = {
        "saas_url": saas_url.rstrip("/"),
        "gateway_token": gateway_token,
        "device_name": device_name,
        "expected_static_ip": expected_ip,
        "local_armed": False,
        "angel": angel,
        "created_at": now_iso(),
    }
    save_config(config)
    STOP_FILE.touch(exist_ok=True)
    print(f"✅ Setup saved: {CONFIG_PATH}")
    print("Next: python okai_local_gateway.py doctor")


def command_doctor():
    config = load_config()
    client = SaaSClient(config)
    hb = client.heartbeat()
    print("=== GATEWAY SERVER CHECK ===")
    print(json.dumps(hb, indent=2))
    print("=== ANGEL LOGIN / MARKET DATA CHECK ===")
    angel = AngelSession(config)
    ltp = angel.ltp("NSE", "Nifty 50", "26000")
    print(f"Angel login OK ✅ | NIFTY LTP={ltp}")
    expected = str(config.get("expected_static_ip") or "")
    observed = str(hb.get("observed_ip") or "")
    if expected:
        print("Static IP match ✅" if expected == observed else f"Static IP mismatch ❌ expected={expected} observed={observed}")
    else:
        print(f"Observed public IP: {observed} — isi IPv4 ko Angel SmartAPI app me register karein")


def command_arm():
    config = load_config()
    command_doctor()
    phrase = input("Type exactly ARM LIVE 1 LOT: ").strip().upper()
    if phrase != "ARM LIVE 1 LOT":
        raise RuntimeError("Live arming cancelled")
    config["local_armed"] = True
    save_config(config)
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    SaaSClient(config).set_server_arm(True)
    print("✅ LIVE gateway armed for new entries. Default quantity is 1 lot.")


def command_disarm():
    config = load_config()
    config["local_armed"] = False
    save_config(config)
    STOP_FILE.touch(exist_ok=True)
    try:
        SaaSClient(config).set_server_arm(False)
    except Exception as exc:
        print(f"Server disarm warning: {exc}")
    print("✅ New entries disarmed. Existing open positions will still be monitored/exited.")


def main():
    parser = argparse.ArgumentParser(description="OKAI personal static-IP order gateway")
    parser.add_argument("command", choices=["setup", "doctor", "arm", "disarm", "run"])
    args = parser.parse_args()
    try:
        if args.command == "setup":
            command_setup()
        elif args.command == "doctor":
            command_doctor()
        elif args.command == "arm":
            command_arm()
        elif args.command == "disarm":
            command_disarm()
        else:
            GatewayRunner(load_config()).run()
    except Exception as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

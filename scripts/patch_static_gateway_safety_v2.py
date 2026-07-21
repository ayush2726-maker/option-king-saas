from pathlib import Path


def replace_once(path, old, new, label):
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{label} marker not found in {path}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "local_gateway/service.py",
    """                WHERE user_id=? AND action='PLACE_ENTRY' AND status='pending'
""",
    """                WHERE user_id=? AND action='PLACE_ENTRY'
                  AND status IN ('pending','leased')
""",
    "cancel leased entry on disarm",
)

replace_once(
    "local_gateway/service.py",
    "def lease_commands(gateway, limit=5):\n",
    "def lease_commands(gateway, limit=5, allow_entries=True):\n",
    "lease signature",
)
replace_once(
    "local_gateway/service.py",
    """            WHERE user_id=? AND (
                status='pending'
                OR (status='leased' AND datetime(lease_expires_at) <= datetime(?))
            )
            ORDER BY CASE action WHEN 'EXIT_POSITION' THEN 0 ELSE 1 END, id ASC
            LIMIT ?
            """,
            (gateway["user_id"], now_iso, limit),
""",
    """            WHERE user_id=?
              AND (?=1 OR action<>'PLACE_ENTRY')
              AND (
                status='pending'
                OR (status='leased' AND datetime(lease_expires_at) <= datetime(?))
              )
            ORDER BY CASE action WHEN 'EXIT_POSITION' THEN 0 ELSE 1 END, id ASC
            LIMIT ?
            """,
            (gateway["user_id"], int(bool(allow_entries)), now_iso, limit),
""",
    "lease entry filter",
)

replace_once(
    "local_gateway/routes.py",
    """    heartbeat_gateway(gateway, observed_ip, "poll")
    commands = lease_commands(gateway, limit)
    return {
        "success": True,
        "server_armed": bool(gateway["server_armed"]),
        "commands": commands,
    }
""",
    """    heartbeat = heartbeat_gateway(gateway, observed_ip, "poll")
    expected_ip = str(heartbeat.get("expected_static_ip") or "").strip()
    ip_allowed = (
        bool(heartbeat.get("static_ip_matches"))
        if expected_ip
        else True
    )
    allow_entries = bool(heartbeat.get("server_armed")) and ip_allowed
    commands = lease_commands(
        gateway,
        limit,
        allow_entries=allow_entries,
    )
    return {
        "success": True,
        "server_armed": bool(heartbeat.get("server_armed")),
        "static_ip_matches": bool(heartbeat.get("static_ip_matches")),
        "expected_static_ip": heartbeat.get("expected_static_ip"),
        "observed_ip": heartbeat.get("observed_ip"),
        "entry_commands_allowed": allow_entries,
        "commands": commands,
    }
""",
    "poll IP and arm safety",
)

agent_path = Path("local_gateway_agent/okai_local_gateway.py")
agent = agent_path.read_text(encoding="utf-8")

constants_marker = 'IST = timezone(timedelta(hours=5, minutes=30))\n'
if "class BrokerSubmittedPending" not in agent:
    if constants_marker not in agent:
        raise RuntimeError("agent constants marker not found")
    agent = agent.replace(
        constants_marker,
        constants_marker
        + '\n\nclass BrokerSubmittedPending(RuntimeError):\n    """Broker accepted an order but final fill status is not known yet."""\n\n\n'
        + 'class BrokerFinalFailure(RuntimeError):\n    """Broker returned a final rejected/cancelled status."""\n',
        1,
    )

start = agent.find("    def execute_entry(self, command):\n")
end = agent.find("    def monitor_positions(self):\n", start)
if start < 0 or end < 0:
    raise RuntimeError("agent execution block boundaries not found")

new_execution_block = r'''    def notify_position_event(self, event):
        try:
            self.saas.position_event(event)
        except Exception as exc:
            print(f"⚠️ SaaS position sync pending | {str(exc)[:160]}")

    def acknowledge(self, command, success, result=None, error=""):
        try:
            self.saas.result(command, success, result or {}, error)
            return True
        except Exception as exc:
            print(
                f"⚠️ Command acknowledgement pending | command={command['id']} | "
                f"{str(exc)[:160]}"
            )
            return False

    def fresh_entry_permission(self):
        if not self.local_armed():
            raise RuntimeError("LOCAL GATEWAY DISARMED — new entry blocked")
        heartbeat = self.saas.heartbeat()
        if not heartbeat.get("server_armed"):
            raise RuntimeError("SERVER GATEWAY DISARMED — new entry blocked")
        expected = str(heartbeat.get("expected_static_ip") or "").strip()
        if expected and not heartbeat.get("static_ip_matches"):
            raise RuntimeError(
                "STATIC IP MISMATCH — new entry blocked | "
                f"expected={expected} observed={heartbeat.get('observed_ip')}"
            )
        return heartbeat

    def finalize_entry(self, payload, order_id, fill):
        quantity = int(payload.get("quantity") or 0)
        expected = float(payload.get("expected_entry_price") or 0)
        entry_price = float(fill.get("price") or expected)
        if entry_price <= 0:
            entry_price = self.angel.ltp(
                payload["exchange"], payload["symbol"], payload["symboltoken"]
            )
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
        self.notify_position_event(event)
        return {**event, "quantity": quantity}

    def finalize_exit(self, position, order_id, fill, reason):
        last_ltp = float(position["last_ltp"] or position["entry_price"])
        exit_price = float(fill.get("price") or last_ltp)
        pnl = self.close_position(
            position["trade_id"], order_id, exit_price, reason
        )
        event = {
            "event": "EXIT_FILLED",
            "trade_id": int(position["trade_id"]),
            "broker_order_id": order_id,
            "exit_price": exit_price,
            "pnl": pnl,
            "reason": reason,
            "exit_time": now_iso(),
        }
        self.notify_position_event(event)
        return event

    def execute_entry(self, command):
        payload = command["payload"]
        self.fresh_entry_permission()
        now_ist = datetime.now(IST)
        minute = now_ist.hour * 60 + now_ist.minute
        if minute < 9 * 60 + 15 or minute >= 15 * 60 + 25:
            raise RuntimeError("New entry outside 09:15–15:25 IST")
        quantity = int(payload.get("quantity") or 0)
        if quantity <= 0:
            raise RuntimeError("Invalid entry quantity")

        expected = float(payload.get("expected_entry_price") or 0)
        order_id = self.angel.place_market(
            payload["exchange"], payload["symbol"], payload["symboltoken"],
            "BUY", quantity,
        )
        self.remember_command(
            command["id"],
            command["action"],
            "submitted",
            {
                "broker_order_id": order_id,
                "payload": payload,
                "fallback_price": expected,
            },
        )
        fill = self.angel.confirm_order(order_id, expected)
        if not fill.get("filled"):
            status = str(fill.get("status") or "").lower()
            if status in {"rejected", "cancelled", "canceled"}:
                self.remember_command(
                    command["id"], command["action"], "failed",
                    {"error": fill.get("error") or status, "broker_order_id": order_id},
                )
                raise BrokerFinalFailure(
                    f"Entry order {status}: {fill.get('error') or status}"
                )
            raise BrokerSubmittedPending(
                f"Entry order submitted; confirmation pending | order={order_id}"
            )
        return self.finalize_entry(payload, order_id, fill)

    def execute_exit(self, trade_id, reason, command=None):
        position = self.db.execute(
            "SELECT * FROM local_positions WHERE trade_id=? AND status IN ('open','exit_submitted')",
            (int(trade_id),),
        ).fetchone()
        if not position:
            raise RuntimeError("Local open position not found")

        if position["status"] == "exit_submitted" and position["exit_order_id"]:
            order_id = str(position["exit_order_id"])
        else:
            order_id = self.angel.place_market(
                position["exchange"], position["symbol"], position["symboltoken"],
                "SELL", position["quantity"],
            )
            self.db.execute(
                """
                UPDATE local_positions
                SET status='exit_submitted', exit_order_id=?, exit_reason=?
                WHERE trade_id=?
                """,
                (order_id, str(reason), int(trade_id)),
            )
            self.db.commit()

        if command is not None:
            self.remember_command(
                command["id"],
                command["action"],
                "submitted",
                {
                    "broker_order_id": order_id,
                    "trade_id": int(trade_id),
                    "reason": reason,
                    "fallback_price": float(
                        position["last_ltp"] or position["entry_price"]
                    ),
                },
            )

        fallback = float(position["last_ltp"] or position["entry_price"])
        fill = self.angel.confirm_order(order_id, fallback)
        if not fill.get("filled"):
            status = str(fill.get("status") or "").lower()
            if status in {"rejected", "cancelled", "canceled"}:
                self.db.execute(
                    "UPDATE local_positions SET status='open', exit_order_id=NULL WHERE trade_id=?",
                    (int(trade_id),),
                )
                self.db.commit()
                if command is not None:
                    self.remember_command(
                        command["id"], command["action"], "failed",
                        {"error": fill.get("error") or status, "broker_order_id": order_id},
                    )
                raise BrokerFinalFailure(
                    f"Exit order {status}: {fill.get('error') or status}"
                )
            raise BrokerSubmittedPending(
                f"Exit order submitted; confirmation pending | order={order_id}"
            )
        return self.finalize_exit(position, order_id, fill, reason)

    def reconcile_submitted(self, command, cached):
        saved = json.loads(cached["result_json"] or "{}")
        order_id = str(saved.get("broker_order_id") or "")
        if not order_id:
            self.remember_command(
                command["id"], command["action"], "failed",
                {"error": "Submitted command missing broker order ID"},
            )
            self.acknowledge(
                command, False, {}, "Submitted command missing broker order ID"
            )
            return None

        fallback = float(saved.get("fallback_price") or 0)
        fill = self.angel.confirm_order(order_id, fallback, timeout_seconds=12)
        if not fill.get("filled"):
            status = str(fill.get("status") or "").lower()
            if status in {"rejected", "cancelled", "canceled"}:
                error = str(fill.get("error") or status)
                self.remember_command(
                    command["id"], command["action"], "failed",
                    {"error": error, "broker_order_id": order_id},
                )
                self.acknowledge(command, False, {}, error)
            else:
                print(
                    f"⏳ Broker confirmation pending | command={command['id']} | "
                    f"order={order_id}"
                )
            return None

        if command["action"] == "PLACE_ENTRY":
            payload = saved.get("payload") or command.get("payload") or {}
            result = self.finalize_entry(payload, order_id, fill)
        elif command["action"] == "EXIT_POSITION":
            trade_id = int(saved.get("trade_id") or command["payload"]["trade_id"])
            position = self.db.execute(
                "SELECT * FROM local_positions WHERE trade_id=?",
                (trade_id,),
            ).fetchone()
            if not position:
                raise RuntimeError("Local position missing during exit reconciliation")
            result = self.finalize_exit(
                position,
                order_id,
                fill,
                saved.get("reason") or command["payload"].get("reason") or "EXIT FILLED",
            )
        else:
            raise RuntimeError(f"Unsupported submitted action: {command['action']}")

        self.remember_command(command["id"], command["action"], "succeeded", result)
        self.acknowledge(command, True, result)
        return result

    def process_command(self, command):
        cached = self.processed(command["id"])
        if cached:
            status = str(cached["status"] or "")
            if status == "succeeded":
                result = json.loads(cached["result_json"] or "{}")
                self.acknowledge(command, True, result)
                return
            if status == "submitted":
                self.reconcile_submitted(command, cached)
                return
            if status == "failed":
                saved = json.loads(cached["result_json"] or "{}")
                self.acknowledge(
                    command, False, {}, saved.get("error") or "Local execution failed"
                )
                return

        try:
            if command["action"] == "PLACE_ENTRY":
                result = self.execute_entry(command)
            elif command["action"] == "EXIT_POSITION":
                result = self.execute_exit(
                    command["payload"]["trade_id"],
                    command["payload"].get("reason") or "REMOTE MANUAL EXIT",
                    command=command,
                )
            else:
                raise RuntimeError(f"Unsupported gateway action: {command['action']}")
        except BrokerSubmittedPending as exc:
            print(f"⏳ {str(exc)}")
            return
        except Exception as exc:
            current = self.processed(command["id"])
            if current and current["status"] == "submitted":
                print(
                    f"⏳ Broker order already submitted; retry will reconcile | "
                    f"command={command['id']}"
                )
                return
            error = str(exc)[:500]
            if not current or current["status"] != "failed":
                self.remember_command(
                    command["id"], command["action"], "failed", {"error": error}
                )
            self.acknowledge(command, False, {}, error)
            print(f"❌ {command['action']} failed | {error}")
            return

        self.remember_command(command["id"], command["action"], "succeeded", result)
        self.acknowledge(command, True, result)
        print(
            f"✅ {command['action']} success | command={command['id']} | "
            f"trade={command.get('trade_id')}"
        )

'''
agent = agent[:start] + new_execution_block + agent[end:]

monitor_marker = "    def monitor_positions(self):\n"
run_marker = "    def run(self):\n"
monitor_start = agent.find(monitor_marker)
run_start = agent.find(run_marker, monitor_start)
if monitor_start < 0 or run_start < 0:
    raise RuntimeError("monitor/run boundaries not found")
monitor_segment = agent[monitor_start:run_start]
if "def monitor_pending_exits" not in monitor_segment:
    pending_method = r'''    def monitor_pending_exits(self):
        rows = self.db.execute(
            "SELECT * FROM local_positions WHERE status='exit_submitted' ORDER BY trade_id"
        ).fetchall()
        for position in rows:
            try:
                order_id = str(position["exit_order_id"] or "")
                if not order_id:
                    self.db.execute(
                        "UPDATE local_positions SET status='open' WHERE trade_id=?",
                        (position["trade_id"],),
                    )
                    self.db.commit()
                    continue
                fill = self.angel.confirm_order(
                    order_id,
                    float(position["last_ltp"] or position["entry_price"]),
                    timeout_seconds=2,
                )
                if fill.get("filled"):
                    self.finalize_exit(
                        position,
                        order_id,
                        fill,
                        position["exit_reason"] or "LOCAL EXIT FILLED",
                    )
                elif str(fill.get("status") or "").lower() in {
                    "rejected", "cancelled", "canceled"
                }:
                    self.db.execute(
                        "UPDATE local_positions SET status='open', exit_order_id=NULL WHERE trade_id=?",
                        (position["trade_id"],),
                    )
                    self.db.commit()
            except Exception as exc:
                print(
                    f"⚠️ Pending exit reconciliation warning | "
                    f"trade={position['trade_id']} | {str(exc)[:160]}"
                )

'''
    agent = agent[:run_start] + pending_method + agent[run_start:]

old_run_core = '''                self.monitor_positions()
                poll = self.saas.poll()
                for command in poll.get("commands") or []:
                    self.process_command(command)
'''
new_run_core = '''                self.monitor_positions()
                self.monitor_pending_exits()
                poll = self.saas.poll()
                server_armed = bool(poll.get("server_armed"))
                for command in poll.get("commands") or []:
                    if command.get("action") == "PLACE_ENTRY" and not server_armed:
                        self.acknowledge(
                            command,
                            False,
                            {},
                            "SERVER GATEWAY DISARMED — entry command rejected",
                        )
                        continue
                    self.process_command(command)
'''
if old_run_core not in agent:
    raise RuntimeError("agent run loop marker not found")
agent = agent.replace(old_run_core, new_run_core, 1)
agent_path.write_text(agent, encoding="utf-8")

print("Static gateway safety v2 patch applied")

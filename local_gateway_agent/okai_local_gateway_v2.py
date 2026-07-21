#!/usr/bin/env python3
"""OKAI static-IP gateway risk engine V2.

This wrapper keeps the battle-tested gateway command/Angel order flow and adds:
- a genuine one-lot hard cap at the local phone;
- use of the server-provided ATR stop instead of recalculating a fixed stop;
- first profit lock at entry + estimated round-trip charges + 2% profit;
- R-based dynamic profit trailing checked against option LTP every second;
- fixed target disabled; structural exits continue to arrive as remote exit commands;
- local SQLite migration, so open-position trail state survives restarts.
"""

import math
from datetime import datetime

import okai_local_gateway as base


RISK_ENGINE_VERSION = "1.1.0-RISK-V2"

# Conservative Angel One equity-option charge model. Rates are decimals.
# Brokerage is charged per executed order. Slight over-estimation is intentional:
# the first lock must remain net-profitable after the actual contract-note costs.
BROKERAGE_PER_ORDER = 20.0
NSE_OPTION_TRANSACTION_RATE = 0.000355299
BSE_SENSEX_OPTION_TRANSACTION_RATE = 0.000325
OPTION_STT_SELL_RATE = 0.0015
OPTION_STAMP_BUY_RATE = 0.00003
SEBI_RATE = 0.000001
GST_RATE = 0.18


def _ceil_tick(value, tick=0.05):
    return round(math.ceil(max(float(value or 0), tick) / tick - 1e-12) * tick, 2)


def estimate_round_trip_charges(entry_price, exit_price, quantity, exchange):
    qty = max(1, int(quantity or 1))
    buy_turnover = max(0.0, float(entry_price or 0)) * qty
    sell_turnover = max(0.0, float(exit_price or 0)) * qty
    turnover = buy_turnover + sell_turnover
    exchange_name = str(exchange or "NFO").upper()
    transaction_rate = (
        BSE_SENSEX_OPTION_TRANSACTION_RATE
        if exchange_name in {"BFO", "BSE"}
        else NSE_OPTION_TRANSACTION_RATE
    )
    brokerage = BROKERAGE_PER_ORDER * 2
    transaction = turnover * transaction_rate
    stt = sell_turnover * OPTION_STT_SELL_RATE
    stamp = buy_turnover * OPTION_STAMP_BUY_RATE
    sebi = turnover * SEBI_RATE
    gst = (brokerage + transaction + sebi) * GST_RATE
    total = brokerage + transaction + stt + stamp + sebi + gst
    return round(total, 2)


def cost_safe_breakeven(entry_price, quantity, exchange):
    entry = float(entry_price or 0)
    qty = max(1, int(quantity or 1))
    desired_exit = entry * 1.02
    charges = estimate_round_trip_charges(entry, desired_exit, qty, exchange)
    per_unit_charges = charges / qty
    return _ceil_tick(desired_exit + per_unit_charges), charges


def dynamic_profit_lock(entry_price, initial_sl, peak_ltp, cost_safe_be):
    entry = float(entry_price or 0)
    initial = float(initial_sl or 0)
    peak = max(entry, float(peak_ltp or entry))
    if initial <= 0 or initial >= entry:
        initial = entry * 0.88
    risk = max(0.05, entry - initial)
    peak_r = max(0.0, (peak - entry) / risk)
    active_sl = initial
    stage = "INITIAL_ATR_SL"

    if peak_r >= 0.8:
        active_sl = max(active_sl, float(cost_safe_be or entry))
        stage = "COST_SAFE_BE_PLUS_2PCT"
    if peak_r >= 1.2:
        active_sl = max(active_sl, entry + 0.5 * risk)
        stage = "LOCK_0_5R"
    if peak_r >= 1.8:
        active_sl = max(active_sl, entry + risk, peak - 0.8 * risk)
        stage = "DYNAMIC_TRAIL_0_8R"

    return {
        "sl_price": _ceil_tick(active_sl),
        "stage": stage,
        "risk": round(risk, 4),
        "peak_r": round(peak_r, 4),
    }


_original_state_db = base.state_db
_original_command_doctor = base.command_doctor


def migrated_state_db():
    conn = _original_state_db()
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(local_positions)").fetchall()
    }
    additions = {
        "initial_sl_price": "REAL",
        "peak_ltp": "REAL",
        "breakeven_price": "REAL",
        "trail_stage": "TEXT",
        "estimated_round_trip_charges": "REAL",
    }
    for name, sql_type in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE local_positions ADD COLUMN {name} {sql_type}")
    conn.commit()
    return conn


class RiskV2GatewayRunner(base.GatewayRunner):
    def save_position(self, payload, order_id, entry_price, sl_price, target_price):
        cost_be, charges = cost_safe_breakeven(
            entry_price,
            payload["quantity"],
            payload["exchange"],
        )
        self.db.execute(
            """
            INSERT OR REPLACE INTO local_positions(
                trade_id, symbol, symboltoken, exchange, option_type, quantity,
                entry_order_id, entry_price, sl_price, target_price,
                force_exit_at, status, opened_at, initial_sl_price, peak_ltp,
                breakeven_price, trail_stage, estimated_round_trip_charges
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
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
                0.0,
                str(payload.get("force_exit_at") or "15:25"),
                base.now_iso(),
                float(sl_price),
                float(entry_price),
                float(cost_be),
                "INITIAL_ATR_SL",
                float(charges),
            ),
        )
        self.db.commit()

    def finalize_entry(self, payload, order_id, fill):
        quantity = int(payload.get("quantity") or 0)
        expected = float(payload.get("expected_entry_price") or 0)
        entry_price = float(fill.get("price") or expected)
        if entry_price <= 0:
            entry_price = self.angel.ltp(
                payload["exchange"], payload["symbol"], payload["symboltoken"]
            )

        # Keep the ATR-derived server stop. Only fall back when payload is invalid.
        sl_price = float(payload.get("sl_price") or 0)
        if sl_price <= 0 or sl_price >= entry_price:
            sl_percent = max(1.0, float(payload.get("sl_percent") or 12))
            sl_price = round(entry_price * (1 - sl_percent / 100), 2)

        self.save_position(payload, order_id, entry_price, sl_price, 0.0)
        row = self.db.execute(
            "SELECT * FROM local_positions WHERE trade_id=?",
            (int(payload["trade_id"]),),
        ).fetchone()
        event = {
            "event": "ENTRY_FILLED",
            "trade_id": int(payload["trade_id"]),
            "broker_order_id": order_id,
            "entry_price": entry_price,
            "sl_price": float(row["sl_price"]),
            "target_price": 0.0,
            "cost_safe_breakeven": float(row["breakeven_price"]),
            "estimated_round_trip_charges": float(
                row["estimated_round_trip_charges"] or 0
            ),
            "risk_engine": RISK_ENGINE_VERSION,
            "entry_time": base.now_iso(),
        }
        self.notify_position_event(event)
        return {**event, "quantity": quantity}

    def execute_entry(self, command):
        payload = command.get("payload") or {}
        lot_size = int(payload.get("lot_size") or 0)
        if lot_size <= 0:
            raise RuntimeError("ONE_LOT_HARD_CAP: valid lot_size missing in payload")
        requested = int(payload.get("quantity") or 0)
        payload["quantity"] = lot_size
        payload["lots"] = 1
        command["payload"] = payload
        if requested != lot_size:
            print(
                f"🛡️ ONE LOT HARD CAP | requested={requested} | executing={lot_size}"
            )
        return super().execute_entry(command)

    def monitor_positions(self):
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
                trail = dynamic_profit_lock(entry, initial_sl, peak, cost_be)
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
                if base.time.time() - last_sent >= 15:
                    self.saas.position_event({
                        "event": "POSITION_HEARTBEAT",
                        "trade_id": int(position["trade_id"]),
                        "ltp": ltp,
                        "peak_ltp": peak,
                        "active_sl": active_sl,
                        "cost_safe_breakeven": cost_be,
                        "trail_stage": stage,
                        "peak_r": trail["peak_r"],
                        "risk_engine": RISK_ENGINE_VERSION,
                        "local_status": "open",
                    })
                    self.last_position_heartbeat[position["trade_id"]] = base.time.time()
            except Exception as exc:
                print(
                    f"⚠️ Position monitor warning | trade={position['trade_id']} | "
                    f"{str(exc)[:180]}"
                )


def command_doctor_v2():
    _original_command_doctor()
    conn = migrated_state_db()
    conn.close()
    print("=== LOCAL RISK ENGINE CHECK ===")
    print(f"Risk engine: {RISK_ENGINE_VERSION} ✅")
    print("One-lot hard cap: ENABLED ✅")
    print("Initial stop: SERVER ATR SL ✅")
    print("First lock: ENTRY + ROUND-TRIP CHARGES + 2% ✅")
    print("Dynamic trail: 0.8R / 1.2R / 1.8R stages ✅")
    print("Fixed target: DISABLED ✅")
    print("Structural exit: SERVER REMOTE EXIT COMMAND ✅")


def install_patches():
    base.AGENT_VERSION = RISK_ENGINE_VERSION
    base.state_db = migrated_state_db
    base.GatewayRunner = RiskV2GatewayRunner
    base.command_doctor = command_doctor_v2


def main():
    install_patches()
    base.main()


if __name__ == "__main__":
    main()

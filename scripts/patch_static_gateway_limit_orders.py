from pathlib import Path

path = Path("local_gateway_agent/okai_local_gateway.py")
text = path.read_text(encoding="utf-8")

if "import math\n" not in text:
    text = text.replace("import json\n", "import json\nimport math\n", 1)

start = text.find("    def place_market(")
end = text.find("    def confirm_order(", start)
if start < 0 or end < 0:
    raise RuntimeError("Angel place_market block not found")

replacement = '''    @staticmethod
    def protected_limit_price(reference_price, transaction, slippage_percent):
        reference = float(reference_price or 0)
        if reference <= 0:
            raise RuntimeError("Valid reference price is required for LIMIT order")
        side = str(transaction or "").upper()
        slippage = max(0.25, min(float(slippage_percent or 0), 10.0))
        raw_price = (
            reference * (1 + slippage / 100)
            if side == "BUY"
            else reference * (1 - slippage / 100)
        )
        ticks = raw_price / 0.05
        rounded_ticks = math.ceil(ticks) if side == "BUY" else math.floor(ticks)
        return round(max(0.05, rounded_ticks * 0.05), 2)

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
        side = str(transaction).upper()
        if side not in {"BUY", "SELL"}:
            raise RuntimeError("Transaction must be BUY or SELL")
        limit_price = self.protected_limit_price(
            reference_price,
            side,
            slippage_percent,
        )
        params = {
            "variety": "NORMAL",
            "tradingsymbol": str(symbol),
            "symboltoken": str(token),
            "transactiontype": side,
            "exchange": str(exchange).upper(),
            "ordertype": "LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": f"{limit_price:.2f}",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(int(quantity)),
            "ordertag": "OKAI_STATIC_IP"[:20],
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
                return {
                    "order_id": str(order_id),
                    "limit_price": limit_price,
                    "reference_price": round(float(reference_price), 2),
                    "slippage_percent": float(slippage_percent),
                    "order_type": "LIMIT",
                }
            except Exception as exc:
                last_error = exc
                self.obj = None
                if attempt == 0:
                    time.sleep(1)
        raise RuntimeError(f"Angel LIMIT placeOrder failed: {last_error}")

'''
text = text[:start] + replacement + text[end:]

old_entry = '''        expected = float(payload.get("expected_entry_price") or 0)
        order_id = self.angel.place_market(
            payload["exchange"], payload["symbol"], payload["symboltoken"],
            "BUY", quantity,
        )
'''
new_entry = '''        expected = float(payload.get("expected_entry_price") or 0)
        local_ltp = self.angel.ltp(
            payload["exchange"], payload["symbol"], payload["symboltoken"]
        )
        submission = self.angel.place_protected_limit(
            payload["exchange"], payload["symbol"], payload["symboltoken"],
            "BUY", quantity, local_ltp, 3.0,
        )
        order_id = submission["order_id"]
'''
if old_entry not in text:
    raise RuntimeError("Entry market-order call not found")
text = text.replace(old_entry, new_entry, 1)

old_entry_saved = '''                "fallback_price": expected,
            },
'''
new_entry_saved = '''                "fallback_price": local_ltp or expected,
                "limit_price": submission["limit_price"],
                "order_type": "LIMIT",
            },
'''
if old_entry_saved not in text:
    raise RuntimeError("Entry submitted payload marker not found")
text = text.replace(old_entry_saved, new_entry_saved, 1)
text = text.replace(
    "        fill = self.angel.confirm_order(order_id, expected)\n",
    "        fill = self.angel.confirm_order(order_id, local_ltp or expected)\n",
    1,
)

old_exit = '''            order_id = self.angel.place_market(
                position["exchange"], position["symbol"], position["symboltoken"],
                "SELL", position["quantity"],
            )
'''
new_exit = '''            local_ltp = self.angel.ltp(
                position["exchange"], position["symbol"], position["symboltoken"]
            )
            submission = self.angel.place_protected_limit(
                position["exchange"], position["symbol"], position["symboltoken"],
                "SELL", position["quantity"], local_ltp, 5.0,
            )
            order_id = submission["order_id"]
'''
if old_exit not in text:
    raise RuntimeError("Exit market-order call not found")
text = text.replace(old_exit, new_exit, 1)

old_exit_saved = '''                    "fallback_price": float(
                        position["last_ltp"] or position["entry_price"]
                    ),
'''
new_exit_saved = '''                    "fallback_price": float(
                        position["last_ltp"] or position["entry_price"]
                    ),
                    "limit_price": (
                        submission.get("limit_price")
                        if "submission" in locals()
                        else None
                    ),
                    "order_type": "LIMIT",
'''
if old_exit_saved not in text:
    raise RuntimeError("Exit submitted payload marker not found")
text = text.replace(old_exit_saved, new_exit_saved, 1)

path.write_text(text, encoding="utf-8")
print("Static-IP gateway converted to protected LIMIT orders")

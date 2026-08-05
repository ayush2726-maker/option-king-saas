from pathlib import Path


RUNTIME = Path("bot/auto_portfolio_runtime.py")
ROUTES = Path("bot/routes.py")
TEST = Path("tests/test_lot_aware_capital_entry_v5.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old in text:
        return text.replace(old, new, 1)
    if new in text:
        return text
    raise SystemExit(f"Patch anchor not found: {label}")


runtime = RUNTIME.read_text(encoding="utf-8")

old_size = '''def _size(capital_base, slot, premium, lot_size):
    lot_size = max(1, _i(lot_size, 1))
    premium = max(0.0, _f(premium, 0))
    budget = max(0.0, capital_base * SLOT_ALLOCATIONS.get(slot, 0))
    one_lot = premium * lot_size

    lots = int(math.floor(budget / one_lot)) if one_lot > 0 else 0
    qty = lots * lot_size
    return {
        "lot_size": lot_size,
        "lots": lots,
        "qty": qty,
        "slot_budget": round(budget, 2),
        "capital_used": round(premium * qty, 2),
    }
'''
new_size = '''def _row_capital_used(row):
    saved = _f(_v(row, "capital_used", 0), 0)
    if saved > 0:
        return saved
    return max(
        0.0,
        _f(_v(row, "entry_price", 0), 0)
        * max(0, _i(_v(row, "qty", 0), 0)),
    )


def _size(capital_base, slot, premium, lot_size, rows=None):
    """Lot-aware sizing with a hard 10% reserve.

    Slot 1/2 percentages are target allocations, not a reason to reject an
    otherwise affordable complete exchange lot. When one lot is above the
    target slot budget but still fits after preserving the reserve and existing
    positions, allow exactly that lot. Never create fractional lots.
    """
    lot_size = max(1, _i(lot_size, 1))
    premium = max(0.0, _f(premium, 0))
    capital_base = max(0.0, _f(capital_base, 0))
    target_budget = max(0.0, capital_base * SLOT_ALLOCATIONS.get(slot, 0))
    reserve_floor = max(0.0, capital_base * RESERVE_ALLOCATION)
    committed = sum(_row_capital_used(row) for row in (rows or []))
    available_after_reserve = max(0.0, capital_base - reserve_floor - committed)
    one_lot = premium * lot_size

    flex_used = bool(
        one_lot > target_budget + 1e-9
        and one_lot <= available_after_reserve + 1e-9
    )
    if flex_used:
        # Permit the minimum complete lot only; do not consume the whole
        # remaining portfolio merely because the target slot was too small.
        budget = one_lot
        sizing_mode = "LOT_AWARE_FLEX_ONE_LOT"
    else:
        budget = min(target_budget, available_after_reserve)
        sizing_mode = "TARGET_SLOT_BUDGET"

    lots = int(math.floor((budget + 1e-9) / one_lot)) if one_lot > 0 else 0
    qty = lots * lot_size
    capital_used = premium * qty
    actual_pct = (
        capital_used / capital_base * 100.0
        if capital_base > 0
        else 0.0
    )
    return {
        "lot_size": lot_size,
        "lots": lots,
        "qty": qty,
        "target_slot_budget": round(target_budget, 2),
        "slot_budget": round(budget, 2),
        "reserve_floor": round(reserve_floor, 2),
        "committed_capital": round(committed, 2),
        "available_after_reserve": round(available_after_reserve, 2),
        "one_lot_cost": round(one_lot, 2),
        "capital_used": round(capital_used, 2),
        "actual_allocation_pct": round(actual_pct, 2),
        "flex_used": flex_used,
        "sizing_mode": sizing_mode,
    }
'''
runtime = replace_once(runtime, old_size, new_size, "_size")

runtime = replace_once(
    runtime,
    '    sizing = _size(capital_base, slot, quote_price, lot_size)\n',
    '    sizing = _size(capital_base, slot, quote_price, lot_size, rows=rows)\n    state["entry_sizing"] = dict(sizing)\n',
    "_size call",
)

runtime = replace_once(
    runtime,
    '''        state["position_size_block"] = {
            "slot": slot,
            "slot_budget": sizing["slot_budget"],
            "one_lot_cost": round(quote_price * sizing["lot_size"], 2),
            "reason": "Slot budget one lot se kam hai",
        }
''',
    '''        state["position_size_block"] = {
            "slot": slot,
            "slot_budget": sizing["slot_budget"],
            "target_slot_budget": sizing.get("target_slot_budget"),
            "available_after_reserve": sizing.get("available_after_reserve"),
            "reserve_floor": sizing.get("reserve_floor"),
            "committed_capital": sizing.get("committed_capital"),
            "one_lot_cost": sizing.get("one_lot_cost"),
            "capital_base": round(capital_base, 2),
            "reason": "10% reserve bachane ke baad ek complete lot afford nahi hota",
        }
''',
    "position size diagnostics",
)

runtime = replace_once(
    runtime,
    '            SLOT_ALLOCATIONS[slot] * 100,\n',
    '            sizing.get("actual_allocation_pct", SLOT_ALLOCATIONS[slot] * 100),\n',
    "stored allocation percent",
)

runtime = replace_once(
    runtime,
    '        "allocation_percent": int(SLOT_ALLOCATIONS[slot] * 100),\n',
    '        "allocation_percent": sizing.get("actual_allocation_pct", int(SLOT_ALLOCATIONS[slot] * 100)),\n        "sizing_mode": sizing.get("sizing_mode"),\n        "flex_used": bool(sizing.get("flex_used")),\n',
    "last opened trade allocation",
)

runtime = replace_once(
    runtime,
    '''                "different_index_required": True,
            },
''',
    '''                "different_index_required": True,
                "lot_aware_flex": True,
                "allocation_note": "50/40 target; complete lot can flex while 10% reserve stays protected",
            },
''',
    "capital plan",
)

RUNTIME.write_text(runtime, encoding="utf-8")

routes = ROUTES.read_text(encoding="utf-8")
routes = replace_once(
    routes,
    '''        "entry_block_reason": (
            engine_state.get("entry_block_reason")
            or engine_state.get("last_entry_block_reason")
            if is_running
            else None
        ),
        "message": "Signal view-only. App refresh trade create/exit nahi karega. Button se trade start/close hoga."
''',
    '''        "entry_block_reason": (
            engine_state.get("entry_block_reason")
            or engine_state.get("last_entry_block_reason")
            if is_running
            else None
        ),
        "entry_permission": (
            engine_state.get("entry_permission")
            if is_running
            else None
        ),
        "entry_sizing": (
            engine_state.get("entry_sizing")
            if is_running
            else None
        ),
        "position_size_block": (
            engine_state.get("position_size_block")
            if is_running
            else None
        ),
        "entry_candidate_attempts": (
            engine_state.get("entry_candidate_attempts", [])
            if is_running
            else []
        ),
        "message": "Signal view-only. App refresh trade create/exit nahi karega. Button se trade start/close hoga."
''',
    "route diagnostics",
)
ROUTES.write_text(routes, encoding="utf-8")

TEST.write_text(
    '''import sqlite3\n\nfrom bot import auto_portfolio_runtime as runtime\n\n\ndef _row(**values):\n    class Row(dict):\n        def keys(self):\n            return super().keys()\n    return Row(values)\n\n\ndef test_small_capital_can_take_one_complete_lot_with_reserve():\n    result = runtime._size(10000, 1, 200, 30, rows=[])\n    assert result["one_lot_cost"] == 6000\n    assert result["target_slot_budget"] == 5000\n    assert result["reserve_floor"] == 1000\n    assert result["lots"] == 1\n    assert result["qty"] == 30\n    assert result["flex_used"] is True\n    assert result["sizing_mode"] == "LOT_AWARE_FLEX_ONE_LOT"\n\n\ndef test_reserve_is_never_breached():\n    result = runtime._size(10000, 1, 310, 30, rows=[])\n    assert result["one_lot_cost"] == 9300\n    assert result["available_after_reserve"] == 9000\n    assert result["lots"] == 0\n    assert result["flex_used"] is False\n\n\ndef test_second_slot_uses_only_remaining_capital_above_reserve():\n    existing = _row(capital_used=6000, entry_price=200, qty=30)\n    result = runtime._size(10000, 2, 200, 30, rows=[existing])\n    assert result["committed_capital"] == 6000\n    assert result["available_after_reserve"] == 3000\n    assert result["lots"] == 0\n\n\ndef test_large_capital_keeps_normal_target_slot_sizing():\n    result = runtime._size(100000, 1, 200, 30, rows=[])\n    assert result["target_slot_budget"] == 50000\n    assert result["flex_used"] is False\n    assert result["lots"] == 8\n    assert result["capital_used"] == 48000\n\n\ndef test_route_exposes_sizing_diagnostics():\n    source = open("bot/routes.py", encoding="utf-8").read()\n    assert '"entry_sizing": (' in source\n    assert '"position_size_block": (' in source\n    assert '"entry_candidate_attempts": (' in source\n''',
    encoding="utf-8",
)

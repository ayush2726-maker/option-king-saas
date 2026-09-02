"""Final capital ceiling plus planned-stop risk sizing.

PAPER sizing follows the active capital cycle:
- Set Capital starts a new cycle from that exact amount.
- Closed PAPER profit/loss after that reset changes the next trade sizing base.
- Historical profit/loss from before the latest capital reset remains visible in
  history but does not affect the new cycle.
- LIVE sizing remains broker-funded.
"""

import math
from datetime import datetime

from fastapi import Header

from backtest import routes as backtest_routes
from backtest.range_capital_mode_patch import apply_range_capital_mode_patch
from bot import auto_portfolio_runtime as runtime
from bot.authoritative_ledger import build_authoritative_ledger
from database import get_db
from user_panel import routes as user_panel_routes
from strategy import routes as strategy_routes


NORMAL_MAX_PLANNED_LOSS_PERCENT = 10.0
BACKTEST_CONSERVATIVE_PREMIUM_RISK_PERCENT = 15.0
RESET_KEY = "paper_capital_reset_at"
CARRY_KEY = "paper_capital_carry_forward"


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def _cycle_paper_sizing_base(conn, user_id, settings):
    """Use configured capital plus CLOSED P&L since the latest capital reset."""
    from bot.capital_continuity_patch import _paper_summary

    summary = _paper_summary(conn, int(user_id), dict(settings or {}))
    return max(1.0, _f(summary.get("equity"), 1.0))


def _configured_paper_sizing_base(conn, user_id, settings):
    return _cycle_paper_sizing_base(conn, user_id, settings)


def _runtime_capital_size(capital_base, slot, premium, lot_size, rows=None, risk_points=None):
    capital = max(0.0, _f(capital_base))
    price = max(0.0, _f(premium))
    lot = max(1, _i(lot_size, 1))
    slot_number = _i(slot, 1)
    allocation = float(runtime.SLOT_ALLOCATIONS.get(slot_number, 0.0))
    target_budget = max(0.0, capital * allocation)
    reserve_floor = max(0.0, capital * runtime.RESERVE_ALLOCATION)
    committed = sum(runtime._row_capital_used(row) for row in (rows or []))
    available_after_reserve = max(0.0, capital - reserve_floor - committed)
    one_lot_cost = price * lot
    flex_used = bool(slot_number == 3 and one_lot_cost > 0 and one_lot_cost <= available_after_reserve + 1e-9)
    budget = one_lot_cost if flex_used else min(target_budget, available_after_reserve)
    affordability_lots = int(math.floor(budget / one_lot_cost)) if one_lot_cost > 0 else 0
    planned_risk = max(0.05, _f(risk_points, 0.05)) if risk_points is not None else None
    risk_budget = capital * NORMAL_MAX_PLANNED_LOSS_PERCENT / 100.0
    planned_risk_per_lot = planned_risk * lot if planned_risk is not None else None
    risk_lots = int(math.floor((risk_budget + 1e-9) / planned_risk_per_lot)) if planned_risk_per_lot else affordability_lots
    lots = min(affordability_lots, max(0, risk_lots))
    qty = lots * lot
    capital_used = round(price * qty, 2)
    return {
        "lot_size": lot, "lots": lots, "qty": qty,
        "target_slot_budget": round(target_budget, 2), "slot_budget": round(budget, 2),
        "usable_capital": round(budget, 2), "reserve_floor": round(reserve_floor, 2),
        "committed_capital": round(committed, 2), "available_after_reserve": round(available_after_reserve, 2),
        "one_lot_cost": round(one_lot_cost, 2), "capital_used": capital_used,
        "capital_left_in_slot": round(max(0.0, budget - capital_used), 2),
        "allocation_percent": round(allocation * 100.0, 2),
        "actual_allocation_pct": round(capital_used / capital * 100.0 if capital > 0 else 0.0, 2),
        "flex_used": flex_used,
        "sizing_mode": "REMAINDER_SLOT_ONE_LOT" if flex_used else "CAPITAL_BASED_ALLOCATION",
        "risk_cap_applied": lots < affordability_lots,
        "risk_sizing_mode": "NORMAL_PLANNED_SL_LOSS_CAP_10PCT" if planned_risk is not None else "CAPITAL_BASED_ALLOCATION_NO_RISK_CONTEXT",
        "quantity_sizing_rule": "MIN_CAPITAL_ALLOCATION_AND_10PCT_PLANNED_SL_RISK" if planned_risk is not None else "FLOOR_ALLOCATION_DIVIDED_BY_PREMIUM_AND_LOT",
        "max_planned_loss_percent": NORMAL_MAX_PLANNED_LOSS_PERCENT if planned_risk is not None else None,
        "max_planned_loss_amount": round(risk_budget, 2) if planned_risk is not None else None,
        "planned_risk_points": round(planned_risk, 2) if planned_risk is not None else None,
        "planned_risk_per_lot": round(planned_risk_per_lot, 2) if planned_risk_per_lot is not None else None,
        "affordability_lots": affordability_lots, "risk_lots": risk_lots,
        "capital_base": round(capital, 2),
        "capital_base_source": "PAPER_RESET_CYCLE_EQUITY_OR_LIVE_BROKER_BASE",
    }


def _backtest_capital_size(capital, premium, lot_size, allocation):
    capital_value = max(0.0, _f(capital)); price = max(0.0, _f(premium)); lot = max(1, _i(lot_size, 1))
    allocation_value = max(0.0, min(1.0, _f(allocation))); budget = capital_value * allocation_value; one_lot_cost = price * lot
    affordability_lots = int(math.floor(budget / one_lot_cost)) if one_lot_cost > 0 else 0
    risk_budget = capital_value * NORMAL_MAX_PLANNED_LOSS_PERCENT / 100.0
    planned_risk_points = price * BACKTEST_CONSERVATIVE_PREMIUM_RISK_PERCENT / 100.0
    planned_risk_per_lot = planned_risk_points * lot
    risk_lots = int(math.floor((risk_budget + 1e-9) / planned_risk_per_lot)) if planned_risk_per_lot > 0 else 0
    lots = min(affordability_lots, max(0, risk_lots)); quantity = lots * lot; capital_used = round(price * quantity, 2)
    return {"lots": lots, "quantity": quantity, "qty": quantity, "lot_size": lot, "allocation": allocation_value,
            "allocation_percent": round(allocation_value * 100.0, 2), "allocated_capital": round(budget, 2),
            "usable_capital": round(budget, 2), "one_lot_cost": round(one_lot_cost, 2), "capital_used": capital_used,
            "used_capital": capital_used, "capital_left": round(max(0.0, budget - capital_used), 2),
            "capital_utilization_percent": round(capital_used / max(0.01, capital_value) * 100.0, 2),
            "slot_utilization_percent": round(capital_used / max(0.01, budget) * 100.0, 2) if budget > 0 else 0.0,
            "affordable": lots >= 1, "risk_cap_applied": lots < affordability_lots, "quantity_risk_cap_enabled": True,
            "quantity_preserved": lots == affordability_lots, "risk_sizing_mode": "CONSERVATIVE_PLANNED_SL_LOSS_CAP_10PCT",
            "quantity_sizing_rule": "MIN_ALLOCATION_AND_10PCT_PLANNED_SL_RISK", "max_planned_loss_percent": NORMAL_MAX_PLANNED_LOSS_PERCENT,
            "max_planned_loss_amount": round(risk_budget, 2), "planned_risk_points": round(planned_risk_points, 2),
            "planned_risk_per_lot": round(planned_risk_per_lot, 2), "affordability_lots": affordability_lots, "risk_lots": risk_lots}


def _annotate_result(result):
    if not isinstance(result, dict): return result
    output = result; metadata = {"quantity_risk_cap_enabled": True, "position_sizing_mode": "CAPITAL_CEILING_PLUS_10PCT_PLANNED_SL_RISK", "capital_use_rule": "MIN_ALLOCATION_AND_10PCT_PLANNED_SL_RISK"}
    output.update(metadata); sizing = dict(output.get("position_sizing") or {}); sizing.update({"mode": "CAPITAL_CEILING_PLUS_10PCT_PLANNED_SL_RISK", "slot_1_allocation_percent": 50, "slot_2_allocation_percent": 40, "auto_backtest_capital_use_percent": 90, "equity_risk_lot_cap": True, "max_planned_loss_percent": NORMAL_MAX_PLANNED_LOSS_PERCENT}); output["position_sizing"] = sizing
    summary = dict(output.get("summary") or {}); summary.update(metadata); output["summary"] = summary
    output["note"] = "Quantity uses active capital-cycle equity and is additionally capped so planned stop loss is at most 10% of that sizing capital."
    return output


def _replace_route(router, path, method, endpoint):
    for route in getattr(router, "routes", []):
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            route.endpoint = endpoint
            try: route.dependant.call = endpoint
            except Exception: pass


def _install_current_capital_profile_fields():
    if getattr(user_panel_routes, "_okai_current_capital_profile_v2", False): return
    original = user_panel_routes.user_profile
    def user_profile_with_current_capital(authorization: str = Header(None)):
        result = original(authorization)
        if not isinstance(result, dict) or not isinstance(result.get("profile"), dict): return result
        profile = result["profile"]; user_id = _i(profile.get("id"), 0)
        if user_id <= 0: return result
        conn = get_db()
        try:
            settings = user_panel_routes.load_settings(conn, user_id); ledger = build_authoritative_ledger(conn, user_id, settings)
            mode = str(settings.get("trading_mode", "paper")).lower()
            sizing_capital = _cycle_paper_sizing_base(conn, user_id, settings) if mode == "paper" else ledger.get("starting_capital")
            current_capital = ledger.get("current_capital")
            if mode == "live" and current_capital is None: current_capital = ledger.get("broker_total_limit")
            profile.update({"starting_capital": ledger.get("starting_capital"), "current_capital": current_capital, "current_equity": current_capital,
                "open_pnl": ledger.get("open_pnl"), "realized_pnl": ledger.get("realized_pnl"), "capital_source": ledger.get("capital_source"),
                "paper_sizing_capital": sizing_capital if mode == "paper" else None, "sizing_capital": sizing_capital,
                "sizing_capital_source": "PAPER_RESET_CYCLE_EQUITY" if mode == "paper" else "LIVE_BROKER_CAPITAL",
                "broker_available_cash": ledger.get("broker_available_cash"), "broker_used_margin": ledger.get("broker_used_margin"),
                "broker_total_limit": ledger.get("broker_total_limit"), "broker_funds_updated_at": ledger.get("broker_funds_updated_at"),
                "broker_funds_age_seconds": ledger.get("broker_funds_age_seconds")})
        except Exception as exc:
            profile.setdefault("current_capital", None); profile.setdefault("current_equity", None); profile["capital_status_error"] = str(exc)[:160]
        finally: conn.close()
        return result
    user_panel_routes.user_profile = user_profile_with_current_capital
    _replace_route(user_panel_routes.router, "/user/profile", "GET", user_profile_with_current_capital)
    user_panel_routes._okai_current_capital_profile_v2 = True


def _install_capital_change_resets_cycle():
    if getattr(strategy_routes, "_okai_paper_capital_cycle_reset_v1", False): return
    original = strategy_routes.save_settings
    def save_settings_with_capital_cycle_reset(body: dict, authorization: str = Header(None)):
        user = strategy_routes.get_current_user(authorization); conn = get_db()
        try:
            old_settings = strategy_routes.ensure_settings(conn, int(user["id"])); old_capital = _f(old_settings.get("paper_capital", 100000), 100000)
        finally: conn.close()
        result = original(body, authorization)
        if not isinstance(result, dict) or not result.get("success"): return result
        saved = dict(result.get("settings") or {}); new_capital = _f(saved.get("paper_capital", old_capital), old_capital)
        if abs(new_capital - old_capital) <= 1e-9: return result
        now = datetime.utcnow().isoformat(); saved[RESET_KEY] = now; saved[CARRY_KEY] = True
        conn = get_db()
        try:
            conn.execute("INSERT OR REPLACE INTO strategy_settings (user_id, settings_json, updated_at) VALUES (?, ?, ?)", (int(user["id"]), strategy_routes.json.dumps(saved), now)); conn.commit()
        finally: conn.close()
        result["settings"] = saved; result["paper_capital_cycle_reset"] = True; result["paper_capital_reset_at"] = now; result["message"] = "Strategy settings saved; PAPER capital cycle restarted"
        return result
    strategy_routes.save_settings = save_settings_with_capital_cycle_reset
    _replace_route(strategy_routes.router, "/strategy/settings", "POST", save_settings_with_capital_cycle_reset)
    strategy_routes._okai_paper_capital_cycle_reset_v1 = True


def apply_capital_based_sizing_restore_patch():
    runtime._size = _runtime_capital_size; runtime._paper_base = _cycle_paper_sizing_base; runtime._okai_paper_sizing_source = "PAPER_RESET_CYCLE_EQUITY"; runtime._okai_risk_sizing_v2 = True; runtime._okai_capital_based_sizing_final = True
    backtest_routes._okai_calculate_lot_sizing = _backtest_capital_size; backtest_routes._okai_risk_sizing_v2 = True; backtest_routes._okai_capital_based_sizing_final = True
    original_auto = getattr(backtest_routes, "_okai_run_auto_index_backtest", None)
    if callable(original_auto) and not getattr(backtest_routes, "_okai_capital_sizing_result_annotation_v1", False):
        def capital_annotated_auto(*args, **kwargs): return _annotate_result(original_auto(*args, **kwargs))
        backtest_routes._okai_run_auto_index_backtest = capital_annotated_auto; backtest_routes._okai_capital_sizing_result_annotation_v1 = True
    _install_current_capital_profile_fields(); _install_capital_change_resets_cycle(); apply_range_capital_mode_patch()

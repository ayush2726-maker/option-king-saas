"""Final capital ceiling plus planned-stop risk sizing.
PAPER uses active reset-cycle equity; LIVE remains broker-funded.
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
NORMAL_MAX_PLANNED_LOSS_PERCENT=10.0;BACKTEST_CONSERVATIVE_PREMIUM_RISK_PERCENT=15.0;RESET_KEY="paper_capital_reset_at";CARRY_KEY="paper_capital_carry_forward"
def _f(v,d=0.0):
    try:return float(v)
    except Exception:return float(d)
def _i(v,d=0):
    try:return int(v)
    except Exception:return int(d)
def _cycle_paper_sizing_base(c,u,s):
    from bot.capital_continuity_patch import _paper_summary
    return max(1.0,_f(_paper_summary(c,int(u),dict(s or {})).get("equity"),1.0))
def _configured_paper_sizing_base(c,u,s):return _cycle_paper_sizing_base(c,u,s)
def _runtime_capital_size(capital_base,slot,premium,lot_size,rows=None,risk_points=None):
    capital=max(0.0,_f(capital_base));price=max(0.0,_f(premium));lot=max(1,_i(lot_size,1));slot_number=_i(slot,1);allocation=float(runtime.SLOT_ALLOCATIONS.get(slot_number,0.0));target=max(0.0,capital*allocation);reserve=max(0.0,capital*runtime.RESERVE_ALLOCATION);committed=sum(runtime._row_capital_used(r) for r in (rows or []));available=max(0.0,capital-reserve-committed);one=price*lot;flex=bool(slot_number==3 and one>0 and one<=available+1e-9);budget=one if flex else min(target,available);aff=int(math.floor(budget/one)) if one>0 else 0;pr=max(0.05,_f(risk_points,0.05)) if risk_points is not None else None;rb=capital*NORMAL_MAX_PLANNED_LOSS_PERCENT/100.0;prl=pr*lot if pr is not None else None;rl=int(math.floor((rb+1e-9)/prl)) if prl else aff;lots=min(aff,max(0,rl));qty=lots*lot;used=round(price*qty,2)
    return {"lot_size":lot,"lots":lots,"qty":qty,"target_slot_budget":round(target,2),"slot_budget":round(budget,2),"usable_capital":round(budget,2),"reserve_floor":round(reserve,2),"committed_capital":round(committed,2),"available_after_reserve":round(available,2),"one_lot_cost":round(one,2),"capital_used":used,"capital_left_in_slot":round(max(0.0,budget-used),2),"allocation_percent":round(allocation*100,2),"actual_allocation_pct":round(used/capital*100 if capital>0 else 0,2),"flex_used":flex,"sizing_mode":"REMAINDER_SLOT_ONE_LOT" if flex else "CAPITAL_BASED_ALLOCATION","risk_cap_applied":lots<aff,"risk_sizing_mode":"NORMAL_PLANNED_SL_LOSS_CAP_10PCT" if pr is not None else "CAPITAL_BASED_ALLOCATION_NO_RISK_CONTEXT","quantity_sizing_rule":"MIN_CAPITAL_ALLOCATION_AND_10PCT_PLANNED_SL_RISK" if pr is not None else "FLOOR_ALLOCATION_DIVIDED_BY_PREMIUM_AND_LOT","max_planned_loss_percent":NORMAL_MAX_PLANNED_LOSS_PERCENT if pr is not None else None,"max_planned_loss_amount":round(rb,2) if pr is not None else None,"planned_risk_points":round(pr,2) if pr is not None else None,"planned_risk_per_lot":round(prl,2) if prl is not None else None,"affordability_lots":aff,"risk_lots":rl,"capital_base":round(capital,2),"capital_base_source":"PAPER_RESET_CYCLE_EQUITY_OR_LIVE_BROKER_BASE"}
def _backtest_capital_size(capital,premium,lot_size,allocation):
    cv=max(0.0,_f(capital));p=max(0.0,_f(premium));lot=max(1,_i(lot_size,1));a=max(0.0,min(1.0,_f(allocation)));budget=cv*a;one=p*lot;aff=int(math.floor(budget/one)) if one>0 else 0;rb=cv*NORMAL_MAX_PLANNED_LOSS_PERCENT/100.0;prp=p*BACKTEST_CONSERVATIVE_PREMIUM_RISK_PERCENT/100.0;prl=prp*lot;rl=int(math.floor((rb+1e-9)/prl)) if prl>0 else 0;lots=min(aff,max(0,rl));q=lots*lot;used=round(p*q,2)
    return {"lots":lots,"quantity":q,"qty":q,"lot_size":lot,"allocation":a,"allocation_percent":round(a*100,2),"allocated_capital":round(budget,2),"usable_capital":round(budget,2),"one_lot_cost":round(one,2),"capital_used":used,"used_capital":used,"capital_left":round(max(0,budget-used),2),"capital_utilization_percent":round(used/max(.01,cv)*100,2),"slot_utilization_percent":round(used/max(.01,budget)*100,2) if budget>0 else 0,"affordable":lots>=1,"risk_cap_applied":lots<aff,"quantity_risk_cap_enabled":True,"quantity_preserved":lots==aff,"risk_sizing_mode":"CONSERVATIVE_PLANNED_SL_LOSS_CAP_10PCT","quantity_sizing_rule":"MIN_ALLOCATION_AND_10PCT_PLANNED_SL_RISK","max_planned_loss_percent":NORMAL_MAX_PLANNED_LOSS_PERCENT,"max_planned_loss_amount":round(rb,2),"planned_risk_points":round(prp,2),"planned_risk_per_lot":round(prl,2),"affordability_lots":aff,"risk_lots":rl}
def _annotate_result(r):
    if not isinstance(r,dict):return r
    r["quantity_risk_cap_enabled"]=True;return r
def _replace_route(router,path,method,endpoint):
    for r in getattr(router,"routes",[]):
        if getattr(r,"path",None)==path and method in getattr(r,"methods",set()):
            r.endpoint=endpoint
            try:r.dependant.call=endpoint
            except Exception:pass
def _install_current_capital_profile_fields():
    if getattr(user_panel_routes,"_okai_current_capital_profile_v2",False):return
    original=user_panel_routes.user_profile
    def wrapped(authorization:str=Header(None)):
        result=original(authorization)
        if not isinstance(result,dict) or not isinstance(result.get("profile"),dict):return result
        profile=result["profile"];uid=_i(profile.get("id"),0)
        if uid<=0:return result
        c=get_db()
        try:
            s=user_panel_routes.load_settings(c,uid);l=build_authoritative_ledger(c,uid,s);mode=str(s.get("trading_mode","paper")).lower();sizing=_cycle_paper_sizing_base(c,uid,s) if mode=="paper" else l.get("starting_capital");current=l.get("current_capital")
            if mode=="live" and current is None:current=l.get("broker_total_limit")
            profile.update({"starting_capital":l.get("starting_capital"),"current_capital":current,"current_equity":current,"open_pnl":l.get("open_pnl"),"realized_pnl":l.get("realized_pnl"),"capital_source":l.get("capital_source"),"paper_sizing_capital":sizing if mode=="paper" else None,"sizing_capital":sizing,"sizing_capital_source":"PAPER_RESET_CYCLE_EQUITY" if mode=="paper" else "LIVE_BROKER_CAPITAL","broker_available_cash":l.get("broker_available_cash"),"broker_used_margin":l.get("broker_used_margin"),"broker_total_limit":l.get("broker_total_limit"),"broker_funds_updated_at":l.get("broker_funds_updated_at"),"broker_funds_age_seconds":l.get("broker_funds_age_seconds")})
        except Exception as e:profile.setdefault("current_capital",None);profile["capital_status_error"]=str(e)[:160]
        finally:c.close()
        return result
    user_panel_routes.user_profile=wrapped;_replace_route(user_panel_routes.router,"/user/profile","GET",wrapped);user_panel_routes._okai_current_capital_profile_v2=True
def _install_capital_change_resets_cycle():
    if getattr(strategy_routes,"_okai_paper_capital_cycle_reset_v1",False):return
    original=strategy_routes.save_settings
    def wrapped(body:dict,authorization:str=Header(None)):
        u=strategy_routes.get_current_user(authorization);c=get_db()
        try:old=_f(strategy_routes.ensure_settings(c,int(u["id"])).get("paper_capital",100000),100000)
        finally:c.close()
        result=original(body,authorization)
        if not isinstance(result,dict) or not result.get("success"):return result
        saved=dict(result.get("settings") or {});new=_f(saved.get("paper_capital",old),old)
        if abs(new-old)<=1e-9:return result
        now=datetime.utcnow().isoformat();saved[RESET_KEY]=now;saved[CARRY_KEY]=True;c=get_db()
        try:c.execute("INSERT OR REPLACE INTO strategy_settings(user_id,settings_json,updated_at) VALUES(?,?,?)",(int(u["id"]),strategy_routes.json.dumps(saved),now));c.commit()
        finally:c.close()
        result["settings"]=saved;result["paper_capital_cycle_reset"]=True;result["paper_capital_reset_at"]=now;return result
    strategy_routes.save_settings=wrapped;_replace_route(strategy_routes.router,"/strategy/settings","POST",wrapped);strategy_routes._okai_paper_capital_cycle_reset_v1=True
def apply_capital_based_sizing_restore_patch():
    runtime._size=_runtime_capital_size;runtime._paper_base=_cycle_paper_sizing_base;runtime._okai_paper_sizing_source="PAPER_RESET_CYCLE_EQUITY";runtime._okai_risk_sizing_v2=True;runtime._okai_capital_based_sizing_final=True;backtest_routes._okai_calculate_lot_sizing=_backtest_capital_size;backtest_routes._okai_risk_sizing_v2=True;backtest_routes._okai_capital_based_sizing_final=True;_install_current_capital_profile_fields();_install_capital_change_resets_cycle();apply_range_capital_mode_patch()

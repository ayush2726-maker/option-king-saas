"""Keep PAPER equity continuous inside each user-defined capital cycle.

PAPER sizing/current capital = configured seed + cumulative CLOSED net P&L after
the latest explicit capital reset. Changing Set Capital starts a new cycle from
that exact amount, so older profit/loss remains visible in trade/history reports
but no longer affects future quantity. LIVE mode remains isolated and broker-funded.
"""

from __future__ import annotations
from datetime import datetime
from fastapi import Header
from database import get_db
from bot import auto_portfolio_runtime as runtime
from paper import routes as paper_routes

RESET_KEY = "paper_capital_reset_at"
CARRY_KEY = "paper_capital_carry_forward"
_INSTALLED = False

def _f(value, default=0.0):
    try: return float(value)
    except Exception: return float(default)

def _has_column(conn, table, column):
    try: return any(str(row["name"]) == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())
    except Exception: return False

def _paper_summary(conn, user_id, settings):
    seed=max(1.0,_f(settings.get("paper_capital",100000),100000)); reset_at=str(settings.get(RESET_KEY) or "").strip(); pnl_expression="COALESCE(net_pnl, pnl, 0)" if _has_column(conn,"paper_trades","net_pnl") else "COALESCE(pnl, 0)"; where=["user_id=?","status='CLOSED'"]
    if _has_column(conn,"paper_trades","trading_mode"): where.append("COALESCE(trading_mode, 'paper')='paper'")
    params=[int(user_id)]
    if reset_at: where.append("datetime(created_at) >= datetime(?)"); params.append(reset_at)
    row=conn.execute(f"SELECT COALESCE(SUM({pnl_expression}),0) AS net_pnl, COUNT(*) AS closed_trades FROM paper_trades WHERE {' AND '.join(where)}",tuple(params)).fetchone(); pnl=round(_f(row["net_pnl"] if row else 0),2); equity=max(1.0,round(seed+pnl,2))
    return {"seed_capital":round(seed,2),"cumulative_net_pnl":pnl,"equity":equity,"closed_trades":int(row["closed_trades"] if row else 0),"reset_at":reset_at or None}

def _continuous_paper_base(conn,user_id,settings): return _paper_summary(conn,int(user_id),settings)["equity"]

def _replace_route(router,path,method,endpoint):
    for route in getattr(router,"routes",[]):
        if getattr(route,"path",None)==path and method in getattr(route,"methods",set()):
            route.endpoint=endpoint
            try: route.dependant.call=endpoint
            except Exception: pass

def _continuous_paper_account(authorization: str = Header(None)):
    user=paper_routes.get_current_user(authorization); conn=get_db()
    try: settings=paper_routes.load_settings(conn,int(user["id"])); summary=_paper_summary(conn,int(user["id"]),settings)
    finally: conn.close()
    return {"success":True,"account":{"trading_mode":settings.get("trading_mode","paper"),"paper_capital":summary["seed_capital"],"opening_capital":summary["seed_capital"],"sizing_capital":summary["equity"],"paper_sizing_capital":summary["equity"],"sizing_capital_source":"PAPER_RESET_CYCLE_EQUITY","total_pnl":summary["cumulative_net_pnl"],"equity":summary["equity"],"current_capital":summary["equity"],"current_equity":summary["equity"],"total_trades":summary["closed_trades"],"capital_carry_forward":True,"capital_source":"PAPER_SEED_PLUS_NET_PNL_SINCE_RESET","paper_capital_reset_at":summary["reset_at"]}}

def _reset_continuous_paper_account(body: dict=None, authorization: str=Header(None)):
    user=paper_routes.get_current_user(authorization); body=body or {}; capital=paper_routes.clamp_cap(body.get("capital",100000)); now=datetime.utcnow().isoformat(); conn=get_db()
    try:
        settings=paper_routes.load_settings(conn,int(user["id"])); mode_filter=" AND COALESCE(trading_mode, 'paper')='paper'" if _has_column(conn,"paper_trades","trading_mode") else ""
        try: open_trade=conn.execute(f"SELECT id FROM paper_trades WHERE user_id=? AND status='OPEN'{mode_filter} LIMIT 1",(int(user["id"]),)).fetchone()
        except Exception: open_trade=None
        if open_trade: return {"success":False,"message":"Open PAPER trade close hone ke baad capital reset karein.","paper_capital":capital}
        settings["paper_capital"]=capital; settings["trading_mode"]="paper"; settings[RESET_KEY]=now; settings[CARRY_KEY]=True; paper_routes.save_settings(conn,int(user["id"]),settings)
    finally: conn.close()
    return {"success":True,"message":"Paper capital reset; new sizing cycle started","paper_capital":capital,"sizing_capital":capital,"sizing_capital_source":"PAPER_RESET_CYCLE_EQUITY","capital_carry_forward":True,"paper_capital_reset_at":now}

def apply_capital_continuity_patch():
    global _INSTALLED
    if _INSTALLED: return
    runtime._paper_base=_continuous_paper_base; runtime._okai_paper_capital_carry_forward_v1=True; runtime._okai_paper_sizing_source="PAPER_RESET_CYCLE_EQUITY"; runtime._okai_live_capital_source="BROKER_AVAILABLE_FUNDS"; paper_routes.paper_account=_continuous_paper_account; paper_routes.reset_paper_account=_reset_continuous_paper_account; _replace_route(paper_routes.router,"/paper/account","GET",_continuous_paper_account); _replace_route(paper_routes.router,"/paper/reset","POST",_reset_continuous_paper_account); _INSTALLED=True

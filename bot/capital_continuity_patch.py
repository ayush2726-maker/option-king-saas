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
RESET_KEY="paper_capital_reset_at"; CARRY_KEY="paper_capital_carry_forward"; _INSTALLED=False

def _f(v,d=0.0):
    try:return float(v)
    except Exception:return float(d)

def _has_column(c,t,n):
    try:return any(str(r["name"])==n for r in c.execute(f"PRAGMA table_info({t})").fetchall())
    except Exception:return False

def _paper_summary(c,u,s):
    seed=max(1.0,_f(s.get("paper_capital",100000),100000)); reset=str(s.get(RESET_KEY) or "").strip(); expr="COALESCE(net_pnl,pnl,0)" if _has_column(c,"paper_trades","net_pnl") else "COALESCE(pnl,0)"; where=["user_id=?","status='CLOSED'"]; params=[int(u)]
    if _has_column(c,"paper_trades","trading_mode"):where.append("COALESCE(trading_mode,'paper')='paper'")
    if reset:where.append("datetime(created_at)>=datetime(?)");params.append(reset)
    r=c.execute(f"SELECT COALESCE(SUM({expr}),0) AS net_pnl,COUNT(*) AS closed_trades FROM paper_trades WHERE {' AND '.join(where)}",tuple(params)).fetchone(); pnl=round(_f(r["net_pnl"] if r else 0),2); eq=max(1.0,round(seed+pnl,2)); return {"seed_capital":round(seed,2),"cumulative_net_pnl":pnl,"equity":eq,"closed_trades":int(r["closed_trades"] if r else 0),"reset_at":reset or None}

def _continuous_paper_base(c,u,s):return _paper_summary(c,int(u),s)["equity"]

def _replace_route(router,path,method,endpoint):
    for r in getattr(router,"routes",[]):
        if getattr(r,"path",None)==path and method in getattr(r,"methods",set()):
            r.endpoint=endpoint
            try:r.dependant.call=endpoint
            except Exception:pass

def _continuous_paper_account(authorization: str=Header(None)):
    u=paper_routes.get_current_user(authorization); c=get_db()
    try:s=paper_routes.load_settings(c,int(u["id"])); x=_paper_summary(c,int(u["id"]),s)
    finally:c.close()
    return {"success":True,"account":{"trading_mode":s.get("trading_mode","paper"),"paper_capital":x["seed_capital"],"opening_capital":x["seed_capital"],"sizing_capital":x["equity"],"paper_sizing_capital":x["equity"],"sizing_capital_source":"PAPER_RESET_CYCLE_EQUITY","total_pnl":x["cumulative_net_pnl"],"equity":x["equity"],"current_capital":x["equity"],"current_equity":x["equity"],"total_trades":x["closed_trades"],"capital_carry_forward":True,"capital_source":"PAPER_SEED_PLUS_NET_PNL_SINCE_RESET","paper_capital_reset_at":x["reset_at"]}}

def _reset_continuous_paper_account(body:dict=None,authorization:str=Header(None)):
    u=paper_routes.get_current_user(authorization); body=body or {}; cap=paper_routes.clamp_cap(body.get("capital",100000)); now=datetime.utcnow().isoformat(); c=get_db()
    try:
        s=paper_routes.load_settings(c,int(u["id"])); mf=" AND COALESCE(trading_mode,'paper')='paper'" if _has_column(c,"paper_trades","trading_mode") else ""
        try:o=c.execute(f"SELECT id FROM paper_trades WHERE user_id=? AND status='OPEN'{mf} LIMIT 1",(int(u["id"]),)).fetchone()
        except Exception:o=None
        if o:return {"success":False,"message":"Open PAPER trade close hone ke baad capital reset karein.","paper_capital":cap}
        s["paper_capital"]=cap;s["trading_mode"]="paper";s[RESET_KEY]=now;s[CARRY_KEY]=True;paper_routes.save_settings(c,int(u["id"]),s)
    finally:c.close()
    return {"success":True,"message":"Paper capital reset; new sizing cycle started","paper_capital":cap,"sizing_capital":cap,"sizing_capital_source":"PAPER_RESET_CYCLE_EQUITY","capital_carry_forward":True,"paper_capital_reset_at":now}

def apply_capital_continuity_patch():
    global _INSTALLED
    if _INSTALLED:return
    runtime._paper_base=_continuous_paper_base;runtime._okai_paper_capital_carry_forward_v1=True;runtime._okai_paper_sizing_source="PAPER_RESET_CYCLE_EQUITY";runtime._okai_live_capital_source="BROKER_AVAILABLE_FUNDS";paper_routes.paper_account=_continuous_paper_account;paper_routes.reset_paper_account=_reset_continuous_paper_account;_replace_route(paper_routes.router,"/paper/account","GET",_continuous_paper_account);_replace_route(paper_routes.router,"/paper/reset","POST",_reset_continuous_paper_account);_INSTALLED=True

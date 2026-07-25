"""Broker-neutral Railway advanced intelligence. Shadow-only; never controls orders."""
import json, math, os, threading, time, uuid, urllib.request
from datetime import datetime, timezone
from auth.utils import decrypt_credential
from database import get_db, get_db_storage_info
from bot.shared_ai import predict
from bot.option_chain import resolve_option, get_atm_strike, EXCHANGE_FOR

VERSION="OKAI-ADVANCED-INTELLIGENCE-SHADOW-V1"
HORIZONS=(5,15,30); PRIMARY=15; POLL=15; REFRESH=60; GLOBAL_REFRESH=180; READY=300
UP_KEYS={"NIFTY":"NSE_INDEX|Nifty 50","BANKNIFTY":"NSE_INDEX|Nifty Bank","SENSEX":"BSE_INDEX|SENSEX"}
GLOBALS={"sp500":"%5EGSPC","nasdaq":"%5EIXIC","nikkei":"%5EN225","hang_seng":"%5EHSI","crude":"CL%3DF","usd_inr":"USDINR%3DX","india_vix":"%5EINDIAVIX"}
_lock=threading.RLock();_started=False;_thread=None;_builder=None;_last_cycle=None;_last_error=None;_last_global=None;_global={};_sessions={};_cache={};_instance=uuid.uuid4().hex[:12]

def num(v,d=0):
    try:
        x=float(v);return x if math.isfinite(x) else float(d)
    except Exception:return float(d)
def integer(v,d=0):
    try:return int(float(v))
    except Exception:return int(d)
def clamp(v,a,b):return max(a,min(b,v))
def now():return datetime.now(timezone.utc)
def iso(v=None):return (v or now()).replace(microsecond=0).isoformat().replace("+00:00","Z")
def parse(v):
    try:
        d=datetime.fromisoformat(str(v).replace("Z","+00:00"));return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:return None
def dumps(v):
    try:return json.dumps(v,ensure_ascii=False,separators=(",",":"))
    except Exception:return "{}" if isinstance(v,dict) else "[]"
def loads(v,d):
    try:return json.loads(str(v or ""))
    except Exception:return d
def side(v):
    t=str(v or "").upper()
    if t in {"CE","CALL","BUY","BULLISH","UP","UPTREND"}:return "CE"
    if t in {"PE","PUT","SELL","BEARISH","DOWN","DOWNTREND"}:return "PE"
    if t in {"NO_TRADE","NO TRADE","WAIT","WAITING","HOLD","SKIP"}:return "NO_TRADE"
    return "NEUTRAL"

def schema():
    c=get_db()
    try:
        c.executescript("""CREATE TABLE IF NOT EXISTS ai_advanced_snapshots(id TEXT PRIMARY KEY,user_id INTEGER,created_at TEXT,broker_name TEXT,symbol TEXT,spot REAL,base_decision TEXT,base_confidence INTEGER,advanced_decision TEXT,advanced_confidence INTEGER,relation TEXT,features_json TEXT,contract_json TEXT,entry_premium REAL,qty INTEGER,quality INTEGER,complete INTEGER DEFAULT 0,completed_at TEXT,trade_blocking INTEGER DEFAULT 0,order_execution INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_adv_user ON ai_advanced_snapshots(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS ai_advanced_outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT,snapshot_id TEXT,user_id INTEGER,horizon INTEGER,evaluated_at TEXT,exit_spot REAL,exit_premium REAL,premium_change REAL,gross_pnl REAL,costs REAL,net_pnl REAL,outcome TEXT,base_outcome TEXT,benefit REAL,UNIQUE(snapshot_id,horizon));
CREATE TABLE IF NOT EXISTS ai_trade_labels(source_key TEXT PRIMARY KEY,user_id INTEGER,broker_name TEXT,symbol TEXT,side TEXT,entry_time TEXT,exit_time TEXT,entry_price REAL,exit_price REAL,qty INTEGER,stored_pnl REAL,estimated_net_pnl REAL,label TEXT,captured_at TEXT);
CREATE TABLE IF NOT EXISTS ai_global_cache(name TEXT PRIMARY KEY,symbol TEXT,price REAL,previous_close REAL,change_pct REAL,fetched_at TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS ai_advanced_runtime(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT,instance_id TEXT,started_at TEXT,heartbeat_at TEXT,last_global_fetch TEXT,last_error TEXT,trade_blocking INTEGER DEFAULT 0,order_execution INTEGER DEFAULT 0);""")
        t=iso();c.execute("""INSERT INTO ai_advanced_runtime VALUES(1,?,?,?,?,NULL,NULL,0,0) ON CONFLICT(singleton) DO UPDATE SET version=excluded.version,instance_id=excluded.instance_id,heartbeat_at=excluded.heartbeat_at,trade_blocking=0,order_execution=0""",(VERSION,_instance,t,t));c.commit()
    finally:c.close()

def users():
    c=get_db()
    try:return [int(r["user_id"]) for r in c.execute("SELECT DISTINCT user_id FROM (SELECT user_id FROM user_bot_state WHERE is_running=1 UNION ALL SELECT user_id FROM bot_status WHERE is_running=1)").fetchall()]
    except Exception:return []
    finally:c.close()

def broker_row(uid):
    c=get_db()
    try:
        r=c.execute("SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY COALESCE(last_connected,created_at) DESC LIMIT 1",(uid,)).fetchone();return dict(r) if r else {}
    finally:c.close()
def broker_session(uid):
    r=broker_row(uid)
    if not r:raise RuntimeError("BROKER_NOT_CONNECTED")
    name=str(r.get("broker_name") or "angelone").lower();old=_sessions.get(uid)
    if old and old[0]==name and time.monotonic()-old[2]<1800:return old[0],old[1]
    creds={"client_id":r.get("client_id"),"api_key":decrypt_credential(r.get("api_key")) if r.get("api_key") else "","password":decrypt_credential(r.get("api_secret")) if r.get("api_secret") else "","totp_secret":decrypt_credential(r.get("totp_secret")) if r.get("totp_secret") else None}
    if name=="angelone":
        from bot.angel_fetcher import angel_login
        obj=angel_login(creds)
    else:
        from bot.brokers.factory import create_broker
        obj=create_broker(name,creds["client_id"],creds["api_key"],creds["password"],creds["totp_secret"]);res=obj.login()
        if not res.get("success"):raise RuntimeError(res.get("message") or "BROKER_LOGIN_FAILED")
    _sessions[uid]=(name,obj,time.monotonic());return name,obj

def http_json(url):
    q=urllib.request.Request(url,headers={"User-Agent":"OptionKingAI/1.0","Accept":"application/json"})
    with urllib.request.urlopen(q,timeout=8) as r:return json.loads(r.read().decode("utf-8","replace"))
def global_features(rows):
    d={str(x.get("name")):dict(x) for x in rows};score=0
    for k,w in (("sp500",1.2),("nasdaq",1.1),("nikkei",.8),("hang_seng",.8)):score+=clamp(num(d.get(k,{}).get("change_pct")),-2.5,2.5)*w
    score-=clamp(num(d.get("crude",{}).get("change_pct")),-4,4)*.45;score-=clamp(num(d.get("usd_inr",{}).get("change_pct")),-1.5,1.5)*1.3;score-=clamp(num(d.get("india_vix",{}).get("change_pct")),-8,8)*.35
    avail=sum(num(x.get("price"))>0 for x in d.values())
    return {"direction":"CE" if score>=.9 else "PE" if score<=-.9 else "NEUTRAL","risk_on_score":round(score,3),"available":avail,"expected":len(GLOBALS),"quality":round(avail/len(GLOBALS)*100,2),"markets":d,"fetched_at":_last_global}
def refresh_global(force=False):
    global _last_global,_global
    p=parse(_last_global)
    if not force and p and (now()-p).total_seconds()<GLOBAL_REFRESH:return dict(_global)
    rows=[]
    for name,sym in GLOBALS.items():
        try:
            z=http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2d&interval=5m");m=((z.get("chart") or {}).get("result") or [{}])[0].get("meta") or {};price=num(m.get("regularMarketPrice"));prev=num(m.get("chartPreviousClose",m.get("previousClose")));chg=(price/prev-1)*100 if price>0 and prev>0 else 0;rows.append({"name":name,"symbol":sym,"price":price,"previous_close":prev,"change_pct":round(chg,4),"fetched_at":iso(),"error":None})
        except Exception as e:rows.append({"name":name,"symbol":sym,"price":0,"previous_close":0,"change_pct":0,"fetched_at":iso(),"error":f"{type(e).__name__}:{str(e)[:100]}"})
    c=get_db()
    try:
        for x in rows:c.execute("INSERT INTO ai_global_cache VALUES(?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET symbol=excluded.symbol,price=excluded.price,previous_close=excluded.previous_close,change_pct=excluded.change_pct,fetched_at=excluded.fetched_at,error=excluded.error",(x["name"],x["symbol"],x["price"],x["previous_close"],x["change_pct"],x["fetched_at"],x["error"]))
        c.commit()
    finally:c.close()
    _last_global=iso();_global=global_features(rows);return dict(_global)

def norm_quote(x):
    x=dict(x or {});ltp=num(x.get("ltp",x.get("last_price",x.get("lastPrice"))));oi=num(x.get("oi",x.get("open_interest")));prev=num(x.get("previous_oi",x.get("prev_oi")));bid=num(x.get("best_bid",x.get("bid_price")));ask=num(x.get("best_ask",x.get("ask_price")));bq=num(x.get("bid_qty"));aq=num(x.get("ask_qty"));depth=x.get("depth") or {}
    if (bid<=0 or ask<=0) and isinstance(depth,dict):
        buy=(depth.get("buy") or [{}])[0];sell=(depth.get("sell") or [{}])[0];bid=num(buy.get("price"),bid);ask=num(sell.get("price"),ask);bq=num(buy.get("quantity"),bq);aq=num(sell.get("quantity"),aq)
    return {"ltp":round(ltp,4),"oi":oi,"change_oi":oi-prev if prev else 0,"bid":bid,"ask":ask,"spread_pct":round((ask-bid)/max(.01,ltp)*100,4) if ask>0 and bid>0 else 0,"imbalance":round((bq-aq)/max(1,bq+aq),4) if bq or aq else 0}

def contract(name,obj,u,spot,s):
    if name=="angelone":return dict(resolve_option(u,spot,s) or {})
    r=obj.search_option(u,"current_week",get_atm_strike(u,spot),s) or {}
    if not r.get("success"):return {}
    return {"symbol":r.get("symbol"),"token":r.get("token"),"exchange":r.get("exchange"),"exch_seg":r.get("exchange"),"expiry":r.get("expiry"),"strike":num(r.get("strike")),"lot_size":integer(r.get("lot_size"),1),"underlying":u,"option_type":s}
def quote(name,obj,c):
    if not c:return {"ltp":0}
    if name=="angelone":
        try:
            z=obj.getMarketData("FULL",{str(c.get("exch_seg") or "NFO"):[str(c.get("token"))]});r=(((z or {}).get("data") or {}).get("fetched") or [{}])[0]
        except Exception:r={}
        if not r:
            try:r=(obj.ltpData(c.get("exch_seg"),c.get("symbol"),c.get("token")) or {}).get("data") or {}
            except Exception:r={}
        return norm_quote(r)
    if name=="upstox":
        try:
            import requests
            z=requests.get(f"{obj.BASE_URL}/market-quote/quotes",params={"instrument_key":c.get("token")},headers=obj._h(),timeout=10).json();d=z.get("data") or {};r=d.get(c.get("token")) or d.get(str(c.get("token")).replace("|",":")) or next(iter(d.values()),{});return norm_quote(r)
        except Exception:return norm_quote({})
    try:return norm_quote({"ltp":(obj.get_ltp(c.get("symbol"),c.get("exchange","NFO")) or {}).get("ltp")})
    except Exception:return norm_quote({})

def angel_chain(obj,u,spot,expiry):
    try:
        from bot import option_chain as oc
        opts=oc._load_cache();parse_exp=oc._parse_expiry
    except Exception:return []
    atm=get_atm_strike(u,spot);step=100 if u in {"BANKNIFTY","SENSEX"} else 50;wanted={atm+i*step for i in range(-4,5)};items=[];by={};tokens={}
    for r in opts:
        if r.get("name")!=u:continue
        ex=parse_exp(r.get("expiry"))
        if expiry and (not ex or ex.isoformat()!=str(expiry)[:10]):continue
        sym=str(r.get("symbol") or "").upper();s="CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else None
        if not s:continue
        try:st=float(r.get("strike"))/100
        except Exception:continue
        if st not in wanted:continue
        item={"strike":st,"side":s,"token":str(r.get("token")),"exchange":str(r.get("exch_seg") or EXCHANGE_FOR.get(u,"NFO"))};items.append(item);by[item["token"]]=item;tokens.setdefault(item["exchange"],[]).append(item["token"])
    try:rows=(((obj.getMarketData("FULL",tokens) or {}).get("data") or {}).get("fetched") or [])
    except Exception:rows=[]
    out={st:{"strike":st,"call":{},"put":{}} for st in wanted}
    for r in rows:
        tok=str(r.get("symbolToken") or r.get("symboltoken") or r.get("token"));m=by.get(tok)
        if m:out[m["strike"]]["call" if m["side"]=="CE" else "put"]=norm_quote(r)
    return [out[k] for k in sorted(out) if out[k]["call"] or out[k]["put"]]
def up_chain(obj,u,c):
    if not c.get("expiry") or not UP_KEYS.get(u):return []
    try:
        import requests
        rows=requests.get(f"{obj.BASE_URL}/option/chain",params={"instrument_key":UP_KEYS[u],"expiry_date":str(c.get("expiry"))[:10]},headers=obj._h(),timeout=12).json().get("data") or [];out=[]
        for r in rows:
            ca=r.get("call_options") or {};pu=r.get("put_options") or {};cq=norm_quote(ca.get("market_data") or {});pq=norm_quote(pu.get("market_data") or {});cq["greeks"]=ca.get("option_greeks") or {};pq["greeks"]=pu.get("option_greeks") or {};out.append({"strike":num(r.get("strike_price")),"call":cq,"put":pq})
        return out
    except Exception:return []
def chain(name,obj,u,spot,c):return up_chain(obj,u,c) if name=="upstox" else angel_chain(obj,u,spot,c.get("expiry")) if name=="angelone" else []

def option_features(rows,spot):
    rows=[dict(r) for r in rows if num(r.get("strike"))>0]
    if not rows:return {"available":False,"direction":"NEUTRAL","risk":0,"points":0}
    coi=poi=cc=pc=0;cw=(0,None);pw=(0,None);ivs_c=[];ivs_p=[];sp=[];im=[];pain={}
    for r in rows:
        st=num(r["strike"]);c=dict(r.get("call") or {});p=dict(r.get("put") or {});co=num(c.get("oi"));po=num(p.get("oi"));coi+=co;poi+=po;cc+=num(c.get("change_oi"));pc+=num(p.get("change_oi"));cw=max(cw,(co,st));pw=max(pw,(po,st))
        cg=c.get("greeks") or {};pg=p.get("greeks") or {};ci=num(cg.get("iv",cg.get("implied_volatility")));pi=num(pg.get("iv",pg.get("implied_volatility")))
        if ci:ivs_c.append(ci)
        if pi:ivs_p.append(pi)
        for q in (c,p):
            if num(q.get("spread_pct")):sp.append(num(q.get("spread_pct")))
            im.append(num(q.get("imbalance")))
    for x in [num(r["strike"]) for r in rows]:pain[x]=sum(max(0,x-num(r["strike"]))*num((r.get("call") or {}).get("oi"))+max(0,num(r["strike"])-x)*num((r.get("put") or {}).get("oi")) for r in rows)
    pcr=poi/coi if coi else None;pcrc=pc/cc if cc else None;mp=min(pain,key=pain.get) if pain else None;iv=(sum(ivs_p)/len(ivs_p)-sum(ivs_c)/len(ivs_c)) if ivs_c and ivs_p else None;spread=sum(sp)/len(sp) if sp else None;imb=sum(im)/len(im) if im else 0;score=0
    if pcr is not None:score+=1.2 if pcr>=1.15 else -1.2 if pcr<=.85 else 0
    if pcrc is not None:score+=.8 if pcrc>=1.2 else -.8 if pcrc<=.8 else 0
    score+=clamp(imb*1.5,-.75,.75);risk=clamp((spread or 0)*22+abs(iv or 0)*1.8,0,100)
    return {"available":True,"pcr_oi":round(pcr,4) if pcr is not None else None,"pcr_change_oi":round(pcrc,4) if pcrc is not None else None,"max_pain":mp,"call_wall":cw[1],"put_wall":pw[1],"iv_skew":round(iv,4) if iv is not None else None,"spread_pct":round(spread,4) if spread is not None else None,"imbalance":round(imb,4),"points":len(rows),"direction":"CE" if score>=.8 else "PE" if score<=-.8 else "NEUTRAL","direction_score":round(score,3),"risk":int(risk)}
def chosen_greeks(rows,c):
    st=num(c.get("strike"));key="call" if side(c.get("option_type"))=="CE" else "put";r=min(rows,key=lambda x:abs(num(x.get("strike"))-st),default={});g=(r.get(key) or {}).get("greeks") or {};return {k:num(g.get(k)) for k in ("delta","gamma","theta","vega","iv")}

def fuse(base,opt,glob,news,q,g):
    p=dict(base.get("probabilities") or {});s={k:num(p.get(k),100 if k=="NO_TRADE" else 0) for k in ("CE","PE","NO_TRADE")};re=[]
    od=side(opt.get("direction"))
    if od in {"CE","PE"}:sh=clamp(7+abs(num(opt.get("direction_score")))*5,7,22);s[od]+=sh;s["PE" if od=="CE" else "CE"]-=sh*.45;re.append("OPTION_"+od)
    gd=side(glob.get("direction"))
    if gd in {"CE","PE"}:s[gd]+=clamp(4+abs(num(glob.get("risk_on_score")))*2.5,4,16);re.append("GLOBAL_"+gd)
    nd=side(news.get("news_bias"))
    if news.get("fresh") and nd in {"CE","PE"}:s[nd]+=clamp(5+num(news.get("news_strength"))*.13,5,18);re.append("NEWS_"+nd)
    if num(opt.get("risk"))>=65:s["NO_TRADE"]+=20;re.append("OPTION_RISK")
    if num(q.get("spread_pct"))>1.2:s["NO_TRADE"]+=clamp(num(q.get("spread_pct"))*10,12,30);re.append("SPREAD")
    if num(g.get("iv"))>45:s["NO_TRADE"]+=clamp((num(g.get("iv"))-45)*.8,5,22);re.append("IV")
    if abs(num(g.get("theta")))>15:s["NO_TRADE"]+=clamp(abs(num(g.get("theta")))*.5,5,18);re.append("THETA")
    s={k:max(0,v) for k,v in s.items()};t=sum(s.values()) or 1;pr={k:int(round(v/t*100)) for k,v in s.items()};pr["NO_TRADE"]+=100-sum(pr.values());d,conf=max(pr.items(),key=lambda x:x[1]);bd=side(base.get("decision"));rel="SAME_AS_BASE" if d==bd else "ADVANCED_WOULD_BLOCK_BASE" if d=="NO_TRADE" and bd in {"CE","PE"} else "ADVANCED_OPPOSITE_BASE" if d in {"CE","PE"} and bd in {"CE","PE"} else "ADVANCED_DIRECTIONAL_WHEN_BASE_WAITED" if d in {"CE","PE"} and bd=="NO_TRADE" else "DIFFERENT"
    return {"decision":d,"confidence":conf,"probabilities":pr,"base_decision":bd,"relation":rel,"reasons":re,"trade_blocking":False,"order_execution":False}
def news():
    try:
        from bot.news_intelligence import aggregate
        return aggregate()
    except Exception:return {"fresh":False,"news_bias":"NEUTRAL","news_strength":0}

def collect(uid,m,force=False):
    u=str(m.get("symbol") or "NIFTY").upper();spot=num(m.get("price"));b=predict(dict(m));bs=side(b.get("decision"));ss=side(m.get("signal_direction") or m.get("signal"));cs=bs if bs in {"CE","PE"} else ss if ss in {"CE","PE"} else "CE";name="unavailable";err=None;c={};q={"ltp":0};rows=[]
    try:
        name,obj=broker_session(uid);old=_cache.get(uid) or {};same=old.get("u")==u and old.get("s")==cs and abs(num(old.get("spot"))-spot)<=max(25,spot*.002)
        if not force and same and time.monotonic()-num(old.get("time"))<REFRESH:c,q,rows=old["c"],old["q"],old["rows"]
        else:c=contract(name,obj,u,spot,cs);q=quote(name,obj,c);rows=chain(name,obj,u,spot,c);_cache[uid]={"u":u,"s":cs,"spot":spot,"time":time.monotonic(),"c":c,"q":q,"rows":rows}
    except Exception as e:err=f"{type(e).__name__}:{str(e)[:160]}";_sessions.pop(uid,None)
    opt=option_features(rows,spot);g=chosen_greeks(rows,c);glob=refresh_global();n=news();a=fuse(b,opt,glob,n,q,g);quality=int(clamp(25+(30 if opt.get("available") else 0)+(20 if num(q.get("ltp")) else 0)+num(glob.get("quality"))*.15+(10 if n.get("fresh") else 0),0,100))
    return {"version":VERSION,"broker_name":name,"broker_error":err,"market":dict(m),"base":b,"advanced":a,"contract":c,"quote":q,"greeks":g,"option":opt,"global":glob,"news":n,"quality":quality,"trade_blocking":False,"order_execution":False}

def costs(e,x,q):return round(40+(e+x)*max(1,q)*.00105+max(.5,e*.002)*max(1,q),2)
def out(s,chg,spotchg):
    if s=="NO_TRADE":return "CORRECT_SKIP" if abs(spotchg)<5 else "MISSED_MOVE"
    if chg is None:return "PREMIUM_UNAVAILABLE"
    return "WIN" if chg>=1 else "LOSS" if chg<=-1 else "FLAT"
def register(uid,f):
    m=f["market"];spot=num(m.get("price"))
    if spot<=0 or not m.get("market_open") or not m.get("feed_connected"):return
    a=f["advanced"];b=f["base"];rel=a.get("relation");sym=str(m.get("symbol") or "NIFTY").upper();c=get_db()
    try:
        last=c.execute("SELECT * FROM ai_advanced_snapshots WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT 1",(uid,)).fetchone()
        if last:
            t=parse(last["created_at"]);same=(last["symbol"],last["advanced_decision"],last["base_decision"],last["relation"],int(num(last["spot"])/5))==(sym,side(a.get("decision")),side(b.get("decision")),rel,int(spot/5))
            if t and same and (now()-t).total_seconds()<300:return
        did=uuid.uuid4().hex[:20];ct=f.get("contract") or {};c.execute("INSERT INTO ai_advanced_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,0,0)",(did,uid,iso(),f.get("broker_name"),sym,round(spot,2),side(b.get("decision")),integer(b.get("confidence")),side(a.get("decision")),integer(a.get("confidence")),rel,dumps(f),dumps(ct),num((f.get("quote") or {}).get("ltp")),integer(ct.get("lot_size"),1),integer(f.get("quality"))));c.commit()
    finally:c.close()
def premium(uid,name,ct):
    try:
        n,obj=broker_session(uid)
        if n!=name:return None
        x=num(quote(n,obj,ct).get("ltp"));return x if x>0 else None
    except Exception:_sessions.pop(uid,None);return None
def observe(uid,m):
    spot=num(m.get("price"));sym=str(m.get("symbol") or "NIFTY").upper()
    if spot<=0 or not m.get("feed_connected"):return
    c=get_db()
    try:
        for r in c.execute("SELECT * FROM ai_advanced_snapshots WHERE user_id=? AND symbol=? AND complete=0",(uid,sym)).fetchall():
            t=parse(r["created_at"])
            if not t:continue
            elapsed=(now()-t).total_seconds();due=[h for h in HORIZONS if elapsed>=h*60 and not c.execute("SELECT 1 FROM ai_advanced_outcomes WHERE snapshot_id=? AND horizon=?",(r["id"],h)).fetchone()]
            if not due:continue
            ep=num(r["entry_premium"]);xp=premium(uid,str(r["broker_name"]),loads(r["contract_json"],{}));chg=xp-ep if xp is not None and ep>0 else None;qty=max(1,integer(r["qty"],1));gross=chg*qty if chg is not None else None;co=costs(ep,xp or ep,qty) if xp is not None and ep>0 else None;net=gross-co if gross is not None and co is not None else None;spotchg=spot-num(r["spot"]);ao=out(str(r["advanced_decision"]),chg,spotchg);bo=out(str(r["base_decision"]),chg,spotchg);benefit=0 if r["advanced_decision"]==r["base_decision"] else -gross if r["advanced_decision"]=="NO_TRADE" and gross is not None else gross or 0
            for h in due:c.execute("INSERT OR IGNORE INTO ai_advanced_outcomes VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)",(r["id"],uid,h,iso(),spot,xp,chg,gross,co,net,ao,bo,benefit))
            if integer(c.execute("SELECT COUNT(*) n FROM ai_advanced_outcomes WHERE snapshot_id=?",(r["id"],)).fetchone()["n"])>=3:c.execute("UPDATE ai_advanced_snapshots SET complete=1,completed_at=? WHERE id=?",(iso(),r["id"]))
        c.commit()
    finally:c.close()
def sync_labels(uid):
    c=get_db()
    try:
        try:rows=c.execute("SELECT * FROM paper_trades WHERE user_id=? AND UPPER(status)='CLOSED' AND exit_price IS NOT NULL ORDER BY id DESC LIMIT 500",(uid,)).fetchall()
        except Exception:rows=[]
        for r in rows:
            x=dict(r);e=num(x.get("entry_price"));z=num(x.get("exit_price"));q=max(1,integer(x.get("qty",x.get("quantity")),1));net=(z-e)*q-costs(e,z,q);label="WIN" if net>0 else "LOSS" if net<0 else "FLAT";c.execute("INSERT OR IGNORE INTO ai_trade_labels VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(f"paper:{x.get('id')}",uid,str(x.get("broker_name") or "paper"),x.get("symbol"),x.get("side"),x.get("created_at"),x.get("exit_time"),e,z,q,num(x.get("pnl")) ,round(net,2),label,iso()))
        c.commit()
    finally:c.close()

def calibration(uid):
    c=get_db()
    try:rows=c.execute("SELECT s.advanced_confidence,s.advanced_decision,o.outcome,o.net_pnl,o.benefit FROM ai_advanced_snapshots s JOIN ai_advanced_outcomes o ON o.snapshot_id=s.id WHERE s.user_id=? AND o.horizon=?",(uid,PRIMARY)).fetchall();lab=c.execute("SELECT COUNT(*) n,SUM(label='WIN') w,SUM(label='LOSS') l,SUM(COALESCE(estimated_net_pnl,0)) net FROM ai_trade_labels WHERE user_id=?",(uid,)).fetchone()
    finally:c.close()
    resolved=[r for r in rows if r["advanced_decision"] in {"CE","PE"} and r["outcome"] in {"WIN","LOSS"}];wins=sum(r["outcome"]=="WIN" for r in resolved);brier=sum((num(r["advanced_confidence"])/100-(1 if r["outcome"]=="WIN" else 0))**2 for r in resolved)/len(resolved) if resolved else None
    return {"samples":len(rows),"resolved":len(resolved),"wins":wins,"losses":len(resolved)-wins,"hit_rate":round(wins/len(resolved)*100,2) if resolved else None,"brier":round(brier,6) if brier is not None else None,"net_option_pnl":round(sum(num(r["net_pnl"]) for r in rows),2),"benefit_vs_base":round(sum(num(r["benefit"]) for r in rows),2),"actual_trade_labels":integer(lab["n"] if lab else 0),"actual_trade_wins":integer(lab["w"] if lab else 0),"actual_trade_losses":integer(lab["l"] if lab else 0),"training_minimum":READY,"training_readiness":"READY_FOR_PAPER_FILTER_REVIEW" if len(rows)>=READY else "COLLECTING_DATA","trained_model_active":False}

def summary(uid,limit=20):
    schema();c=get_db()
    try:
        rs=c.execute("SELECT * FROM ai_advanced_snapshots WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT ?",(uid,max(1,min(integer(limit,20),50)))).fetchall();recent=[]
        for r in rs:
            x=dict(r);x["features"]=loads(x.pop("features_json"),{});x["contract"]=loads(x.pop("contract_json"),{});x["outcomes"]=[dict(o) for o in c.execute("SELECT * FROM ai_advanced_outcomes WHERE snapshot_id=? ORDER BY horizon",(x["id"],)).fetchall()];recent.append(x)
        gr=[dict(r) for r in c.execute("SELECT * FROM ai_global_cache ORDER BY name").fetchall()]
    finally:c.close()
    current={}
    try:current=collect(uid,_builder(uid)) if callable(_builder) else {}
    except Exception as e:current={"error":str(e)[:200]}
    return {"success":True,"version":VERSION,"mode":"RAILWAY_ADVANCED_SHADOW_ONLY","broker_neutral":True,"supported_brokers":["angelone","upstox","zerodha"],"broker_notes":{"angelone":"Market/news/outcomes plus Angel scrip-master and SmartAPI FULL quote/OI/depth when available.","upstox":"Native option-chain and Greeks when API entitlement permits.","zerodha":"Market/news/outcomes and selected-option quote; missing chain fields degrade safely."},"trade_blocking":False,"order_execution":False,"storage":get_db_storage_info(),"global":global_features(gr),"calibration":calibration(uid),"current":current,"recent":recent}
def health():
    schema();c=get_db()
    try:r=c.execute("SELECT * FROM ai_advanced_runtime WHERE singleton=1").fetchone();s=c.execute("SELECT COUNT(*) n,SUM(complete=0) p FROM ai_advanced_snapshots").fetchone();l=c.execute("SELECT COUNT(*) n FROM ai_trade_labels").fetchone()
    finally:c.close()
    return {"success":True,"version":VERSION,"started":_started,"thread_alive":bool(_thread and _thread.is_alive()),"last_cycle_at":_last_cycle,"last_global_fetch_at":_last_global,"last_error":_last_error,"runtime":dict(r) if r else None,"snapshots":integer(s["n"] if s else 0),"pending":integer(s["p"] if s else 0),"actual_trade_labels":integer(l["n"] if l else 0),"broker_neutral":True,"supported_brokers":["angelone","upstox","zerodha"],"storage":get_db_storage_info(),"trade_blocking":False,"order_execution":False}
def heartbeat(err=None):
    global _last_cycle,_last_error
    _last_cycle=iso();_last_error=str(err)[:300] if err else None;c=get_db()
    try:c.execute("UPDATE ai_advanced_runtime SET heartbeat_at=?,last_global_fetch=?,last_error=?,trade_blocking=0,order_execution=0 WHERE singleton=1",(_last_cycle,_last_global,_last_error));c.commit()
    finally:c.close()
def cycle():
    refresh_global()
    if not callable(_builder):return
    for uid in users():
        m=dict(_builder(uid) or {})
        try:observe(uid,m);sync_labels(uid);register(uid,collect(uid,m))
        except Exception as e:print(f"AI ADVANCED RAILWAY | user={uid} | {type(e).__name__}:{str(e)[:160]}")
def loop():
    while True:
        err=None
        try:cycle()
        except Exception as e:err=f"{type(e).__name__}:{str(e)[:220]}"
        try:heartbeat(err)
        except Exception:pass
        time.sleep(POLL)
def start(snapshot_builder):
    global _started,_thread,_builder
    with _lock:
        _builder=snapshot_builder
        if _started and _thread and _thread.is_alive():return health()
        schema();_started=True;_thread=threading.Thread(target=loop,name="okai-advanced-intelligence",daemon=True);_thread.start();print(f"AI ADVANCED RAILWAY {VERSION} active | Angel/Upstox/Zerodha | monitor only, trade blocking OFF");return health()

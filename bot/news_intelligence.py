"""Railway global-news intelligence and non-blocking fusion monitor."""
from __future__ import annotations
import email.utils, hashlib, json, math, os, threading, time, urllib.parse, urllib.request, uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional
from database import get_db, get_db_storage_info
from bot.shared_ai import predict

VERSION="OKAI-NEWS-FUSION-SHADOW-V1"; HORIZONS=(5,15,30); POLL=15; FETCH=180; LOOKBACK=180
GDELT="https://api.gdeltproject.org/api/v2/doc/doc"
QUERY='(war OR conflict OR missile OR attack OR invasion OR sanctions OR ceasefire OR coup OR election OR "political crisis" OR tariff OR "trade war" OR "central bank" OR inflation OR recession OR crude OR oil OR cyberattack)'
RSS=(("RBI_PRESS","OFFICIAL_RBI","https://rbi.org.in/pressreleases_rss.xml"),("RBI_NOTIFICATIONS","OFFICIAL_RBI","https://rbi.org.in/notifications_rss.xml"),("FED_MONETARY","OFFICIAL_FED","https://www.federalreserve.gov/feeds/press_monetary.xml"),("FED_ALL","OFFICIAL_FED","https://www.federalreserve.gov/feeds/press_all.xml"))
REL={"reuters.com":.95,"apnews.com":.92,"bbc.com":.9,"bbc.co.uk":.9,"ft.com":.9,"bloomberg.com":.9,"wsj.com":.88,"cnbc.com":.86,"thehindu.com":.84,"indianexpress.com":.84,"business-standard.com":.84,"economictimes.indiatimes.com":.82,"livemint.com":.82}
POS={"ceasefire":34,"peace deal":34,"truce":30,"de-escalation":28,"sanctions lifted":26,"rate cut":26,"cuts rates":26,"dovish":22,"stimulus":22,"liquidity support":20,"inflation falls":18,"inflation cools":18,"oil prices fall":18,"crude falls":18,"rupee strengthens":16,"trade deal":20,"tariff rollback":22}
NEG={"nuclear":42,"invasion":38,"missile attack":38,"air strike":34,"airstrike":34,"war":30,"armed conflict":30,"attack":25,"escalation":26,"sanctions":22,"coup":28,"state of emergency":28,"political crisis":22,"government collapse":24,"trade war":26,"tariff hike":24,"new tariff":20,"rate hike":25,"hikes rates":25,"hawkish":20,"inflation surges":22,"inflation rises":18,"recession":25,"default":30,"bank failure":32,"cyberattack":24,"oil prices surge":24,"crude surges":24,"supply disruption":22,"rupee falls":18,"rupee weakens":18}
CATS={"WAR_GEOPOLITICAL":("war","missile","attack","invasion","airstrike","ceasefire","truce","sanctions","military","nuclear","terror"),"CENTRAL_BANK":("rbi","reserve bank","federal reserve","fomc","central bank","rate cut","rate hike","interest rate","monetary policy"),"TRADE_TARIFF":("tariff","trade war","trade deal","export ban","import ban"),"OIL_ENERGY":("crude","oil price","opec","natural gas","oil supply"),"MACRO":("inflation","recession","gdp","unemployment","default","debt crisis","currency","rupee","dollar"),"POLITICAL":("election","government","parliament","president","prime minister","political crisis","coup","coalition"),"DISASTER_CYBER":("earthquake","flood","cyclone","hurricane","pandemic","cyberattack","outage","explosion")}
HIGH=("nuclear","invasion","missile","war","rate hike","rate cut","state of emergency","bank failure","default","ceasefire","trade war","sanctions","coup")
_lock=threading.RLock(); _started=False; _thread=None; _builder=None; _instance=uuid.uuid4().hex[:12]
_last_fetch=None; _last_cycle=None; _last_error=None; _last_fetch_mono=0.0; _fetching=False

def num(v,d=0.0):
    try:
        n=float(v); return n if math.isfinite(n) else float(d)
    except (TypeError,ValueError): return float(d)
def integer(v,d=0):
    try:return int(float(v))
    except (TypeError,ValueError):return int(d)
def now(): return datetime.now(timezone.utc)
def iso(v=None): return (v or now()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")
def parse_date(v):
    s=str(v or "").strip()
    if not s:return None
    for fn in (lambda:datetime.fromisoformat(s.replace("Z","+00:00")),lambda:email.utils.parsedate_to_datetime(s),lambda:datetime.strptime(s,"%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),lambda:datetime.strptime(s,"%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)):
        try:
            d=fn(); return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except Exception:pass
    return None
def dumps(v):
    try:return json.dumps(v,ensure_ascii=False,separators=(",",":"))
    except Exception:return "{}" if isinstance(v,dict) else "[]"
def loads(v,d):
    try:return json.loads(str(v or ""))
    except Exception:return d
def clamp(v,a,b):return max(a,min(b,v))
def direction(v):
    t=str(v or "").strip().upper()
    if t in {"CE","UP","BULLISH","BUY","CALL","UPTREND"}:return "CE"
    if t in {"PE","DOWN","BEARISH","SELL","PUT","DOWNTREND"}:return "PE"
    if t in {"NO_TRADE","NO TRADE","WAIT","WAITING","HOLD","SKIP"}:return "NO_TRADE"
    return "NEUTRAL"
def domain(url):
    try:return (urllib.parse.urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:return ""
def reliability(st,dom):
    if st.startswith("OFFICIAL_"):return 1.0
    for k,v in REL.items():
        if dom==k or dom.endswith("."+k):return v
    return .68

def classify_headline(title,summary="",source_type="GLOBAL_NEWS",domain=""):
    text=(" "+str(title)+" "+str(summary)+" ").lower(); cat="GENERAL"; matches=[]
    for name,terms in CATS.items():
        found=[x for x in terms if x in text]
        if len(found)>len(matches):cat,matches=name,found
    ph=[(k,w) for k,w in POS.items() if k in text]; nh=[(k,w) for k,w in NEG.items() if k in text]
    ps,ns=sum(w for _,w in ph),sum(w for _,w in nh)
    if any(x in text for x in ("ceasefire","peace deal","truce","de-escalation")):ps+=18;ns=max(0,ns-16)
    raw=ps-ns; side="CE" if raw>=8 else "PE" if raw<=-8 else "NEUTRAL"
    impact=int(clamp(24+abs(raw)+7*len(set(matches))+(10 if source_type.startswith("OFFICIAL_") else 0),18,100))
    return {"category":cat,"direction":side,"impact_score":impact,"reliability":round(reliability(source_type,domain),3),"high_impact":bool(impact>=70 or any(x in text for x in HIGH)),"matched_keywords":sorted(set(matches+[k for k,_ in ph]+[k for k,_ in nh]))[:16]}

def http_get(url):
    req=urllib.request.Request(url,headers={"User-Agent":"OptionKingAI-NewsMonitor/1.0","Accept":"application/json, application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req,timeout=10) as r:return r.read()
def xt(node,names):
    wanted={x.lower() for x in names}
    for c in list(node):
        tag=c.tag.split("}")[-1].lower()
        if tag in wanted:
            if tag=="link" and c.attrib.get("href"):return str(c.attrib["href"]).strip()
            return "".join(c.itertext()).strip()
    return ""
def eid(url,title,source):return hashlib.sha256(f"{url}|{title}|{source}".encode("utf-8","ignore")).hexdigest()[:28]
def fetch_rss(source,st,url):
    root=ET.fromstring(http_get(url)); nodes=list(root.findall(".//item")) or [n for n in root.iter() if n.tag.split("}")[-1].lower()=="entry"]; out=[]
    for n in nodes[:60]:
        title=xt(n,("title",))
        if not title:continue
        link=xt(n,("link","guid","id")); summary=xt(n,("description","summary","content")); published=xt(n,("pubDate","published","updated","date")); dom=domain(link)
        out.append({"event_id":eid(link,title,source),"source":source,"source_type":st,"domain":dom,"title":title[:500],"summary":summary[:1200],"url":link[:1500],"published_at":iso(parse_date(published) or now()),**classify_headline(title,summary,st,dom)})
    return out
def fetch_gdelt():
    q=urllib.parse.urlencode({"query":QUERY,"mode":"artlist","format":"json","maxrecords":"75","timespan":"3h","sort":"hybridrel"}); data=json.loads(http_get(GDELT+"?"+q).decode("utf-8","replace"));out=[]
    for a in (data.get("articles") or []) if isinstance(data,dict) else []:
        title=str(a.get("title") or "").strip();link=str(a.get("url") or "").strip()
        if not title or not link:continue
        dom=str(a.get("domain") or domain(link)).lower().removeprefix("www.")
        out.append({"event_id":eid(link,title,"GDELT"),"source":"GDELT","source_type":"GDELT_GLOBAL","domain":dom,"title":title[:500],"summary":"","url":link[:1500],"published_at":iso(parse_date(a.get("seendate") or a.get("date")) or now()),**classify_headline(title,"","GDELT_GLOBAL",dom)})
    return out

def ensure_schema():
    c=get_db()
    try:
        c.executescript("""CREATE TABLE IF NOT EXISTS ai_news_events(event_id TEXT PRIMARY KEY,first_seen_at TEXT,last_seen_at TEXT,published_at TEXT,source TEXT,source_type TEXT,domain TEXT,title TEXT,summary TEXT,url TEXT,category TEXT,direction TEXT,impact_score INTEGER,reliability REAL,high_impact INTEGER,matched_keywords_json TEXT);
CREATE INDEX IF NOT EXISTS idx_ai_news_events_recent ON ai_news_events(published_at DESC,first_seen_at DESC);
CREATE TABLE IF NOT EXISTS ai_news_sources(source TEXT PRIMARY KEY,source_type TEXT,url TEXT,last_attempt_at TEXT,last_success_at TEXT,last_error TEXT,item_count INTEGER);
CREATE TABLE IF NOT EXISTS ai_news_fusion_decisions(id TEXT PRIMARY KEY,user_id INTEGER,created_at TEXT,symbol TEXT,entry_spot REAL,base_decision TEXT,base_confidence INTEGER,base_probabilities_json TEXT,strategy_signal TEXT,strategy_trade_allowed INTEGER,news_bias TEXT,news_strength INTEGER,news_risk_score INTEGER,news_event_count INTEGER,news_high_impact_count INTEGER,news_snapshot_json TEXT,fusion_decision TEXT,fusion_confidence INTEGER,fusion_probabilities_json TEXT,fusion_reasons_json TEXT,market_reaction TEXT,relation_to_base TEXT,complete INTEGER DEFAULT 0,completed_at TEXT,trade_blocking INTEGER DEFAULT 0,order_execution INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ai_news_fusion_outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT,decision_id TEXT,user_id INTEGER,horizon_minutes INTEGER,evaluated_at TEXT,exit_spot REAL,spot_change REAL,fusion_signed_spot_points REAL,base_signed_spot_points REAL,noise_threshold_points REAL,fusion_outcome TEXT,base_outcome TEXT,fusion_vs_base_benefit_spot_points REAL,UNIQUE(decision_id,horizon_minutes));
CREATE TABLE IF NOT EXISTS ai_news_runtime(singleton INTEGER PRIMARY KEY CHECK(singleton=1),news_version TEXT,instance_id TEXT,started_at TEXT,heartbeat_at TEXT,last_fetch_at TEXT,last_error TEXT,trade_blocking INTEGER DEFAULT 0,order_execution INTEGER DEFAULT 0);""")
        t=iso();c.execute("""INSERT INTO ai_news_runtime VALUES(1,?,?,?,?,NULL,NULL,0,0) ON CONFLICT(singleton) DO UPDATE SET news_version=excluded.news_version,instance_id=excluded.instance_id,heartbeat_at=excluded.heartbeat_at,trade_blocking=0,order_execution=0""",(VERSION,_instance,t,t));c.commit()
    finally:c.close()
def source_status(source,st,url,ok,count,error=""):
    c=get_db()
    try:
        t=iso();c.execute("""INSERT INTO ai_news_sources VALUES(?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET source_type=excluded.source_type,url=excluded.url,last_attempt_at=excluded.last_attempt_at,last_success_at=CASE WHEN excluded.last_success_at IS NOT NULL THEN excluded.last_success_at ELSE ai_news_sources.last_success_at END,last_error=excluded.last_error,item_count=excluded.item_count""",(source,st,url,t,t if ok else None,None if ok else error[:300],count));c.commit()
    finally:c.close()
def persist(events):
    c=get_db();t=iso()
    try:
        for e in events:c.execute("""INSERT INTO ai_news_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,impact_score=MAX(ai_news_events.impact_score,excluded.impact_score),reliability=MAX(ai_news_events.reliability,excluded.reliability),high_impact=MAX(ai_news_events.high_impact,excluded.high_impact)""",(e["event_id"],t,t,e.get("published_at") or t,e.get("source"),e.get("source_type"),e.get("domain"),e.get("title"),e.get("summary"),e.get("url"),e.get("category"),e.get("direction"),integer(e.get("impact_score")),num(e.get("reliability")),1 if e.get("high_impact") else 0,dumps(e.get("matched_keywords") or [])))
        c.execute("DELETE FROM ai_news_events WHERE datetime(COALESCE(published_at,first_seen_at))<datetime('now','-14 days')");c.commit()
    finally:c.close()

def refresh():
    global _last_fetch,_last_error
    ensure_schema();total=0;errors=[]
    for s,st,u in RSS:
        try:ev=fetch_rss(s,st,u);persist(ev);source_status(s,st,u,True,len(ev));total+=len(ev)
        except Exception as x:msg=f"{s}:{type(x).__name__}:{str(x)[:120]}";errors.append(msg);source_status(s,st,u,False,0,msg)
    try:ev=fetch_gdelt();persist(ev);source_status("GDELT","GDELT_GLOBAL",GDELT,True,len(ev));total+=len(ev)
    except Exception as x:msg=f"GDELT:{type(x).__name__}:{str(x)[:120]}";errors.append(msg);source_status("GDELT","GDELT_GLOBAL",GDELT,False,0,msg)
    _last_fetch=iso();_last_error="; ".join(errors)[:500] if errors else None
    return {"fetched_items":total,"errors":errors}
def fetch_worker():
    global _fetching,_last_error
    try:r=refresh();print(f"AI NEWS RAILWAY | fetch | items={r['fetched_items']} | errors={len(r['errors'])} | trade blocking OFF")
    except Exception as x:_last_error=f"{type(x).__name__}:{str(x)[:300]}"
    finally:
        with _lock:_fetching=False
def schedule_fetch(force=False):
    global _fetching,_last_fetch_mono
    if str(os.getenv("OKAI_NEWS_FETCH_ENABLED","1")).lower() in {"0","false","no","off"}:return
    m=time.monotonic()
    with _lock:
        if _fetching or (not force and m-_last_fetch_mono<FETCH):return
        _fetching=True;_last_fetch_mono=m
    threading.Thread(target=fetch_worker,name="okai-news-fetch",daemon=True).start()

def aggregate():
    ensure_schema();cur=now();cut=iso(cur-timedelta(minutes=LOOKBACK));c=get_db()
    try:rows=c.execute("SELECT * FROM ai_news_events WHERE datetime(COALESCE(published_at,first_seen_at))>=datetime(?) ORDER BY high_impact DESC,impact_score DESC LIMIT 150",(cut,)).fetchall()
    finally:c.close()
    signed=total=0.0;risks=[];cats={};sources={};heads=[];high=0
    for r in rows:
        x=dict(r);d=parse_date(x.get("published_at") or x.get("first_seen_at"));age=max(0,(cur-d).total_seconds()/60) if d else 100;fresh=1 if age<=15 else .85 if age<=60 else .62 if age<=LOOKBACK else 0
        if not fresh:continue
        w=num(x.get("impact_score"))*num(x.get("reliability"),.6)*fresh;total+=w;risks.append(w)
        signed+=w if x.get("direction")=="CE" else -w if x.get("direction")=="PE" else 0;high+=int(bool(x.get("high_impact")))
        cats[x.get("category") or "GENERAL"]=cats.get(x.get("category") or "GENERAL",0)+1;sources[x.get("source") or "UNKNOWN"]=sources.get(x.get("source") or "UNKNOWN",0)+1
        if len(heads)<12:heads.append({"title":x.get("title"),"source":x.get("source"),"domain":x.get("domain"),"published_at":x.get("published_at"),"category":x.get("category"),"direction":x.get("direction"),"impact_score":integer(x.get("impact_score")),"high_impact":bool(x.get("high_impact"))})
    if total<=0:return {"news_bias":"NEUTRAL","news_strength":0,"news_risk_score":0,"event_count":0,"high_impact_count":0,"fresh":False,"top_headlines":[],"categories":{},"sources":{},"last_fetch_at":_last_fetch,"fetch_error":_last_error}
    ratio=signed/total;bias="CE" if ratio>=.12 else "PE" if ratio<=-.12 else "NEUTRAL";risk=int(clamp((max(risks) if risks else 0)+min(22,len(rows)*1.8)+(18 if abs(ratio)<.1 and high else 0),0,100))
    return {"news_bias":bias,"news_strength":int(clamp(abs(ratio)*100,0,100)),"news_risk_score":risk,"event_count":len(rows),"high_impact_count":high,"fresh":True,"top_headlines":heads,"categories":cats,"sources":sources,"last_fetch_at":_last_fetch,"fetch_error":_last_error}

def reaction(market,bias):
    p=num(market.get("price"));ef=num(market.get("ema_fast",market.get("ema9")),p);es=num(market.get("ema_slow",market.get("ema21")),p);v=num(market.get("vwap"),p);s=direction(market.get("signal_direction") or market.get("signal"));up=int(ef>es)+int(p>v)+int(s=="CE");down=int(ef<es)+int(p<v)+int(s=="PE");move="CE" if up>=2 else "PE" if down>=2 else "NEUTRAL"
    status="NEWS_MARKET_REACTION_CONFIRMED" if bias in {"CE","PE"} and move==bias else "NEWS_MARKET_REACTION_CONFLICT" if bias in {"CE","PE"} and move in {"CE","PE"} else "NEWS_MARKET_REACTION_UNCLEAR"
    return move,status
def fuse_news_with_market(base,news,market):
    bp=dict(base.get("probabilities") or {});scores={"CE":num(bp.get("CE")),"PE":num(bp.get("PE")),"NO_TRADE":num(bp.get("NO_TRADE"),100)};bd=direction(base.get("decision"));bias=direction(news.get("news_bias"));strength=integer(news.get("news_strength"));risk=integer(news.get("news_risk_score"));high=integer(news.get("high_impact_count"));move,status=reaction(market,bias);reasons=[status]
    if news.get("fresh") and bias in {"CE","PE"}:
        sh=clamp(8+strength*.22+high*2,8,34);opp="PE" if bias=="CE" else "CE"
        if status=="NEWS_MARKET_REACTION_CONFIRMED":scores[bias]+=sh*1.5;scores[opp]-=sh*1.2;scores["NO_TRADE"]-=sh*.15;reasons.append("NEWS_DIRECTION_CONFIRMED")
        elif status=="NEWS_MARKET_REACTION_CONFLICT":scores["NO_TRADE"]+=sh*.85;scores[bias]+=sh*.2;reasons.append("NEWS_PRICE_CONFLICT")
        else:scores[bias]+=sh*.45;scores["NO_TRADE"]+=sh*.4;reasons.append("WAIT_FOR_NEWS_REACTION")
    elif risk>=65:scores["NO_TRADE"]+=28;reasons.append("HIGH_NEWS_RISK_WITHOUT_CLEAR_DIRECTION")
    else:reasons.append("NO_FRESH_OR_DIRECTIONAL_NEWS")
    if risk>=85:scores["NO_TRADE"]+=18;reasons.append("EXTREME_EVENT_RISK")
    scores={k:max(0,v) for k,v in scores.items()};tot=sum(scores.values()) or 1;probs={k:int(round(v/tot*100)) for k,v in scores.items()};probs["NO_TRADE"]+=100-sum(probs.values());fd,conf=max(probs.items(),key=lambda x:x[1])
    rel="SAME_AS_BASE" if fd==bd else "NEWS_WOULD_BLOCK_BASE" if fd=="NO_TRADE" and bd in {"CE","PE"} else "NEWS_OPPOSITE_BASE" if fd in {"CE","PE"} and bd in {"CE","PE"} else "NEWS_DIRECTIONAL_WHEN_BASE_WAITED" if fd in {"CE","PE"} and bd=="NO_TRADE" else "DIFFERENT"
    return {"success":True,"model_version":VERSION,"decision":fd,"confidence":conf,"probabilities":probs,"base_decision":bd,"news_bias":bias,"news_strength":strength,"news_risk_score":risk,"market_reaction_direction":move,"market_reaction":status,"relation_to_base":rel,"reasons":reasons,"trade_blocking":False,"order_execution":False,"mode":"NEWS_FUSION_SHADOW_ONLY"}

def users():
    c=get_db()
    try:return [int(r["user_id"]) for r in c.execute("SELECT DISTINCT user_id FROM (SELECT user_id FROM user_bot_state WHERE is_running=1 UNION ALL SELECT user_id FROM bot_status WHERE is_running=1)").fetchall()]
    except Exception:return []
    finally:c.close()
def signed(side,e,x):return x-e if side=="CE" else e-x if side=="PE" else 0.0
def threshold(e,h):return round(max(4,abs(e)*.00018)*{5:.8,15:1,30:1.25}[h],2)
def register(uid,m,b,n,f):
    p=num(m.get("price"))
    if p<=0 or not m.get("market_open") or not m.get("feed_connected"):return
    fd,bd=direction(f.get("decision")),direction(b.get("decision"));rel=str(f.get("relation_to_base"));sym=str(m.get("symbol") or "NIFTY").upper()
    if rel=="SAME_AS_BASE" and not n.get("high_impact_count"):return
    c=get_db()
    try:
        last=c.execute("SELECT * FROM ai_news_fusion_decisions WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT 1",(uid,)).fetchone()
        if last:
            t=parse_date(last["created_at"]);same=(str(last["symbol"]),str(last["base_decision"]),str(last["fusion_decision"]),str(last["news_bias"]),str(last["relation_to_base"]),int(num(last["entry_spot"])/5))==(sym,bd,fd,str(n.get("news_bias")),rel,int(p/5))
            if t and same and (now()-t).total_seconds()<300:return
        did=uuid.uuid4().hex[:20];c.execute("INSERT INTO ai_news_fusion_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,0,0)",(did,uid,iso(),sym,round(p,2),bd,integer(b.get("confidence")),dumps(b.get("probabilities") or {}),direction(m.get("signal_direction") or m.get("signal")),1 if m.get("server_trade_allowed") else 0,str(n.get("news_bias")),integer(n.get("news_strength")),integer(n.get("news_risk_score")),integer(n.get("event_count")),integer(n.get("high_impact_count")),dumps(n),fd,integer(f.get("confidence")),dumps(f.get("probabilities") or {}),dumps(f.get("reasons") or []),str(f.get("market_reaction")),rel));c.commit()
    finally:c.close()
def observe(uid,m):
    p=num(m.get("price"));sym=str(m.get("symbol") or "NIFTY").upper()
    if p<=0 or not m.get("feed_connected"):return
    c=get_db()
    try:
        for r in c.execute("SELECT * FROM ai_news_fusion_decisions WHERE user_id=? AND symbol=? AND complete=0",(uid,sym)).fetchall():
            t=parse_date(r["created_at"])
            if not t:continue
            e=num(r["entry_spot"]);fd=str(r["fusion_decision"]);bd=str(r["base_decision"]);fp=signed(fd,e,p);bp=signed(bd,e,p)
            for h in HORIZONS:
                if (now()-t).total_seconds()<h*60 or c.execute("SELECT 1 FROM ai_news_fusion_outcomes WHERE decision_id=? AND horizon_minutes=?",(r["id"],h)).fetchone():continue
                th=threshold(e,h);out=lambda d,x:"CORRECT_SKIP" if d=="NO_TRADE" and abs(p-e)<th else "MISSED_MOVE" if d=="NO_TRADE" else "WIN" if x>=th else "LOSS" if x<=-th else "FLAT";c.execute("INSERT OR IGNORE INTO ai_news_fusion_outcomes VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?)",(r["id"],uid,h,iso(),round(p,2),round(p-e,2),round(fp,2),round(bp,2),th,out(fd,fp),out(bd,bp),round(-bp if fd=="NO_TRADE" else fp-bp,2)))
            if integer(c.execute("SELECT COUNT(*) n FROM ai_news_fusion_outcomes WHERE decision_id=?",(r["id"],)).fetchone()["n"])>=3:c.execute("UPDATE ai_news_fusion_decisions SET complete=1,completed_at=? WHERE id=?",(iso(),r["id"]))
        c.commit()
    finally:c.close()

def get_news_summary(uid,recent_limit=20):
    ensure_schema();n=aggregate();c=get_db()
    try:
        rows=c.execute("SELECT * FROM ai_news_fusion_decisions WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT ?",(uid,max(1,min(integer(recent_limit,20),50)))).fetchall();primary=c.execute("SELECT * FROM ai_news_fusion_outcomes WHERE user_id=? AND horizon_minutes=15",(uid,)).fetchall();recent=[]
        for r in rows:
            x=dict(r)
            for a,b,d in (("base_probabilities_json","base_probabilities",{}),("news_snapshot_json","news_snapshot",{}),("fusion_probabilities_json","fusion_probabilities",{}),("fusion_reasons_json","fusion_reasons",[])):x[b]=loads(x.pop(a),d)
            x["outcomes"]=[dict(o) for o in c.execute("SELECT * FROM ai_news_fusion_outcomes WHERE decision_id=? ORDER BY horizon_minutes",(x["id"],)).fetchall()];recent.append(x)
        wins=sum(r["fusion_outcome"]=="WIN" for r in primary);losses=sum(r["fusion_outcome"]=="LOSS" for r in primary);resolved=wins+losses;benefit=round(sum(num(r["fusion_vs_base_benefit_spot_points"]) for r in primary),2)
        return {"success":True,"news_version":VERSION,"mode":"RAILWAY_NEWS_FUSION_SHADOW_ONLY","location":"RAILWAY","trade_blocking":False,"order_execution":False,"current_news":n,"summary":{"fusion_15m_wins":wins,"fusion_15m_losses":losses,"fusion_15m_hit_rate_percent":round(wins/resolved*100,2) if resolved else None,"fusion_better_than_base_count":sum(num(r["fusion_vs_base_benefit_spot_points"])>0 for r in primary),"fusion_worse_than_base_count":sum(num(r["fusion_vs_base_benefit_spot_points"])<0 for r in primary),"estimated_net_benefit_vs_base_spot_points_15m":benefit},"sources":[dict(r) for r in c.execute("SELECT * FROM ai_news_sources ORDER BY source").fetchall()],"storage":get_db_storage_info(),"recent_decisions":recent}
    finally:c.close()
def news_health():
    ensure_schema();c=get_db()
    try:cnt=c.execute("SELECT COUNT(*) n FROM ai_news_events WHERE datetime(COALESCE(published_at,first_seen_at))>=datetime('now','-3 hours')").fetchone()["n"];src=c.execute("SELECT COUNT(*) sources,SUM(CASE WHEN last_success_at IS NOT NULL THEN 1 ELSE 0 END) successful_sources,SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) failing_sources FROM ai_news_sources").fetchone()
    finally:c.close()
    return {"success":True,"news_version":VERSION,"started":bool(_started),"thread_alive":bool(_thread and _thread.is_alive()),"last_cycle_at":_last_cycle,"last_fetch_at":_last_fetch,"last_error":_last_error,"recent_event_count":integer(cnt),"source_counts":dict(src),"storage":get_db_storage_info(),"location":"RAILWAY","trade_blocking":False,"order_execution":False}
def cycle():
    if not callable(_builder):return
    schedule_fetch();n=aggregate()
    for uid in users():
        try:m=dict(_builder(uid) or {});observe(uid,m);b=predict(m);f=fuse_news_with_market(b,n,m);register(uid,m,b,n,f)
        except Exception as x:print(f"AI NEWS FUSION | cycle warning | user={uid} | {type(x).__name__}:{str(x)[:180]}")
def loop():
    global _last_cycle,_last_error
    schedule_fetch(True)
    while True:
        try:cycle();_last_cycle=iso()
        except Exception as x:_last_error=f"{type(x).__name__}:{str(x)[:300]}"
        time.sleep(POLL)
def start_news_intelligence(snapshot_builder):
    global _started,_thread,_builder
    with _lock:
        _builder=snapshot_builder
        if _started and _thread and _thread.is_alive():return news_health()
        ensure_schema();_started=True;_thread=threading.Thread(target=loop,name="okai-railway-news-fusion",daemon=True);_thread.start();print(f"AI NEWS RAILWAY {VERSION} active | GDELT + RBI + FED | trade blocking OFF | order execution OFF");return news_health()

"""Broker-neutral calibrated advanced intelligence V2.

Uses the proven V1 broker adapters for Angel One, Upstox and Zerodha, then adds
both-side option outcomes, estimated charges/slippage, global/institutional
context and an automatically calibrated walk-forward model. Shadow-only.
"""
from __future__ import annotations

import math
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from bot import advanced_intelligence as legacy
from bot.advanced_model import MIN_MODEL_SAMPLES, dumps, fuse, loads, train
from bot.option_chain import STRIKE_STEP, get_atm_strike
from bot.shared_ai import predict
from database import get_db, get_db_storage_info

VERSION = "OKAI-BROKER-NEUTRAL-ADVANCED-AI-V2"
MODEL_VERSION = VERSION + "-SOFTMAX-V1"
HORIZONS = (5, 15, 30)
PRIMARY = 15
POLL = 15
REFRESH = 60
SPACING = 300
RETRAIN_STEP = 50
SLIPPAGE_PERCENT = 0.15
SUPPORTED = ("angelone", "upstox", "zerodha")

_lock = threading.RLock()
_started = False
_thread = None
_builder: Optional[Callable[[int], Dict[str, Any]]] = None
_instance = uuid.uuid4().hex[:12]
_last_cycle = None
_last_error = None
_last_feature_at: Dict[int, float] = {}
_last_train_check = 0.0


def num(value, default=0.0):
    return legacy.num(value, default)


def integer(value, default=0):
    return legacy.integer(value, default)


def side(value):
    return legacy.side(value)


def iso(value=None):
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse(value):
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def schema():
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_advanced_v2_decisions(
                id TEXT PRIMARY KEY,user_id INTEGER,created_at TEXT,broker TEXT,
                symbol TEXT,spot REAL,base_decision TEXT,base_confidence INTEGER,
                base_probabilities_json TEXT,news_json TEXT,global_json TEXT,
                option_json TEXT,feature_json TEXT,model_json TEXT,
                fusion_decision TEXT,fusion_confidence INTEGER,
                fusion_probabilities_json TEXT,reasons_json TEXT,
                ce_contract_json TEXT,pe_contract_json TEXT,
                complete INTEGER DEFAULT 0,completed_at TEXT,
                trade_blocking INTEGER DEFAULT 0,order_execution INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_adv_v2_user
            ON ai_advanced_v2_decisions(user_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS ai_advanced_v2_outcomes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,decision_id TEXT,user_id INTEGER,
                horizon INTEGER,evaluated_at TEXT,spot_exit REAL,ce_exit_bid REAL,
                pe_exit_bid REAL,ce_gross REAL,pe_gross REAL,ce_net REAL,pe_net REAL,
                fusion_net REAL,base_net REAL,best_label TEXT,benefit REAL,
                UNIQUE(decision_id,horizon)
            );
            CREATE TABLE IF NOT EXISTS ai_advanced_v2_model(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),model_version TEXT,
                trained_at TEXT,sample_count INTEGER DEFAULT 0,
                validation_count INTEGER DEFAULT 0,validation_accuracy REAL DEFAULT 0,
                majority_accuracy REAL DEFAULT 0,validation_net_utility REAL DEFAULT 0,
                active INTEGER DEFAULT 0,model_json TEXT DEFAULT '{}',
                calibration_json TEXT DEFAULT '{}',last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS ai_advanced_v2_runtime(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT,
                instance_id TEXT,started_at TEXT,heartbeat_at TEXT,last_error TEXT,
                trade_blocking INTEGER DEFAULT 0,order_execution INTEGER DEFAULT 0
            );
            """
        )
        stamp = iso()
        conn.execute(
            """INSERT INTO ai_advanced_v2_runtime VALUES(1,?,?,?,?,NULL,0,0)
               ON CONFLICT(singleton) DO UPDATE SET version=excluded.version,
               instance_id=excluded.instance_id,heartbeat_at=excluded.heartbeat_at,
               trade_blocking=0,order_execution=0""",
            (VERSION, _instance, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def users():
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT DISTINCT user_id FROM(
               SELECT user_id FROM user_bot_state WHERE is_running=1
               UNION ALL SELECT user_id FROM bot_status WHERE is_running=1)"""
        ).fetchall()
        return [int(row["user_id"]) for row in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _norm_depth(raw):
    raw = dict(raw or {})
    depth = raw.get("depth") or {}
    buy = (depth.get("buy") or [{}])[0] if isinstance(depth, Mapping) else {}
    sell = (depth.get("sell") or [{}])[0] if isinstance(depth, Mapping) else {}
    ltp = num(raw.get("ltp", raw.get("last_price", raw.get("lastPrice"))))
    bid = num(raw.get("best_bid", raw.get("bid_price")), num(buy.get("price")))
    ask = num(raw.get("best_ask", raw.get("ask_price")), num(sell.get("price")))
    bid_qty = num(raw.get("bid_qty"), num(buy.get("quantity")))
    ask_qty = num(raw.get("ask_qty"), num(sell.get("quantity")))
    if ltp > 0 and bid <= 0:
        bid = ltp * .9985
    if ltp > 0 and ask <= 0:
        ask = ltp * 1.0015
    total = bid_qty + ask_qty
    return {
        "ltp": ltp, "oi": num(raw.get("oi", raw.get("open_interest"))),
        "volume": num(raw.get("volume")), "bid": bid, "ask": ask,
        "bid_qty": bid_qty, "ask_qty": ask_qty,
        "spread_pct": (ask-bid)/max(.05,ltp)*100 if ask and bid else 0,
        "imbalance": (bid_qty-ask_qty)/total if total else 0,
    }


def _years(expiry):
    text = str(expiry or "")[:10]
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return 1/365
    ist = timezone(timedelta(hours=5,minutes=30))
    end = datetime(day.year,day.month,day.day,15,30,tzinfo=ist)
    return max(900,(end-datetime.now(timezone.utc).astimezone(ist)).total_seconds())/(365*86400)


def _cdf(x):
    return .5*(1+math.erf(x/math.sqrt(2)))


def _pdf(x):
    return math.exp(-.5*x*x)/math.sqrt(2*math.pi)


def _bs_price(option_side,spot,strike,years,rate,sigma):
    if min(spot,strike,years,sigma)<=0:
        return 0
    root=math.sqrt(years)
    d1=(math.log(spot/strike)+(rate+sigma*sigma/2)*years)/(sigma*root)
    d2=d1-sigma*root
    return spot*_cdf(d1)-strike*math.exp(-rate*years)*_cdf(d2) if option_side=="CE" else strike*math.exp(-rate*years)*_cdf(-d2)-spot*_cdf(-d1)


def estimated_greeks(option_side,premium,spot,strike,expiry):
    years=_years(expiry)
    rate=num(os.getenv("OKAI_RISK_FREE_RATE"),.065)
    low,high=.01,5.0
    for _ in range(50):
        sigma=(low+high)/2
        if _bs_price(option_side,spot,strike,years,rate,sigma)>premium:
            high=sigma
        else:
            low=sigma
    sigma=(low+high)/2
    root=math.sqrt(years)
    d1=(math.log(spot/strike)+(rate+sigma*sigma/2)*years)/(sigma*root)
    d2=d1-sigma*root
    delta=_cdf(d1) if option_side=="CE" else _cdf(d1)-1
    gamma=_pdf(d1)/(spot*sigma*root)
    vega=spot*_pdf(d1)*root/100
    theta=(-(spot*_pdf(d1)*sigma)/(2*root)+(-rate*strike*math.exp(-rate*years)*_cdf(d2) if option_side=="CE" else rate*strike*math.exp(-rate*years)*_cdf(-d2)))/365
    return {"iv":sigma*100,"delta":delta,"gamma":gamma,"theta":theta,"vega":vega}


def _zerodha_quote(obj,contract,spot):
    key=f"{contract.get('exchange','NFO')}:{contract.get('symbol')}"
    raw=dict((obj.kite.quote([key]) or {}).get(key) or {})
    quote=_norm_depth(raw)
    quote.update({k:contract.get(k) for k in ("side","symbol","token","exchange","expiry","strike","lot_size")})
    quote.update(estimated_greeks(contract.get("side"),quote["ltp"],spot,num(contract.get("strike")),contract.get("expiry")))
    return quote


def _quote(broker,obj,contract,spot):
    if broker=="zerodha":
        return _zerodha_quote(obj,contract,spot)
    quote=dict(legacy.quote(broker,obj,contract) or {})
    quote.update({k:contract.get(k) for k in ("side","symbol","token","exchange","expiry","strike","lot_size")})
    greek={}
    if broker=="angelone":
        try:
            day=datetime.strptime(str(contract.get("expiry"))[:10],"%Y-%m-%d").strftime("%d%b%Y").upper()
            underlying=str(contract.get("underlying") or "NIFTY")
            payload=obj.optionGreek({"name":underlying,"expirydate":day}) if hasattr(obj,"optionGreek") else {}
            for row in payload.get("data") or []:
                if abs(num(row.get("strikePrice"))-num(contract.get("strike")))<.01 and side(row.get("optionType"))==contract.get("side"):
                    greek={k:num(row.get(k)) for k in ("delta","gamma","theta","vega","iv")}
                    break
        except Exception:
            pass
    if not greek and num(quote.get("ltp"))>0:
        greek=estimated_greeks(contract.get("side"),num(quote.get("ltp")),spot,num(contract.get("strike")),contract.get("expiry"))
    quote.update(greek)
    return quote


def _zerodha_chain(obj,underlying,spot,expiry):
    exchange="BFO" if underlying=="SENSEX" else "NFO"
    atm=get_atm_strike(underlying,spot)
    step=STRIKE_STEP.get(underlying,50)
    wanted={atm+i*step for i in range(-3,4)}
    contracts=[]
    for row in obj.kite.instruments(exchange):
        if str(row.get("name") or "").upper()!=underlying:
            continue
        option_side=str(row.get("instrument_type") or "").upper()
        ex=row.get("expiry")
        ex=ex.date() if isinstance(ex,datetime) else ex
        if option_side not in {"CE","PE"} or num(row.get("strike")) not in wanted or (expiry and str(ex)!=str(expiry)[:10]):
            continue
        contracts.append({"side":option_side,"symbol":row.get("tradingsymbol"),"token":str(row.get("instrument_token")),"exchange":exchange,"expiry":str(ex),"strike":num(row.get("strike")),"lot_size":integer(row.get("lot_size"),1),"underlying":underlying})
    if not contracts:
        return []
    keys=[f"{x['exchange']}:{x['symbol']}" for x in contracts]
    raw=obj.kite.quote(keys) or {}
    result={strike:{"strike":strike,"call":{},"put":{}} for strike in wanted}
    for contract,key in zip(contracts,keys):
        quote=_norm_depth(raw.get(key))
        quote.update(estimated_greeks(contract["side"],quote["ltp"],spot,contract["strike"],contract["expiry"]))
        quote["greeks"]={k:quote.get(k) for k in ("delta","gamma","theta","vega","iv")}
        result[contract["strike"]]["call" if contract["side"]=="CE" else "put"]=quote
    return [result[x] for x in sorted(result) if result[x]["call"] or result[x]["put"]]


def _upstox_analytics(obj,underlying,expiry):
    if not hasattr(obj,"_h"):
        return {}
    try:
        import requests
    except Exception:
        return {}
    key=legacy.UP_KEYS.get(underlying)
    today=(datetime.now(timezone.utc)+timedelta(hours=5,minutes=30)).date().isoformat()
    result={}
    specs=(
        ("oi","oi",{"instrument_key":key,"expiry":expiry,"date":today}),
        ("change_oi","change-oi",{"instrument_key":key,"expiry":expiry,"date":today,"interval":1}),
        ("pcr","pcr",{"instrument_key":key,"expiry":expiry,"date":today,"bucket_interval":60}),
        ("max_pain","max-pain",{"instrument_key":key,"expiry":expiry,"date":today}),
        ("fii","fii",[("data_type","NSE_FO|INDEX_FUTURES"),("data_type","NSE_FO|INDEX_OPTIONS"),("data_type","NSE_EQ|CASH"),("interval","1D")]),
        ("dii","dii",{"data_type":"NSE_EQ|CASH","interval":"1D"}),
    )
    for name,endpoint,params in specs:
        try:
            response=requests.get(f"https://api.upstox.com/v2/market/{endpoint}",params=params,headers=obj._h(),timeout=6)
            payload=response.json()
            if response.status_code==200 and payload.get("status")=="success":
                result[name]=payload.get("data")
        except Exception:
            pass
    return result


def _institutional_score(analytics):
    total=0.0
    for name in ("fii","dii"):
        payload=analytics.get(name)
        if not isinstance(payload,Mapping):
            continue
        for rows in payload.values():
            if isinstance(rows,list) and rows and isinstance(rows[0],Mapping):
                total+=num(rows[0].get("buy_amount"))-num(rows[0].get("sell_amount"))
    if not total:
        return 0.0,0.0
    score=math.copysign(min(100,math.log10(abs(total)+1)*18),total)
    return total,score


def _extra_global():
    symbols={"us_10y":"^TNX"}
    gift=str(os.getenv("OKAI_GIFT_NIFTY_YAHOO_SYMBOL","")).strip()
    if gift:
        symbols["gift_nifty"]=gift
    result={}
    for name,symbol in symbols.items():
        try:
            import json
            encoded=urllib.parse.quote(symbol,safe="")
            request=urllib.request.Request(f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=2d&interval=5m",headers={"User-Agent":"OptionKingAI/2.0"})
            with urllib.request.urlopen(request,timeout=4) as response:
                payload=json.loads(response.read().decode("utf-8","replace"))
            meta=(((payload.get("chart") or {}).get("result") or [{}])[0].get("meta") or {})
            price=num(meta.get("regularMarketPrice"))
            previous=num(meta.get("chartPreviousClose",meta.get("previousClose")))
            result[name]={"symbol":symbol,"price":price,"previous_close":previous,"change_pct":((price/previous)-1)*100 if price and previous else 0}
        except Exception as exc:
            result[name]={"symbol":symbol,"price":0,"change_pct":0,"error":f"{type(exc).__name__}:{str(exc)[:80]}"}
    return result


def _global_context():
    base=dict(legacy.refresh_global() or {})
    markets=dict(base.get("markets") or {})
    markets.update(_extra_global())
    score=num(base.get("risk_on_score"))
    yield_change=num((markets.get("us_10y") or {}).get("change_pct"))
    score-=max(0,yield_change)*.6
    score+=max(0,-yield_change)*.25
    gift=num((markets.get("gift_nifty") or {}).get("change_pct"))
    score+=gift*1.2
    return {"global_bias":"CE" if score>=.9 else "PE" if score<=-.9 else "NEUTRAL","global_strength":int(legacy.clamp(abs(score)*14,0,100)),"global_risk_score":int(legacy.clamp(abs(yield_change)*12+abs(num((markets.get("india_vix") or {}).get("change_pct")))*10,0,100)),"risk_on_score":score,"instruments":markets,"fresh":bool(markets),"trade_blocking":False,"order_execution":False}


def _option_snapshot(uid,market,previous):
    underlying=str(market.get("symbol") or "NIFTY").upper()
    spot=num(market.get("price"))
    broker,obj=legacy.broker_session(uid)
    contracts={}
    for option_side in ("CE","PE"):
        contract=dict(legacy.contract(broker,obj,underlying,spot,option_side) or {})
        contract.update({"side":option_side,"option_type":option_side,"underlying":underlying})
        contracts[option_side]=contract
    quotes={option_side:_quote(broker,obj,contract,spot) for option_side,contract in contracts.items()}
    expiry=contracts["CE"].get("expiry") or contracts["PE"].get("expiry")
    rows=_zerodha_chain(obj,underlying,spot,expiry) if broker=="zerodha" else legacy.chain(broker,obj,underlying,spot,contracts["CE"])
    features=dict(legacy.option_features(rows,spot) or {})
    analytics=_upstox_analytics(obj,underlying,str(expiry)[:10]) if broker=="upstox" else {}
    institutional_net,institutional_score=_institutional_score(analytics)
    call_oi=sum(num((x.get("call") or {}).get("oi")) for x in rows)
    put_oi=sum(num((x.get("put") or {}).get("oi")) for x in rows)
    call_change=sum(num((x.get("call") or {}).get("change_oi")) for x in rows)
    put_change=sum(num((x.get("put") or {}).get("change_oi")) for x in rows)
    pcr=num(features.get("pcr_oi"),put_oi/max(1,call_oi))
    max_pain=num(features.get("max_pain"))
    avg_spread=(num(quotes["CE"].get("spread_pct"))+num(quotes["PE"].get("spread_pct")))/2
    quality=int(legacy.clamp(30+(30 if rows else 0)+(20 if all(num(q.get("ltp"))>0 for q in quotes.values()) else 0)+max(0,20-avg_spread*8),0,100))
    option_direction=side(features.get("direction"))
    strength=int(legacy.clamp(abs(num(features.get("direction_score")))*20,0,100))
    return {"success":True,"broker":broker,"capabilities":{"angelone":"scrip master + SmartAPI FULL OI/depth + native/estimated Greeks","upstox":"native chain/Greeks + OI/PCR/max-pain/FII-DII analytics","zerodha":"Kite instruments + full quote OI/depth + estimated Greeks"}.get(broker),"underlying":underlying,"spot_price":spot,"expiry":str(expiry),"atm_strike":get_atm_strike(underlying,spot),"call_oi":call_oi,"put_oi":put_oi,"pcr":pcr,"call_change_oi":call_change-num(previous.get("call_oi")),"put_change_oi":put_change-num(previous.get("put_oi")),"max_pain":max_pain,"max_pain_distance_percent":((max_pain-spot)/spot*100) if max_pain else 0,"iv_skew":num(features.get("iv_skew")),"depth_imbalance":num(quotes["CE"].get("imbalance"))-num(quotes["PE"].get("imbalance")),"average_spread_percent":avg_spread,"option_bias":option_direction,"option_strength":strength,"data_quality_score":quality,"institutional_net_amount":institutional_net,"institutional_bias_score":institutional_score,"best_ce":{**contracts["CE"],**quotes["CE"]},"best_pe":{**contracts["PE"],**quotes["PE"]},"near_atm_chain":rows,"native_analytics":analytics}


def _registry():
    conn=get_db()
    try:
        row=conn.execute("SELECT * FROM ai_advanced_v2_model WHERE singleton=1").fetchone()
    finally:
        conn.close()
    if not row:
        return {"active":False,"sample_count":0}
    item=dict(row)
    item["active"]=bool(item.get("active"))
    item["model"]=loads(item.pop("model_json"),{})
    item["calibration"]=loads(item.pop("calibration_json"),{})
    return item


def _previous(uid):
    conn=get_db()
    try:
        row=conn.execute("SELECT option_json FROM ai_advanced_v2_decisions WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT 1",(uid,)).fetchone()
    finally:
        conn.close()
    return loads(row["option_json"],{}) if row else {}


def _entry(contract):
    ask=num(contract.get("ask"));ltp=num(contract.get("ltp"))
    return ask if ask>0 else ltp*(1+SLIPPAGE_PERCENT/100)


def _exit(contract):
    bid=num(contract.get("bid"));ltp=num(contract.get("ltp"))
    return bid if bid>0 else ltp*(1-SLIPPAGE_PERCENT/100)


def _cost(broker,entry,exit_price,qty):
    fixed=num(os.getenv(f"OKAI_{broker.upper()}_OPTION_ROUND_TRIP_CHARGES_PER_LOT"),80)
    return round(fixed+(entry+exit_price)*qty*num(os.getenv("OKAI_OPTION_CHARGE_BUFFER_RATE"),.00005),2)


def register(uid,market,base,news,glob,options,advanced):
    if not market.get("market_open") or not market.get("feed_connected") or num(market.get("price"))<=0:
        return
    ce=dict(options.get("best_ce") or {});pe=dict(options.get("best_pe") or {})
    if num(ce.get("ltp"))<=0 or num(pe.get("ltp"))<=0:
        return
    conn=get_db()
    try:
        last=conn.execute("SELECT created_at,broker,symbol,fusion_decision FROM ai_advanced_v2_decisions WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT 1",(uid,)).fetchone()
        if last:
            stamp=parse(last["created_at"])
            if stamp and last["broker"]==options.get("broker") and last["symbol"]==str(market.get("symbol") or "NIFTY").upper() and last["fusion_decision"]==side(advanced.get("decision")) and (datetime.now(timezone.utc)-stamp).total_seconds()<SPACING:
                return
        decision_id=uuid.uuid4().hex[:20]
        conn.execute("INSERT INTO ai_advanced_v2_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,0,0)",(decision_id,uid,iso(),options.get("broker"),str(market.get("symbol") or "NIFTY").upper(),num(market.get("price")),side(base.get("decision")),integer(base.get("confidence")),dumps(base.get("probabilities") or {}),dumps(news),dumps(glob),dumps(options),dumps(advanced.get("features") or {}),dumps(advanced.get("calibrated_model") or {}),side(advanced.get("decision")),integer(advanced.get("confidence")),dumps(advanced.get("probabilities") or {}),dumps(advanced.get("reasons") or []),dumps(ce),dumps(pe)))
        conn.commit()
    finally:
        conn.close()


def observe(uid,market):
    spot=num(market.get("price"));symbol=str(market.get("symbol") or "NIFTY").upper()
    if spot<=0 or not market.get("feed_connected"):
        return
    conn=get_db()
    try:
        rows=conn.execute("SELECT * FROM ai_advanced_v2_decisions WHERE user_id=? AND symbol=? AND complete=0",(uid,symbol)).fetchall()
        for row in rows:
            stamp=parse(row["created_at"])
            if not stamp:
                continue
            due=[h for h in HORIZONS if (datetime.now(timezone.utc)-stamp).total_seconds()>=h*60 and not conn.execute("SELECT 1 FROM ai_advanced_v2_outcomes WHERE decision_id=? AND horizon=?",(row["id"],h)).fetchone()]
            if not due:
                continue
            try:
                broker,obj=legacy.broker_session(uid)
                ce0=loads(row["ce_contract_json"],{});pe0=loads(row["pe_contract_json"],{})
                ce1=_quote(broker,obj,ce0,spot);pe1=_quote(broker,obj,pe0,spot)
            except Exception:
                continue
            def pnl(entry_contract,exit_contract):
                qty=max(1,integer(entry_contract.get("lot_size"),1));entry_price=_entry(entry_contract);exit_price=_exit(exit_contract);gross=(exit_price-entry_price)*qty
                return exit_price,gross,gross-_cost(str(row["broker"]),entry_price,exit_price,qty)
            ce_exit,ce_gross,ce_net=pnl(ce0,ce1);pe_exit,pe_gross,pe_net=pnl(pe0,pe1)
            best="NO_TRADE" if max(ce_net,pe_net)<=0 else "CE" if ce_net>=pe_net else "PE"
            def chosen(value):
                return ce_net if side(value)=="CE" else pe_net if side(value)=="PE" else 0
            fusion_net=chosen(row["fusion_decision"]);base_net=chosen(row["base_decision"])
            for horizon in due:
                conn.execute("INSERT OR IGNORE INTO ai_advanced_v2_outcomes VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(row["id"],uid,horizon,iso(),spot,ce_exit,pe_exit,ce_gross,pe_gross,ce_net,pe_net,fusion_net,base_net,best,fusion_net-base_net))
            if integer(conn.execute("SELECT COUNT(*) n FROM ai_advanced_v2_outcomes WHERE decision_id=?",(row["id"],)).fetchone()["n"])>=3:
                conn.execute("UPDATE ai_advanced_v2_decisions SET complete=1,completed_at=? WHERE id=?",(iso(),row["id"]))
        conn.commit()
    finally:
        conn.close()


def maybe_train(force=False):
    conn=get_db()
    try:
        old=conn.execute("SELECT * FROM ai_advanced_v2_model WHERE singleton=1").fetchone()
        last=integer(old["sample_count"]) if old else 0
        rows=conn.execute("SELECT d.created_at,d.feature_json,o.best_label,o.ce_net AS ce_net_pnl,o.pe_net AS pe_net_pnl FROM ai_advanced_v2_outcomes o JOIN ai_advanced_v2_decisions d ON d.id=o.decision_id WHERE o.horizon=? ORDER BY datetime(d.created_at),d.rowid",(PRIMARY,)).fetchall()
        if len(rows)<MIN_MODEL_SAMPLES:
            return {"success":False,"reason":"COLLECTING_DATA","sample_count":len(rows),"minimum_required":MIN_MODEL_SAMPLES}
        if not force and len(rows)<last+RETRAIN_STEP:
            return {"success":True,"reason":"MODEL_CURRENT","sample_count":len(rows)}
        result=train(rows)
        if not result.get("success"):
            return result
        conn.execute("INSERT INTO ai_advanced_v2_model VALUES(1,?,?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(singleton) DO UPDATE SET model_version=excluded.model_version,trained_at=excluded.trained_at,sample_count=excluded.sample_count,validation_count=excluded.validation_count,validation_accuracy=excluded.validation_accuracy,majority_accuracy=excluded.majority_accuracy,validation_net_utility=excluded.validation_net_utility,active=excluded.active,model_json=excluded.model_json,calibration_json=excluded.calibration_json,last_error=NULL",(MODEL_VERSION,iso(),result["sample_count"],result["validation_count"],result["validation_accuracy"],result["majority_baseline_accuracy"],result["validation_net_utility"],1 if result["active"] else 0,dumps(result["model"]),dumps(result["calibration"])))
        conn.commit()
        return result
    finally:
        conn.close()


def collect(uid,market):
    base=predict(dict(market));news=legacy.news();glob=_global_context();options=_option_snapshot(uid,market,_previous(uid));advanced=fuse(market,base,news,glob,options,_registry())
    return base,news,glob,options,advanced


def summary(uid,limit=20):
    schema();conn=get_db()
    try:
        rows=conn.execute("SELECT * FROM ai_advanced_v2_decisions WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT ?",(uid,max(1,min(integer(limit,20),50)))).fetchall()
        recent=[]
        for row in rows:
            item=dict(row)
            for src,dst,default in (("base_probabilities_json","base_probabilities",{}),("news_json","news",{}),("global_json","global",{}),("option_json","option",{}),("feature_json","features",{}),("model_json","model",{}),("fusion_probabilities_json","fusion_probabilities",{}),("reasons_json","reasons",[]),("ce_contract_json","ce_contract",{}),("pe_contract_json","pe_contract",{})):
                item[dst]=loads(item.pop(src),default)
            item["outcomes"]=[dict(x) for x in conn.execute("SELECT * FROM ai_advanced_v2_outcomes WHERE decision_id=? ORDER BY horizon",(item["id"],)).fetchall()]
            recent.append(item)
        primary=conn.execute("SELECT * FROM ai_advanced_v2_outcomes WHERE user_id=? AND horizon=?",(uid,PRIMARY)).fetchall()
    finally:
        conn.close()
    registry=_registry();broker=""
    try:
        broker=legacy.broker_row(uid).get("broker_name") or ""
    except Exception:
        pass
    return {"success":True,"version":VERSION,"mode":"BROKER_NEUTRAL_ADVANCED_SHADOW_ONLY","active_broker":str(broker).lower(),"supported_brokers":list(SUPPORTED),"trade_blocking":False,"order_execution":False,"legacy_v1_data_preserved":True,"charges_note":"Ask-entry/bid-exit with configurable estimated costs; not a broker contract note.","model":{"status":"ACTIVE" if registry.get("active") else "COLLECTING_DATA","sample_count":integer(registry.get("sample_count")),"minimum_samples":MIN_MODEL_SAMPLES,"validation_accuracy":num(registry.get("validation_accuracy")),"majority_accuracy":num(registry.get("majority_accuracy")),"validation_net_utility":num(registry.get("validation_net_utility"))},"summary":{"evaluated_15m":len(primary),"fusion_net_estimated_pnl":round(sum(num(x["fusion_net"]) for x in primary),2),"base_net_estimated_pnl":round(sum(num(x["base_net"]) for x in primary),2),"benefit_vs_base":round(sum(num(x["benefit"]) for x in primary),2)},"storage":get_db_storage_info(),"recent":recent}


def health():
    schema();conn=get_db()
    try:
        runtime=conn.execute("SELECT * FROM ai_advanced_v2_runtime WHERE singleton=1").fetchone();counts=conn.execute("SELECT COUNT(*) n,SUM(complete=0) p FROM ai_advanced_v2_decisions").fetchone();outcomes=conn.execute("SELECT COUNT(*) n FROM ai_advanced_v2_outcomes").fetchone()
    finally:
        conn.close()
    return {"success":True,"version":VERSION,"started":_started,"thread_alive":bool(_thread and _thread.is_alive()),"last_cycle_at":_last_cycle,"last_error":_last_error,"runtime":dict(runtime) if runtime else None,"snapshots":integer(counts["n"] if counts else 0),"pending":integer(counts["p"] if counts else 0),"outcomes":integer(outcomes["n"] if outcomes else 0),"model":{k:v for k,v in _registry().items() if k not in {"model","calibration"}},"supported_brokers":list(SUPPORTED),"storage":get_db_storage_info(),"trade_blocking":False,"order_execution":False}


def heartbeat(error=None):
    global _last_cycle,_last_error
    _last_cycle=iso();_last_error=str(error)[:300] if error else None
    conn=get_db()
    try:
        conn.execute("UPDATE ai_advanced_v2_runtime SET heartbeat_at=?,last_error=?,trade_blocking=0,order_execution=0 WHERE singleton=1",(_last_cycle,_last_error));conn.commit()
    finally:
        conn.close()


def cycle():
    global _last_train_check
    if not callable(_builder):
        return
    for uid in users():
        market=dict(_builder(uid) or {})
        observe(uid,market)
        if time.monotonic()-_last_feature_at.get(uid,0)<REFRESH:
            continue
        _last_feature_at[uid]=time.monotonic()
        if not market.get("market_open") or not market.get("feed_connected") or num(market.get("price"))<=0:
            continue
        try:
            base,news,glob,options,advanced=collect(uid,market)
            register(uid,market,base,news,glob,options,advanced)
        except Exception as exc:
            legacy._sessions.pop(uid,None)
            print(f"AI ADVANCED V2 | user={uid} | {type(exc).__name__}:{str(exc)[:180]}")
    if time.monotonic()-_last_train_check>=1800:
        _last_train_check=time.monotonic();maybe_train(False)


def loop():
    while True:
        error=None
        try:
            cycle()
        except Exception as exc:
            error=f"{type(exc).__name__}:{str(exc)[:240]}"
        try:
            heartbeat(error)
        except Exception:
            pass
        time.sleep(POLL)


def start(snapshot_builder):
    global _started,_thread,_builder
    with _lock:
        _builder=snapshot_builder
        if _started and _thread and _thread.is_alive():
            return health()
        schema();_started=True
        _thread=threading.Thread(target=loop,name="okai-broker-neutral-advanced-v2",daemon=True)
        _thread.start()
        print(f"AI ADVANCED V2 {VERSION} active | Angel One + Upstox + Zerodha | calibrated model + both-side option outcomes | monitor only, trade blocking OFF")
        return health()

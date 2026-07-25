"""Railway broker-neutral advanced AI and exact option outcome monitor V2.

Combines market AI, option intelligence, global/news context and an adaptive
model. Shadow-only: never changes signals, entries, exits, quantities or orders.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from backtest.realism_costs_patch import calculate_option_round_trip_costs
from bot.adaptive_model_v2 import feature_vector, maybe_train_models, model_status, predict_adaptive
from bot.broker_intelligence import (
    BROKER_CAPABILITIES,
    VERSION as BROKER_VERSION,
    get_broker_intelligence,
    option_oi_identity_map,
    selected_contract,
)
from bot.news_intelligence import aggregate as aggregate_news
from bot.global_market_intelligence import snapshot as global_market_snapshot
from bot.shared_ai import predict
from database import get_db, get_db_storage_info

VERSION = "OKAI-ADVANCED-BROKER-NEUTRAL-SHADOW-V2"
HORIZONS = (5, 15, 30)
PRIMARY_HORIZON = 15
POLL_SECONDS = 15
OPTION_REFRESH_SECONDS = 60
RECORD_SPACING_SECONDS = 300

_lock = threading.RLock()
_started = False
_thread = None
_builder: Optional[Callable[[int], Dict[str, Any]]] = None
_instance = uuid.uuid4().hex[:12]
_last_cycle = None
_last_error = None
_option_cache: Dict[int, Dict[str, Any]] = {}
_option_fetch_mono: Dict[int, float] = {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or _now()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}" if isinstance(value, dict) else "[]"


def _direction(value: Any) -> str:
    text = str(value or "").upper().strip()
    if text in {"CE", "CALL", "BUY", "UP", "UPTREND", "BULLISH"}:
        return "CE"
    if text in {"PE", "PUT", "SELL", "DOWN", "DOWNTREND", "BEARISH"}:
        return "PE"
    return "NO_TRADE"


def ensure_advanced_schema() -> None:
    conn = get_db()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS ai_advanced_v2_snapshots(
          id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,created_at TEXT NOT NULL,
          broker TEXT,symbol TEXT NOT NULL,spot REAL NOT NULL,
          base_decision TEXT,base_confidence INTEGER,base_probabilities_json TEXT,
          option_decision TEXT,option_confidence INTEGER,news_bias TEXT,
          news_strength INTEGER,news_risk INTEGER,advanced_decision TEXT,
          advanced_confidence INTEGER,advanced_probabilities_json TEXT,
          reasons_json TEXT,feature_json TEXT,option_summary_json TEXT,
          global_market_json TEXT,news_snapshot_json TEXT,adaptive_model_json TEXT,
          ce_contract_json TEXT,pe_contract_json TEXT,data_coverage_score INTEGER,
          option_risk_score INTEGER,complete INTEGER DEFAULT 0,completed_at TEXT,
          trade_blocking INTEGER DEFAULT 0,order_execution INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_ai_advanced_v2_user_created
          ON ai_advanced_v2_snapshots(user_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_ai_advanced_v2_pending
          ON ai_advanced_v2_snapshots(user_id,complete,created_at);
        CREATE TABLE IF NOT EXISTS ai_advanced_v2_contract_outcomes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,decision_id TEXT NOT NULL,
          user_id INTEGER NOT NULL,horizon_minutes INTEGER NOT NULL,
          evaluated_at TEXT NOT NULL,spot_exit REAL NOT NULL,
          ce_entry_price REAL,ce_exit_price REAL,ce_net_pnl REAL,
          pe_entry_price REAL,pe_exit_price REAL,pe_net_pnl REAL,
          no_trade_net_pnl REAL DEFAULT 0,advanced_net_pnl REAL,
          base_net_pnl REAL,advanced_vs_base_benefit REAL,best_label TEXT,
          advanced_outcome TEXT,base_outcome TEXT,charge_model TEXT,
          details_json TEXT,UNIQUE(decision_id,horizon_minutes));
        CREATE INDEX IF NOT EXISTS idx_ai_advanced_v2_outcome_user_horizon
          ON ai_advanced_v2_contract_outcomes(user_id,horizon_minutes,evaluated_at DESC);
        CREATE TABLE IF NOT EXISTS ai_advanced_v2_runtime(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),version TEXT,
          instance_id TEXT,started_at TEXT,heartbeat_at TEXT,last_error TEXT,
          trade_blocking INTEGER DEFAULT 0,order_execution INTEGER DEFAULT 0);
        """)
        now = _iso()
        conn.execute("""INSERT INTO ai_advanced_v2_runtime VALUES(1,?,?,?,?,NULL,0,0)
        ON CONFLICT(singleton) DO UPDATE SET version=excluded.version,
        instance_id=excluded.instance_id,heartbeat_at=excluded.heartbeat_at,
        trade_blocking=0,order_execution=0""", (VERSION, _instance, now, now))
        conn.commit()
    finally:
        conn.close()


def _users():
    conn = get_db()
    try:
        rows = conn.execute("""SELECT DISTINCT user_id FROM(
        SELECT user_id FROM user_bot_state WHERE is_running=1 UNION ALL
        SELECT user_id FROM bot_status WHERE is_running=1) ORDER BY user_id""").fetchall()
        return [int(row["user_id"]) for row in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _previous_oi(user_id: int) -> Dict[str, float]:
    conn = get_db()
    try:
        row = conn.execute("""SELECT option_summary_json FROM ai_advanced_v2_snapshots
        WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT 1""", (user_id,)).fetchone()
        return option_oi_identity_map(_loads(row["option_summary_json"], {})) if row else {}
    except Exception:
        return {}
    finally:
        conn.close()


def _option_payload(user_id: int, market: Mapping[str, Any]) -> Dict[str, Any]:
    current = time.monotonic()
    cached = _option_cache.get(user_id)
    if cached and current - _option_fetch_mono.get(user_id, 0) < OPTION_REFRESH_SECONDS:
        if str(cached.get("underlying")) == str(market.get("symbol") or market.get("underlying") or "NIFTY"):
            return cached
    result = get_broker_intelligence(user_id, market, _previous_oi(user_id))
    independent_global = global_market_snapshot()
    native_global = dict(result.get("global_market") or {})
    if native_global.get("available"):
        merged = dict(independent_global)
        merged_values = dict(independent_global.get("values") or {})
        merged_values.update(native_global.get("values") or {})
        merged["values"] = merged_values
        merged["native_broker_overlay"] = native_global
        result["global_market"] = merged
    else:
        result["global_market"] = independent_global
    _option_cache[user_id] = result
    _option_fetch_mono[user_id] = current
    return result


def _probs(scores: Mapping[str, float]) -> Dict[str, int]:
    values = {key: max(0.0, _f(scores.get(key))) for key in ("CE", "PE", "NO_TRADE")}
    total = sum(values.values()) or 1.0
    result = {key: int(round(value / total * 100)) for key, value in values.items()}
    result["NO_TRADE"] += 100 - sum(result.values())
    return result


def fuse_advanced(market, base, option_payload, news):
    option = dict(option_payload.get("option_intelligence") or {})
    global_market = dict(option_payload.get("global_market") or {})
    base_probs = dict(base.get("probabilities") or {})
    scores = {key: _f(base_probs.get(key), 0) for key in ("CE", "PE", "NO_TRADE")}
    reasons = []
    option_direction = _direction(option.get("option_direction"))
    option_confidence = _f(option.get("option_confidence"))
    option_risk = _f(option.get("risk_score"))
    coverage = _f(option.get("data_coverage_score"))
    if option_payload.get("success") and option_direction in {"CE", "PE"}:
        strength = max(5.0, min(34.0, option_confidence * 0.32 * coverage / 100.0))
        scores[option_direction] += strength
        scores["PE" if option_direction == "CE" else "CE"] -= strength * 0.45
        reasons.append("OPTION_INTELLIGENCE_" + option_direction)
    else:
        scores["NO_TRADE"] += 12
        reasons.append("OPTION_INTELLIGENCE_UNAVAILABLE_OR_NEUTRAL")
    if option_risk >= 60:
        scores["NO_TRADE"] += 22
        reasons.append("OPTION_LIQUIDITY_OR_IV_RISK")
    news_bias = _direction(news.get("news_bias"))
    news_strength = _f(news.get("news_strength"))
    news_risk = _f(news.get("news_risk_score"))
    if news.get("fresh") and news_bias in {"CE", "PE"}:
        scores[news_bias] += min(24.0, 5.0 + news_strength * 0.22)
        reasons.append("NEWS_" + news_bias)
    if news_risk >= 65:
        scores["NO_TRADE"] += min(28.0, news_risk * 0.28)
        reasons.append("HIGH_NEWS_RISK")
    vix = dict((global_market.get("values") or {}).get("india_vix") or {})
    vix_value = _f(vix.get("last_price"))
    vix_change = _f(vix.get("change_percent"))
    if vix_value >= 25 or vix_change >= 8:
        scores["NO_TRADE"] += 18
        reasons.append("VIX_RISK_HIGH")
    feature = feature_vector(market=market, base=base, option=option, news=news, global_market=global_market)
    adaptive = predict_adaptive(feature, PRIMARY_HORIZON)
    if adaptive.get("available"):
        for key in scores:
            scores[key] = scores[key] * 0.65 + _f((adaptive.get("probabilities") or {}).get(key)) * 0.35
        reasons.append("ADAPTIVE_MODEL_ACTIVE_SHADOW")
    else:
        reasons.append("ADAPTIVE_MODEL_COLLECTING")
    probabilities = _probs(scores)
    decision, confidence = max(probabilities.items(), key=lambda item: item[1])
    if coverage < 45 or option_risk >= 80:
        decision = "NO_TRADE"
        confidence = max(confidence, probabilities["NO_TRADE"], 70)
        reasons.append("DATA_COVERAGE_RISK_GATE")
    return {"success": True,"version": VERSION,"decision": decision,"confidence": int(confidence),
            "probabilities": probabilities,"reasons": list(dict.fromkeys(reasons))[:15],
            "feature": feature,"adaptive_model": adaptive,"trade_blocking": False,"order_execution": False}


def _find_contract(option_summary, entry):
    target_token = str(entry.get("token") or entry.get("instrument_key") or "")
    target_symbol = str(entry.get("symbol") or "")
    target_side = str(entry.get("side") or "").upper()
    target_strike = _f(entry.get("strike"))
    for row in option_summary.get("rows") or []:
        option = dict(row.get(target_side.lower()) or {})
        if not option:
            continue
        if target_token and str(option.get("token") or option.get("instrument_key")) == target_token:
            return option
        if target_symbol and str(option.get("symbol")) == target_symbol:
            return option
        if abs(_f(option.get("strike")) - target_strike) < 0.01 and str(option.get("side")).upper() == target_side:
            return option
    return {}


def _contract_pnl(broker, underlying, entry, current):
    entry_price = _f(entry.get("ask") or entry.get("ltp"))
    exit_price = _f(current.get("bid") or current.get("ltp"))
    qty = max(1, _i(entry.get("lot_size"), 1))
    if entry_price <= 0 or exit_price <= 0:
        return {"available": False,"reason": "OPTION_QUOTE_MISSING"}
    costs = calculate_option_round_trip_costs(broker, underlying, entry_price, exit_price, qty)
    return {"available": True,"entry_price": entry_price,"exit_price": exit_price,"quantity": qty,
            "gross_pnl": costs["market_gross_pnl"],"slippage_cost": costs["slippage_cost"],
            "charges": costs["total_charges"],"net_pnl": costs["net_pnl"],"cost_details": costs}


def _decision_pnl(decision, ce_net, pe_net):
    return ce_net if _direction(decision) == "CE" else pe_net if _direction(decision) == "PE" else 0.0


def _outcome(net):
    return "WIN" if net > 0 else "LOSS" if net < 0 else "FLAT"


def register_snapshot(user_id, market, base, option_payload, news, advanced):
    option = dict(option_payload.get("option_intelligence") or {})
    if not (market.get("market_open") and market.get("feed_connected") and _f(market.get("price")) > 0):
        return None
    ce = selected_contract(option, "CE") or {}
    pe = selected_contract(option, "PE") or {}
    if not ce or not pe:
        return None
    now = _now()
    conn = get_db()
    try:
        last = conn.execute("SELECT created_at,advanced_decision,broker,symbol FROM ai_advanced_v2_snapshots WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT 1", (user_id,)).fetchone()
        if last:
            when = _parse(last["created_at"])
            same = str(last["advanced_decision"]) == str(advanced.get("decision")) and str(last["broker"]) == str(option_payload.get("broker")) and str(last["symbol"]) == str(market.get("symbol"))
            if when and same and (now - when).total_seconds() < RECORD_SPACING_SECONDS:
                return None
        decision_id = uuid.uuid4().hex[:20]
        conn.execute("""INSERT INTO ai_advanced_v2_snapshots(
        id,user_id,created_at,broker,symbol,spot,base_decision,base_confidence,
        base_probabilities_json,option_decision,option_confidence,news_bias,
        news_strength,news_risk,advanced_decision,advanced_confidence,
        advanced_probabilities_json,reasons_json,feature_json,option_summary_json,
        global_market_json,news_snapshot_json,adaptive_model_json,ce_contract_json,
        pe_contract_json,data_coverage_score,option_risk_score,complete,completed_at,
        trade_blocking,order_execution) VALUES(
        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,0,0)""", (
            decision_id,user_id,_iso(now),str(option_payload.get("broker") or ""),str(market.get("symbol") or "NIFTY"),round(_f(market.get("price")),2),
            _direction(base.get("decision")),_i(base.get("confidence")),_dumps(base.get("probabilities") or {}),
            str(option.get("option_direction") or "NO_TRADE"),_i(option.get("option_confidence")),str(news.get("news_bias") or "NEUTRAL"),
            _i(news.get("news_strength")),_i(news.get("news_risk_score")),_direction(advanced.get("decision")),_i(advanced.get("confidence")),
            _dumps(advanced.get("probabilities") or {}),_dumps(advanced.get("reasons") or []),_dumps(advanced.get("feature") or {}),
            _dumps(option),_dumps(option_payload.get("global_market") or {}),_dumps(news),_dumps(advanced.get("adaptive_model") or {}),
            _dumps(ce),_dumps(pe),_i(option.get("data_coverage_score")),_i(option.get("risk_score"))))
        conn.commit()
        print(f"AI ADVANCED V2 RAILWAY | logged | user={user_id} | broker={option_payload.get('broker')} | {advanced.get('decision')} {advanced.get('confidence')}% | blocking OFF")
        return decision_id
    finally:
        conn.close()


def observe_outcomes(user_id, market, option_payload):
    option = dict(option_payload.get("option_intelligence") or {})
    if not option_payload.get("success") or _f(market.get("price")) <= 0:
        return 0
    conn = get_db()
    created = 0
    try:
        pending = conn.execute("SELECT * FROM ai_advanced_v2_snapshots WHERE user_id=? AND complete=0 AND symbol=? ORDER BY datetime(created_at)", (user_id, str(market.get("symbol") or "NIFTY"))).fetchall()
        for row in pending:
            start = _parse(row["created_at"])
            if not start:
                continue
            ce_entry = _loads(row["ce_contract_json"], {})
            pe_entry = _loads(row["pe_contract_json"], {})
            ce_result = _contract_pnl(str(row["broker"] or "angelone"), str(row["symbol"]), ce_entry, _find_contract(option, ce_entry))
            pe_result = _contract_pnl(str(row["broker"] or "angelone"), str(row["symbol"]), pe_entry, _find_contract(option, pe_entry))
            if not (ce_result.get("available") and pe_result.get("available")):
                continue
            ce_net, pe_net = _f(ce_result.get("net_pnl")), _f(pe_result.get("net_pnl"))
            elapsed = (_now() - start).total_seconds()
            for horizon in HORIZONS:
                if elapsed < horizon * 60:
                    continue
                if conn.execute("SELECT 1 FROM ai_advanced_v2_contract_outcomes WHERE decision_id=? AND horizon_minutes=?", (row["id"], horizon)).fetchone():
                    continue
                best_label = max((("CE", ce_net), ("PE", pe_net), ("NO_TRADE", 0.0)), key=lambda item: item[1])[0]
                advanced_net = _decision_pnl(row["advanced_decision"], ce_net, pe_net)
                base_net = _decision_pnl(row["base_decision"], ce_net, pe_net)
                details = {"ce": ce_result,"pe": pe_result,"entry_spot": _f(row["spot"]),"exit_spot": _f(market.get("price")),
                           "premium_basis": "ENTRY_ASK_OR_LTP_EXIT_BID_OR_LTP","cost_model": "INDIA_INDEX_OPTIONS_ALL_COSTS_V2"}
                conn.execute("""INSERT OR IGNORE INTO ai_advanced_v2_contract_outcomes VALUES(
                NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    row["id"],user_id,horizon,_iso(),round(_f(market.get("price")),2),
                    ce_result["entry_price"],ce_result["exit_price"],round(ce_net,2),
                    pe_result["entry_price"],pe_result["exit_price"],round(pe_net,2),0.0,
                    round(advanced_net,2),round(base_net,2),round(advanced_net-base_net,2),best_label,
                    _outcome(advanced_net),_outcome(base_net),"INDIA_INDEX_OPTIONS_ALL_COSTS_V2",_dumps(details)))
                created += 1
            count = conn.execute("SELECT COUNT(*) n FROM ai_advanced_v2_contract_outcomes WHERE decision_id=?", (row["id"],)).fetchone()["n"]
            if _i(count) >= len(HORIZONS):
                conn.execute("UPDATE ai_advanced_v2_snapshots SET complete=1,completed_at=? WHERE id=?", (_iso(), row["id"]))
        conn.commit()
        return created
    finally:
        conn.close()


def _calibration(user_id):
    conn = get_db()
    try:
        rows = conn.execute("""SELECT s.advanced_confidence,o.advanced_outcome FROM ai_advanced_v2_snapshots s
        JOIN ai_advanced_v2_contract_outcomes o ON o.decision_id=s.id WHERE s.user_id=? AND o.horizon_minutes=?""", (user_id, PRIMARY_HORIZON)).fetchall()
    finally:
        conn.close()
    buckets = {}
    for row in rows:
        bucket = min(90, max(0, _i(row["advanced_confidence"]) // 10 * 10))
        item = buckets.setdefault(bucket, {"total": 0,"wins": 0,"losses": 0,"flat": 0})
        item["total"] += 1
        key = "wins" if row["advanced_outcome"] == "WIN" else "losses" if row["advanced_outcome"] == "LOSS" else "flat"
        item[key] += 1
    output = []
    for bucket, item in sorted(buckets.items()):
        resolved = item["wins"] + item["losses"]
        output.append({"confidence_bucket": f"{bucket}-{bucket+9}",**item,
                       "empirical_hit_rate_percent": round(item["wins"] / resolved * 100, 2) if resolved else None})
    return output


def get_advanced_summary(user_id, recent_limit=20):
    ensure_advanced_schema()
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM ai_advanced_v2_snapshots WHERE user_id=? ORDER BY datetime(created_at) DESC,rowid DESC LIMIT ?", (user_id, max(1, min(_i(recent_limit,20),50)))).fetchall()
        primary = conn.execute("SELECT * FROM ai_advanced_v2_contract_outcomes WHERE user_id=? AND horizon_minutes=?", (user_id, PRIMARY_HORIZON)).fetchall()
        recent = []
        json_fields = (("base_probabilities_json","base_probabilities",{}),("advanced_probabilities_json","advanced_probabilities",{}),("reasons_json","reasons",[]),("feature_json","features",{}),("option_summary_json","option_summary",{}),("global_market_json","global_market",{}),("news_snapshot_json","news_snapshot",{}),("adaptive_model_json","adaptive_model",{}),("ce_contract_json","ce_contract",{}),("pe_contract_json","pe_contract",{}))
        for row in rows:
            item = dict(row)
            for source, target, default in json_fields:
                item[target] = _loads(item.pop(source), default)
            outcomes = []
            for outcome in conn.execute("SELECT * FROM ai_advanced_v2_contract_outcomes WHERE decision_id=? ORDER BY horizon_minutes", (item["id"],)).fetchall():
                out = dict(outcome); out["details"] = _loads(out.pop("details_json"), {}); outcomes.append(out)
            item["outcomes"] = outcomes; item["complete"] = bool(item["complete"]); recent.append(item)
        wins = sum(row["advanced_outcome"] == "WIN" for row in primary); losses = sum(row["advanced_outcome"] == "LOSS" for row in primary); flat = sum(row["advanced_outcome"] == "FLAT" for row in primary); resolved = wins + losses
        active_broker = str(rows[0]["broker"] or "") if rows else None
        return {"success": True,"version": VERSION,"broker_intelligence_version": BROKER_VERSION,"location": "RAILWAY",
                "mode": "ADVANCED_FUSION_SHADOW_ONLY","trade_blocking": False,"order_execution": False,
                "active_broker": active_broker,"broker_capabilities": BROKER_CAPABILITIES.get(str(active_broker or "").lower(), {}),
                "summary": {"evaluated_15m": len(primary),"advanced_15m_wins": wins,"advanced_15m_losses": losses,"advanced_15m_flat": flat,
                            "advanced_15m_hit_rate_percent": round(wins / resolved * 100,2) if resolved else None,
                            "advanced_better_than_base_count": sum(_f(row["advanced_vs_base_benefit"]) > 0 for row in primary),
                            "advanced_worse_than_base_count": sum(_f(row["advanced_vs_base_benefit"]) < 0 for row in primary),
                            "advanced_vs_base_net_benefit_rupees_per_lot_15m": round(sum(_f(row["advanced_vs_base_benefit"]) for row in primary),2)},
                "calibration": _calibration(user_id),"adaptive_models": model_status(),"storage": get_db_storage_info(),"recent_decisions": recent}
    finally:
        conn.close()


def advanced_health():
    ensure_advanced_schema()
    conn = get_db()
    try:
        runtime = conn.execute("SELECT * FROM ai_advanced_v2_runtime WHERE singleton=1").fetchone()
        counts = conn.execute("SELECT COUNT(*) decisions,SUM(CASE WHEN complete=0 THEN 1 ELSE 0 END) pending FROM ai_advanced_v2_snapshots").fetchone()
    finally:
        conn.close()
    return {"success": True,"version": VERSION,"started": bool(_started),"thread_alive": bool(_thread and _thread.is_alive()),
            "instance_id": _instance,"last_cycle_at": _last_cycle,"last_error": _last_error,
            "runtime": dict(runtime) if runtime else None,"decision_count": _i(counts["decisions"] if counts else 0),
            "pending_count": _i(counts["pending"] if counts else 0),"adaptive_models": model_status(),
            "storage": get_db_storage_info(),"location": "RAILWAY","mode": "SHADOW_ONLY","trade_blocking": False,"order_execution": False}


def _heartbeat(error=None):
    global _last_cycle, _last_error
    _last_cycle = _iso(); _last_error = str(error)[:300] if error else None
    conn = get_db()
    try:
        conn.execute("UPDATE ai_advanced_v2_runtime SET heartbeat_at=?,last_error=?,trade_blocking=0,order_execution=0 WHERE singleton=1", (_last_cycle,_last_error)); conn.commit()
    finally:
        conn.close()


def _cycle():
    if not callable(_builder):
        return
    for user_id in _users():
        try:
            market = dict(_builder(user_id) or {})
            option_payload = _option_payload(user_id, market)
            observe_outcomes(user_id, market, option_payload)
            base = predict(market); news = aggregate_news()
            advanced = fuse_advanced(market, base, option_payload, news)
            register_snapshot(user_id, market, base, option_payload, news, advanced)
        except Exception as exc:
            print(f"AI ADVANCED V2 RAILWAY | user={user_id} | {type(exc).__name__}:{str(exc)[:220]}")
    maybe_train_models(force=False)


def _loop():
    while True:
        error = None
        try:
            _cycle()
        except Exception as exc:
            error = f"{type(exc).__name__}:{str(exc)[:260]}"
            print("AI ADVANCED V2 RAILWAY | monitor warning | " + error)
        try:
            _heartbeat(error)
        except Exception as exc:
            print(f"AI ADVANCED V2 RAILWAY | heartbeat warning | {str(exc)[:180]}")
        time.sleep(POLL_SECONDS)


def start_advanced_intelligence(snapshot_builder):
    global _started, _thread, _builder
    with _lock:
        _builder = snapshot_builder
        if _started and _thread and _thread.is_alive():
            return advanced_health()
        ensure_advanced_schema(); _started = True
        _thread = threading.Thread(target=_loop,name="okai-advanced-broker-neutral-shadow-v2",daemon=True); _thread.start()
        print(f"AI ADVANCED V2 RAILWAY {VERSION} active | Angel One + Upstox + Zerodha | exact option labels | blocking OFF | orders OFF")
        return advanced_health()

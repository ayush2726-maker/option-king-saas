"""Persistent market-specialist memory and reasoning layer for Option King AI.

This module does not pretend to be a general-purpose language model. It gives the
existing Railway shadow AI a broker-neutral market knowledge layer built from:

* live price/indicator state,
* option-chain, news and global-market context,
* completed, cost-adjusted option outcomes already stored by the monitor,
* explicit risk and data-quality rules, and
* nearest historical regimes that were genuinely observed before the decision.

The runtime patch is deliberately shadow-only. It can improve the advanced AI
prediction and explanation, but it cannot block trades or place orders.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from database import get_db
from bot.adaptive_model_v2 import FEATURE_NAMES, feature_vector

VERSION = "OKAI-MARKET-KNOWLEDGE-BRAIN-V1"
PRIMARY_HORIZON = 15
MAX_HISTORY_ROWS = 3000
NEIGHBOUR_LIMIT = 60
MIN_MEMORY_SUPPORT = 12
MIN_BLEND_SUPPORT = 20
MAX_BLEND_WEIGHT = 0.22
_PATCH_LOCK = threading.RLock()
_PATCHED = False
_WATCHER_STARTED = False
_LAST_ERROR: Optional[str] = None
_LAST_PATCHED_AT: Optional[str] = None

# Human-readable knowledge map. These are domains the engine can reason about;
# they are not claims that every field is available from every broker.
KNOWLEDGE_DOMAINS = {
    "PRICE_ACTION": ["market structure", "gap", "ORB", "trend", "momentum"],
    "INDICATORS": ["VWAP", "EMA", "Supertrend", "ADX", "RSI", "ATR", "volume"],
    "OPTIONS": ["PCR", "OI", "IV", "spread", "depth", "delta", "gamma", "theta"],
    "REGIME": ["trend", "range", "high volatility", "low liquidity", "expiry risk"],
    "CONTEXT": ["India VIX", "global market", "news bias", "event risk"],
    "EXECUTION": ["charges", "slippage", "liquidity", "broker data coverage"],
    "RISK": ["NO_TRADE", "EOD", "cooldown", "loss control", "data quality"],
    "MEMORY": ["similar historical regimes", "actual option outcomes", "cost-adjusted P&L"],
}


def _iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ensure_market_knowledge_schema() -> None:
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_market_knowledge_v1(
              decision_id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              symbol TEXT,
              horizon_minutes INTEGER NOT NULL DEFAULT 15,
              knowledge_version TEXT NOT NULL,
              regime_json TEXT NOT NULL DEFAULT '{}',
              memory_json TEXT NOT NULL DEFAULT '{}',
              risk_flags_json TEXT NOT NULL DEFAULT '[]',
              explanation_json TEXT NOT NULL DEFAULT '[]',
              trade_blocking INTEGER NOT NULL DEFAULT 0,
              order_execution INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ai_market_knowledge_v1_user_created
              ON ai_market_knowledge_v1(user_id,created_at DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _option_section(option_payload: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(option_payload.get("option_intelligence") or {})


def _global_section(option_payload: Mapping[str, Any]) -> Dict[str, Any]:
    return dict(option_payload.get("global_market") or {})


def classify_regime(
    market: Mapping[str, Any],
    option_payload: Mapping[str, Any],
    news: Mapping[str, Any],
) -> Dict[str, Any]:
    """Convert live inputs into a stable, explainable market regime."""
    option = _option_section(option_payload)
    global_market = _global_section(option_payload)
    vix = dict((global_market.get("values") or {}).get("india_vix") or {})

    adx = _f(market.get("adx"))
    rsi = _f(market.get("rsi"), 50.0)
    atr_percent = _f(market.get("atr_percent"))
    volume_ratio = _f(market.get("volume_ratio"))
    spread = _f(option.get("average_spread_percent"), _f(market.get("spread_percent")))
    coverage = _f(option.get("data_coverage_score"))
    option_risk = _f(option.get("risk_score"))
    vix_value = _f(vix.get("last_price"))
    vix_change = _f(vix.get("change_percent"))
    news_risk = _f(news.get("news_risk_score"))

    if adx >= 28:
        trend = "STRONG_TREND"
    elif adx >= 20:
        trend = "DEVELOPING_TREND"
    else:
        trend = "RANGE_OR_WEAK_TREND"

    if atr_percent >= 1.2 or vix_value >= 24 or vix_change >= 8:
        volatility = "HIGH"
    elif atr_percent <= 0.35 and 0 < vix_value < 14:
        volatility = "LOW"
    else:
        volatility = "NORMAL"

    if rsi >= 68:
        momentum = "OVERBOUGHT_BULLISH"
    elif rsi <= 32:
        momentum = "OVERSOLD_BEARISH"
    elif rsi >= 55:
        momentum = "BULLISH"
    elif rsi <= 45:
        momentum = "BEARISH"
    else:
        momentum = "NEUTRAL"

    if coverage < 45 or spread >= 4 or option_risk >= 80:
        liquidity = "POOR_OR_UNRELIABLE"
    elif spread >= 2 or option_risk >= 60:
        liquidity = "CAUTION"
    else:
        liquidity = "NORMAL"

    return {
        "trend": trend,
        "volatility": volatility,
        "momentum": momentum,
        "liquidity": liquidity,
        "event_risk": "HIGH" if news_risk >= 65 else "NORMAL",
        "market_open": bool(market.get("market_open")),
        "feed_connected": bool(market.get("feed_connected")),
        "measurements": {
            "adx": round(adx, 2),
            "rsi": round(rsi, 2),
            "atr_percent": round(atr_percent, 3),
            "volume_ratio": round(volume_ratio, 2),
            "average_spread_percent": round(spread, 3),
            "data_coverage_score": round(coverage, 2),
            "option_risk_score": round(option_risk, 2),
            "india_vix": round(vix_value, 2),
            "india_vix_change_percent": round(vix_change, 2),
            "news_risk_score": round(news_risk, 2),
        },
    }


def _risk_flags(regime: Mapping[str, Any], market: Mapping[str, Any]) -> List[str]:
    flags: List[str] = []
    if not regime.get("feed_connected"):
        flags.append("LIVE_FEED_NOT_CONFIRMED")
    if not regime.get("market_open"):
        flags.append("MARKET_CLOSED")
    if regime.get("liquidity") == "POOR_OR_UNRELIABLE":
        flags.append("OPTION_DATA_OR_LIQUIDITY_RISK")
    if regime.get("volatility") == "HIGH":
        flags.append("HIGH_VOLATILITY_REGIME")
    if regime.get("event_risk") == "HIGH":
        flags.append("HIGH_NEWS_EVENT_RISK")
    if _f(market.get("price")) <= 0:
        flags.append("INVALID_SPOT_PRICE")
    return flags


def _historical_rows(symbol: str, horizon: int) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT s.id,s.created_at,s.symbol,s.feature_json,
            o.best_label,o.ce_net_pnl,o.pe_net_pnl,o.no_trade_net_pnl
            FROM ai_advanced_v2_snapshots s
            JOIN ai_advanced_v2_contract_outcomes o ON o.decision_id=s.id
            WHERE o.horizon_minutes=? AND o.best_label IN('CE','PE','NO_TRADE')
              AND s.feature_json IS NOT NULL
            ORDER BY datetime(s.created_at) DESC,s.rowid DESC
            LIMIT ?""",
            (int(horizon), int(MAX_HISTORY_ROWS)),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    output = [dict(row) for row in rows]
    same_symbol = [row for row in output if str(row.get("symbol") or "").upper() == symbol.upper()]
    return same_symbol if len(same_symbol) >= MIN_MEMORY_SUPPORT else output


def _vector_from_feature(feature: Mapping[str, Any]) -> List[float]:
    return [_f(feature.get(name)) for name in FEATURE_NAMES]


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return float("inf")
    # feature_vector already bounds nearly all features to a compact range.
    return sum(abs(float(a) - float(b)) for a, b in zip(left, right)) / len(left)


def _probabilities_from_scores(scores: Mapping[str, float]) -> Dict[str, int]:
    values = {label: max(0.0, _f(scores.get(label))) for label in ("CE", "PE", "NO_TRADE")}
    total = sum(values.values()) or 1.0
    result = {label: int(round(value / total * 100.0)) for label, value in values.items()}
    result["NO_TRADE"] += 100 - sum(result.values())
    return result


def similar_regime_memory(
    feature: Mapping[str, Any],
    symbol: str,
    horizon: int = PRIMARY_HORIZON,
) -> Dict[str, Any]:
    """Use only completed historical outcomes; unresolved rows never teach it."""
    current = _vector_from_feature(feature)
    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for row in _historical_rows(symbol, horizon):
        past_feature = _loads(row.get("feature_json"), {})
        distance = _distance(current, _vector_from_feature(past_feature))
        if math.isfinite(distance):
            candidates.append((distance, row))
    candidates.sort(key=lambda item: item[0])
    neighbours = candidates[:NEIGHBOUR_LIMIT]

    label_scores = {"CE": 0.0, "PE": 0.0, "NO_TRADE": 0.0}
    pnl_by_label = {"CE": 0.0, "PE": 0.0, "NO_TRADE": 0.0}
    total_weight = 0.0
    similarity_total = 0.0
    for distance, row in neighbours:
        similarity = max(0.0, 1.0 - min(1.0, distance))
        weight = max(0.02, similarity ** 3)
        label = str(row.get("best_label") or "NO_TRADE").upper()
        if label not in label_scores:
            continue
        label_scores[label] += weight
        pnl_by_label["CE"] += weight * _f(row.get("ce_net_pnl"))
        pnl_by_label["PE"] += weight * _f(row.get("pe_net_pnl"))
        pnl_by_label["NO_TRADE"] += weight * _f(row.get("no_trade_net_pnl"))
        total_weight += weight
        similarity_total += similarity

    support = len(neighbours)
    probabilities = _probabilities_from_scores(label_scores) if support else {"CE": 0, "PE": 0, "NO_TRADE": 100}
    decision, confidence = max(probabilities.items(), key=lambda item: item[1])
    average_pnl = {
        label: round(value / total_weight, 2) if total_weight > 0 else 0.0
        for label, value in pnl_by_label.items()
    }
    average_similarity = similarity_total / support if support else 0.0
    reliable = support >= MIN_MEMORY_SUPPORT and average_similarity >= 0.55

    return {
        "available": bool(support),
        "reliable": reliable,
        "horizon_minutes": int(horizon),
        "support": support,
        "minimum_support": MIN_MEMORY_SUPPORT,
        "average_similarity_percent": round(average_similarity * 100.0, 2),
        "decision": decision,
        "confidence": confidence,
        "probabilities": probabilities,
        "average_cost_adjusted_pnl_per_lot": average_pnl,
        "source": "COMPLETED_HISTORICAL_OPTION_OUTCOMES_ONLY",
        "leakage_safe": True,
    }


def _explanations(
    regime: Mapping[str, Any],
    memory: Mapping[str, Any],
    option_payload: Mapping[str, Any],
    news: Mapping[str, Any],
) -> List[str]:
    option = _option_section(option_payload)
    lines = [
        f"Regime: {regime.get('trend')} / volatility {regime.get('volatility')} / momentum {regime.get('momentum')}.",
        f"Option data: coverage {_i(option.get('data_coverage_score'))}% and risk {_i(option.get('risk_score'))}%.",
    ]
    if memory.get("reliable"):
        lines.append(
            f"Similar-regime memory: {memory.get('support')} completed cases favour "
            f"{memory.get('decision')} at {memory.get('confidence')}%."
        )
    elif memory.get("available"):
        lines.append(
            f"Historical memory exists but proof is weak: support {memory.get('support')}, "
            f"similarity {memory.get('average_similarity_percent')}%."
        )
    else:
        lines.append("Historical market memory is still collecting completed outcomes.")
    if _f(news.get("news_risk_score")) >= 65:
        lines.append("News/event risk is high, so confidence must be reduced rather than guessed.")
    return lines


def build_market_knowledge(
    market: Mapping[str, Any],
    base: Mapping[str, Any],
    option_payload: Mapping[str, Any],
    news: Mapping[str, Any],
) -> Dict[str, Any]:
    option = _option_section(option_payload)
    global_market = _global_section(option_payload)
    feature = feature_vector(
        market=market,
        base=base,
        option=option,
        news=news,
        global_market=global_market,
    )
    symbol = str(market.get("symbol") or market.get("underlying") or "NIFTY").upper()
    regime = classify_regime(market, option_payload, news)
    memory = similar_regime_memory(feature, symbol, PRIMARY_HORIZON)
    flags = _risk_flags(regime, market)
    return {
        "success": True,
        "version": VERSION,
        "symbol": symbol,
        "knowledge_domains": KNOWLEDGE_DOMAINS,
        "regime": regime,
        "similar_regime_memory": memory,
        "risk_flags": flags,
        "explanation": _explanations(regime, memory, option_payload, news),
        "facts_used": {
            "live_market": True,
            "option_chain": bool(option),
            "news": bool(news),
            "global_market": bool(global_market),
            "completed_outcomes": int(memory.get("support") or 0),
        },
        "hallucination_guard": "MISSING_DATA_IS_MARKED_UNAVAILABLE_NOT_INVENTED",
        "trade_blocking": False,
        "order_execution": False,
    }


def blend_shadow_prediction(
    advanced: Mapping[str, Any],
    knowledge: Mapping[str, Any],
) -> Dict[str, Any]:
    """Blend reliable memory into the shadow prediction only."""
    result = dict(advanced or {})
    result["market_knowledge"] = dict(knowledge or {})
    memory = dict(knowledge.get("similar_regime_memory") or {})
    base_probs = dict(result.get("probabilities") or {})
    memory_probs = dict(memory.get("probabilities") or {})
    support = _i(memory.get("support"))
    similarity = _f(memory.get("average_similarity_percent")) / 100.0
    reliable = bool(memory.get("reliable")) and support >= MIN_BLEND_SUPPORT

    if reliable and base_probs:
        support_factor = min(1.0, support / float(NEIGHBOUR_LIMIT))
        weight = min(MAX_BLEND_WEIGHT, 0.08 + 0.14 * support_factor * similarity)
        scores = {
            label: (1.0 - weight) * _f(base_probs.get(label)) + weight * _f(memory_probs.get(label))
            for label in ("CE", "PE", "NO_TRADE")
        }
        probabilities = _probabilities_from_scores(scores)
        decision, confidence = max(probabilities.items(), key=lambda item: item[1])
        result["probabilities"] = probabilities
        result["decision"] = decision
        result["confidence"] = int(confidence)
        result["market_knowledge_blend_weight_percent"] = round(weight * 100.0, 2)
        reasons = list(result.get("reasons") or [])
        reasons.append("MARKET_KNOWLEDGE_SIMILAR_REGIME_MEMORY")
        result["reasons"] = list(dict.fromkeys(reasons))[:20]
    else:
        result["market_knowledge_blend_weight_percent"] = 0.0

    result["trade_blocking"] = False
    result["order_execution"] = False
    return result


def _persist_knowledge(decision_id: str, user_id: int, advanced: Mapping[str, Any]) -> None:
    knowledge = dict(advanced.get("market_knowledge") or {})
    if not decision_id or not knowledge:
        return
    ensure_market_knowledge_schema()
    conn = get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO ai_market_knowledge_v1(
            decision_id,user_id,created_at,symbol,horizon_minutes,knowledge_version,
            regime_json,memory_json,risk_flags_json,explanation_json,
            trade_blocking,order_execution) VALUES(?,?,?,?,?,?,?,?,?,?,0,0)""",
            (
                str(decision_id),
                int(user_id),
                _iso(),
                str(knowledge.get("symbol") or ""),
                PRIMARY_HORIZON,
                VERSION,
                _dumps(knowledge.get("regime") or {}),
                _dumps(knowledge.get("similar_regime_memory") or {}),
                _dumps(knowledge.get("risk_flags") or []),
                _dumps(knowledge.get("explanation") or []),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _knowledge_for_decisions(decision_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ids = [str(value) for value in decision_ids if value]
    if not ids:
        return {}
    ensure_market_knowledge_schema()
    placeholders = ",".join("?" for _ in ids)
    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM ai_market_knowledge_v1 WHERE decision_id IN({placeholders})",
            tuple(ids),
        ).fetchall()
    finally:
        conn.close()
    output: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        output[str(item["decision_id"])] = {
            "version": item.get("knowledge_version"),
            "symbol": item.get("symbol"),
            "horizon_minutes": item.get("horizon_minutes"),
            "regime": _loads(item.get("regime_json"), {}),
            "similar_regime_memory": _loads(item.get("memory_json"), {}),
            "risk_flags": _loads(item.get("risk_flags_json"), []),
            "explanation": _loads(item.get("explanation_json"), []),
            "trade_blocking": False,
            "order_execution": False,
        }
    return output


def brain_health() -> Dict[str, Any]:
    ensure_market_knowledge_schema()
    conn = get_db()
    try:
        row = conn.execute("SELECT COUNT(*) AS total,MAX(created_at) AS latest FROM ai_market_knowledge_v1").fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    return {
        "success": True,
        "version": VERSION,
        "patched": bool(_PATCHED),
        "patched_at": _LAST_PATCHED_AT,
        "last_error": _LAST_ERROR,
        "stored_reasoning_count": _i(row["total"] if row else 0),
        "latest_reasoning_at": row["latest"] if row else None,
        "knowledge_domains": KNOWLEDGE_DOMAINS,
        "memory_source": "COMPLETED_COST_ADJUSTED_OPTION_OUTCOMES",
        "trade_blocking": False,
        "order_execution": False,
    }


def apply_market_knowledge_brain_patch() -> bool:
    """Patch the loaded advanced shadow runtime; idempotent and fail-closed."""
    global _PATCHED, _LAST_ERROR, _LAST_PATCHED_AT
    with _PATCH_LOCK:
        if _PATCHED:
            return True
        runtime = sys.modules.get("bot.advanced_intelligence_v2")
        if runtime is None or not all(
            hasattr(runtime, name)
            for name in ("fuse_advanced", "register_snapshot", "get_advanced_summary", "advanced_health")
        ):
            return False
        try:
            original_fuse = runtime.fuse_advanced
            original_register = runtime.register_snapshot
            original_summary = runtime.get_advanced_summary
            original_health = runtime.advanced_health

            if getattr(original_fuse, "_market_knowledge_v1", False):
                _PATCHED = True
                return True

            def fuse_advanced(market, base, option_payload, news):
                advanced = original_fuse(market, base, option_payload, news)
                knowledge = build_market_knowledge(market, base, option_payload, news)
                return blend_shadow_prediction(advanced, knowledge)

            def register_snapshot(user_id, market, base, option_payload, news, advanced):
                decision_id = original_register(user_id, market, base, option_payload, news, advanced)
                try:
                    _persist_knowledge(str(decision_id or ""), int(user_id), advanced)
                except Exception as exc:
                    global _LAST_ERROR
                    _LAST_ERROR = f"PERSIST:{type(exc).__name__}:{str(exc)[:180]}"
                return decision_id

            def get_advanced_summary(user_id, recent_limit=20):
                result = dict(original_summary(user_id, recent_limit=recent_limit) or {})
                recent = list(result.get("recent_decisions") or [])
                stored = _knowledge_for_decisions(item.get("id") for item in recent)
                for item in recent:
                    item["market_knowledge"] = stored.get(str(item.get("id")))
                result["recent_decisions"] = recent
                result["market_knowledge_brain"] = brain_health()
                return result

            def advanced_health():
                result = dict(original_health() or {})
                result["market_knowledge_brain"] = brain_health()
                return result

            fuse_advanced._market_knowledge_v1 = True
            register_snapshot._market_knowledge_v1 = True
            get_advanced_summary._market_knowledge_v1 = True
            advanced_health._market_knowledge_v1 = True
            runtime.fuse_advanced = fuse_advanced
            runtime.register_snapshot = register_snapshot
            runtime.get_advanced_summary = get_advanced_summary
            runtime.advanced_health = advanced_health

            # ai_routes imports these functions by value. Rebind its local names too
            # when the route module has finished importing.
            routes = sys.modules.get("bot.ai_routes")
            if routes is not None:
                routes.get_advanced_summary = get_advanced_summary
                routes.advanced_health = advanced_health

            ensure_market_knowledge_schema()
            _PATCHED = True
            _LAST_PATCHED_AT = _iso()
            _LAST_ERROR = None
            print(f"MARKET KNOWLEDGE BRAIN {VERSION} active | shadow reasoning ON | orders OFF")
            return True
        except Exception as exc:
            _LAST_ERROR = f"PATCH:{type(exc).__name__}:{str(exc)[:220]}"
            return False


def _watch_for_runtime() -> None:
    # Keep watching because ai_routes can rebind imported functions after the
    # advanced module itself has loaded.
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if apply_market_knowledge_brain_patch():
            routes = sys.modules.get("bot.ai_routes")
            runtime = sys.modules.get("bot.advanced_intelligence_v2")
            if routes is not None and runtime is not None:
                routes.get_advanced_summary = runtime.get_advanced_summary
                routes.advanced_health = runtime.advanced_health
                return
        time.sleep(0.05)


def schedule_market_knowledge_brain_patch() -> None:
    global _WATCHER_STARTED
    with _PATCH_LOCK:
        if _WATCHER_STARTED:
            return
        _WATCHER_STARTED = True
        threading.Thread(
            target=_watch_for_runtime,
            name="okai-market-knowledge-brain-v1-loader",
            daemon=True,
        ).start()

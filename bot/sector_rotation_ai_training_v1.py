"""Sector Rotation training memory for Option King AI.

This module captures live sector breadth beside each Advanced AI V2 snapshot and
joins it to the existing exact 5/15/30-minute option outcomes. It creates a
leakage-safe dataset that can later be validated and promoted into the adaptive
model. The layer is permanently shadow-only: it cannot block trades, change
strategy scores, or place broker orders.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from database import get_db

VERSION = "OKAI-SECTOR-ROTATION-AI-TRAINING-V1"
MIN_VALIDATED_SAMPLES = 300
CACHE_REUSE_SECONDS = 60.0
_PATCH_LOCK = threading.RLock()
_PATCHED = False
_WATCHER_STARTED = False
_LAST_ERROR: Optional[str] = None
_LAST_PATCHED_AT: Optional[str] = None

ROTATION_DIRECTION = {
    "BROAD_POSITIVE": 1.0,
    "POSITIVE_BIAS": 0.55,
    "MIXED": 0.0,
    "NEGATIVE_BIAS": -0.55,
    "BROAD_NEGATIVE": -1.0,
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


def _clip(value: Any, low: float, high: float) -> float:
    return max(float(low), min(float(high), _f(value)))


def normalize_index(value: Any) -> str:
    text = str(value or "NIFTY").upper().replace(" ", "").replace("_", "")
    aliases = {
        "NIFTY50": "NIFTY",
        "NIFTY": "NIFTY",
        "NIFTYBANK": "BANKNIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "BANKNIFTYINDEX": "BANKNIFTY",
        "BSESENSEX": "SENSEX",
        "SENSEX": "SENSEX",
    }
    return aliases.get(text, "NIFTY")


def sector_bias(payload: Mapping[str, Any]) -> str:
    rotation = str((payload.get("summary") or {}).get("rotation") or "MIXED").upper()
    if rotation in {"BROAD_POSITIVE", "POSITIVE_BIAS"}:
        return "CE"
    if rotation in {"BROAD_NEGATIVE", "NEGATIVE_BIAS"}:
        return "PE"
    return "NO_TRADE"


def extract_sector_features(payload: Mapping[str, Any]) -> Dict[str, float]:
    """Convert the display payload into bounded model-ready features."""
    summary = dict(payload.get("summary") or {})
    sectors = [row for row in (payload.get("sectors") or []) if isinstance(row, Mapping)]
    sectors = sorted(
        sectors,
        key=lambda row: _f(row.get("average_change_percent")),
        reverse=True,
    )

    rotation = str(summary.get("rotation") or "MIXED").upper()
    coverage = max(0, _i(summary.get("coverage")))
    constituents = max(0, _i(summary.get("constituents")))
    advancers = max(0, _i(summary.get("advancers")))
    decliners = max(0, _i(summary.get("decliners")))
    strongest = _f(sectors[0].get("average_change_percent")) if sectors else 0.0
    weakest = _f(sectors[-1].get("average_change_percent")) if sectors else 0.0
    breadth = _clip(summary.get("breadth_percent"), 0.0, 100.0)
    average = _clip(summary.get("average_change_percent"), -6.0, 6.0)
    denominator = max(1, coverage)

    return {
        "sector_rotation_direction": ROTATION_DIRECTION.get(rotation, 0.0),
        "sector_average_change": round(average / 6.0, 6),
        "sector_breadth": round(breadth / 100.0, 6),
        "sector_breadth_centered": round((breadth - 50.0) / 50.0, 6),
        "sector_adv_decl_balance": round((advancers - decliners) / denominator, 6),
        "sector_coverage": round(coverage / max(1, constituents), 6),
        "sector_strongest_change": round(_clip(strongest, -6.0, 6.0) / 6.0, 6),
        "sector_weakest_change": round(_clip(weakest, -6.0, 6.0) / 6.0, 6),
        "sector_dispersion": round(_clip(strongest - weakest, 0.0, 12.0) / 12.0, 6),
        "sector_count": round(min(20, len(sectors)) / 20.0, 6),
        "sector_mixed": 1.0 if rotation == "MIXED" else 0.0,
    }


def training_annotation(payload: Mapping[str, Any]) -> Dict[str, Any]:
    summary = dict(payload.get("summary") or {})
    return {
        "version": VERSION,
        "index": str(payload.get("index") or "NIFTY"),
        "rotation": str(summary.get("rotation") or "MIXED"),
        "sector_bias": sector_bias(payload),
        "features": extract_sector_features(payload),
        "source": payload.get("source"),
        "market_open": bool(payload.get("market_open")),
        "training_only": True,
        "trade_blocking": False,
        "order_execution": False,
    }


def ensure_schema() -> None:
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_sector_rotation_training_v1(
              decision_id TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              symbol TEXT NOT NULL,
              broker TEXT,
              rotation_label TEXT NOT NULL,
              sector_bias TEXT NOT NULL,
              feature_json TEXT NOT NULL DEFAULT '{}',
              payload_json TEXT NOT NULL DEFAULT '{}',
              outcomes_json TEXT NOT NULL DEFAULT '{}',
              training_version TEXT NOT NULL,
              trade_blocking INTEGER NOT NULL DEFAULT 0,
              order_execution INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ai_sector_rotation_user_created
              ON ai_sector_rotation_training_v1(user_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_sector_rotation_bias
              ON ai_sector_rotation_training_v1(user_id,sector_bias,created_at DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _cached_or_live_payload(user_id: int, market: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    index_name = normalize_index(market.get("symbol") or market.get("underlying"))
    try:
        from bot import sector_rotation_routes as routes
        from bot.market_routes import _get_active_broker

        cache_key = (int(user_id), index_name)
        now = time.monotonic()
        with routes._rotation_lock:
            cached = routes._rotation_cache.get(cache_key)
            if cached and now - _f(cached.get("stored_at")) < CACHE_REUSE_SECONDS:
                payload = dict(cached.get("payload") or {})
                if payload.get("success"):
                    return payload

        broker_name, creds = _get_active_broker(int(user_id))
        if not creds:
            return None
        universe, universe_source = routes._get_universe(index_name)
        quotes = routes._fetch_quotes(int(user_id), broker_name, creds, universe)
        payload = routes._build_payload(
            index_name,
            broker_name,
            universe_source,
            universe,
            quotes,
        )
        if not payload.get("success"):
            return None
        payload["cache_hit"] = False
        with routes._rotation_lock:
            routes._rotation_cache[cache_key] = {
                "stored_at": now,
                "payload": payload,
            }
        return payload
    except Exception as exc:
        global _LAST_ERROR
        _LAST_ERROR = f"SECTOR_FETCH:{type(exc).__name__}:{str(exc)[:180]}"
        return None


def _persist_training(
    decision_id: str,
    user_id: int,
    market: Mapping[str, Any],
    option_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    if not decision_id:
        return
    ensure_schema()
    annotation = training_annotation(payload)
    conn = get_db()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO ai_sector_rotation_training_v1(
            decision_id,user_id,created_at,symbol,broker,rotation_label,
            sector_bias,feature_json,payload_json,outcomes_json,training_version,
            trade_blocking,order_execution) VALUES(?,?,?,?,?,?,?,?,?,'{}',?,0,0)""",
            (
                str(decision_id),
                int(user_id),
                _iso(),
                normalize_index(market.get("symbol") or market.get("underlying")),
                str(option_payload.get("broker") or payload.get("source") or ""),
                str(annotation["rotation"]),
                str(annotation["sector_bias"]),
                _dumps(annotation["features"]),
                _dumps(payload),
                VERSION,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _sync_outcomes(user_id: int) -> int:
    ensure_schema()
    conn = get_db()
    updated = 0
    try:
        rows = conn.execute(
            """SELECT t.decision_id,t.outcomes_json,o.horizon_minutes,
            o.best_label,o.ce_net_pnl,o.pe_net_pnl,o.no_trade_net_pnl,
            o.advanced_net_pnl,o.base_net_pnl,o.advanced_vs_base_benefit,
            o.advanced_outcome,o.base_outcome,o.evaluated_at
            FROM ai_sector_rotation_training_v1 t
            JOIN ai_advanced_v2_contract_outcomes o ON o.decision_id=t.decision_id
            WHERE t.user_id=? ORDER BY datetime(o.evaluated_at)""",
            (int(user_id),),
        ).fetchall()
        by_decision: Dict[str, Dict[str, Any]] = {}
        existing: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            decision_id = str(row["decision_id"])
            if decision_id not in existing:
                existing[decision_id] = _loads(row["outcomes_json"], {})
                by_decision[decision_id] = dict(existing[decision_id])
            horizon = str(_i(row["horizon_minutes"]))
            by_decision[decision_id][horizon] = {
                "best_label": str(row["best_label"] or "NO_TRADE"),
                "ce_net_pnl": round(_f(row["ce_net_pnl"]), 2),
                "pe_net_pnl": round(_f(row["pe_net_pnl"]), 2),
                "no_trade_net_pnl": round(_f(row["no_trade_net_pnl"]), 2),
                "advanced_net_pnl": round(_f(row["advanced_net_pnl"]), 2),
                "base_net_pnl": round(_f(row["base_net_pnl"]), 2),
                "advanced_vs_base_benefit": round(_f(row["advanced_vs_base_benefit"]), 2),
                "advanced_outcome": row["advanced_outcome"],
                "base_outcome": row["base_outcome"],
                "evaluated_at": row["evaluated_at"],
            }
        for decision_id, outcomes in by_decision.items():
            if outcomes == existing.get(decision_id, {}):
                continue
            conn.execute(
                "UPDATE ai_sector_rotation_training_v1 SET outcomes_json=? WHERE decision_id=?",
                (_dumps(outcomes), decision_id),
            )
            updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()


def _sector_pnl(row: Mapping[str, Any]) -> float:
    bias = str(row.get("sector_bias") or "NO_TRADE")
    if bias == "CE":
        return _f(row.get("ce_net_pnl"))
    if bias == "PE":
        return _f(row.get("pe_net_pnl"))
    return _f(row.get("no_trade_net_pnl"))


def training_summary(user_id: int, recent_limit: int = 20) -> Dict[str, Any]:
    ensure_schema()
    _sync_outcomes(int(user_id))
    conn = get_db()
    try:
        recent_rows = conn.execute(
            """SELECT * FROM ai_sector_rotation_training_v1
            WHERE user_id=? ORDER BY datetime(created_at) DESC LIMIT ?""",
            (int(user_id), max(1, min(_i(recent_limit, 20), 50))),
        ).fetchall()
        evaluated = conn.execute(
            """SELECT t.sector_bias,t.rotation_label,o.best_label,
            o.ce_net_pnl,o.pe_net_pnl,o.no_trade_net_pnl
            FROM ai_sector_rotation_training_v1 t
            JOIN ai_advanced_v2_contract_outcomes o ON o.decision_id=t.decision_id
            WHERE t.user_id=? AND o.horizon_minutes=15""",
            (int(user_id),),
        ).fetchall()
    finally:
        conn.close()

    recent: Dict[str, Dict[str, Any]] = {}
    for row in recent_rows:
        item = dict(row)
        decision_id = str(item["decision_id"])
        recent[decision_id] = {
            "version": item.get("training_version"),
            "rotation": item.get("rotation_label"),
            "sector_bias": item.get("sector_bias"),
            "features": _loads(item.get("feature_json"), {}),
            "outcomes": _loads(item.get("outcomes_json"), {}),
            "training_only": True,
            "trade_blocking": False,
            "order_execution": False,
        }

    sample_count = len(evaluated)
    aligned = sum(str(row["sector_bias"]) == str(row["best_label"]) for row in evaluated)
    total_pnl = sum(_sector_pnl(dict(row)) for row in evaluated)
    ce_samples = sum(str(row["sector_bias"]) == "CE" for row in evaluated)
    pe_samples = sum(str(row["sector_bias"]) == "PE" for row in evaluated)
    no_trade_samples = sample_count - ce_samples - pe_samples

    return {
        "success": True,
        "version": VERSION,
        "status": "READY_FOR_CHRONOLOGICAL_VALIDATION" if sample_count >= MIN_VALIDATED_SAMPLES else "COLLECTING",
        "sample_count_15m": sample_count,
        "required_samples": MIN_VALIDATED_SAMPLES,
        "progress_percent": round(min(100.0, sample_count / MIN_VALIDATED_SAMPLES * 100.0), 2),
        "sector_bias_match_rate_percent": round(aligned / sample_count * 100.0, 2) if sample_count else None,
        "sector_shadow_net_pnl_per_lot": round(total_pnl, 2),
        "bias_samples": {
            "CE": ce_samples,
            "PE": pe_samples,
            "NO_TRADE": no_trade_samples,
        },
        "recent_by_decision": recent,
        "training_source": "LIVE_SECTOR_BREADTH_PLUS_EXACT_OPTION_OUTCOMES_5_15_30M",
        "model_activation": "NOT_ACTIVE_UNTIL_SEPARATE_CHRONOLOGICAL_VALIDATION",
        "trade_blocking": False,
        "order_execution": False,
    }


def training_health() -> Dict[str, Any]:
    ensure_schema()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT COUNT(*) total,MAX(created_at) latest FROM ai_sector_rotation_training_v1"
        ).fetchone()
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
        "stored_samples": _i(row["total"] if row else 0),
        "latest_sample_at": row["latest"] if row else None,
        "required_validated_samples": MIN_VALIDATED_SAMPLES,
        "trade_blocking": False,
        "order_execution": False,
    }


def apply_sector_rotation_ai_training_patch() -> bool:
    """Patch the loaded Advanced AI runtime; idempotent and fail-closed."""
    global _PATCHED, _LAST_ERROR, _LAST_PATCHED_AT
    with _PATCH_LOCK:
        if _PATCHED:
            return True
        runtime = sys.modules.get("bot.advanced_intelligence_v2")
        if runtime is None or not all(
            hasattr(runtime, name)
            for name in (
                "register_snapshot",
                "observe_outcomes",
                "get_advanced_summary",
                "advanced_health",
            )
        ):
            return False
        try:
            original_register = runtime.register_snapshot
            original_observe = runtime.observe_outcomes
            original_summary = runtime.get_advanced_summary
            original_health = runtime.advanced_health

            if getattr(original_register, "_sector_rotation_ai_training_v1", False):
                _PATCHED = True
                return True

            def register_snapshot(user_id, market, base, option_payload, news, advanced):
                payload = None
                enriched = dict(advanced or {})
                if (
                    bool(market.get("market_open"))
                    and bool(market.get("feed_connected"))
                    and _f(market.get("price")) > 0
                ):
                    payload = _cached_or_live_payload(int(user_id), market)
                if payload:
                    annotation = training_annotation(payload)
                    features = dict(enriched.get("feature") or {})
                    features.update(annotation["features"])
                    enriched["feature"] = features
                    enriched["sector_rotation_training"] = annotation
                    reasons = list(enriched.get("reasons") or [])
                    if "SECTOR_ROTATION_TRAINING_CAPTURED" not in reasons:
                        reasons.append("SECTOR_ROTATION_TRAINING_CAPTURED")
                    enriched["reasons"] = reasons[:20]

                decision_id = original_register(
                    user_id,
                    market,
                    base,
                    option_payload,
                    news,
                    enriched,
                )
                if decision_id and payload:
                    try:
                        _persist_training(
                            str(decision_id),
                            int(user_id),
                            market,
                            option_payload,
                            payload,
                        )
                    except Exception as exc:
                        global _LAST_ERROR
                        _LAST_ERROR = f"PERSIST:{type(exc).__name__}:{str(exc)[:180]}"
                return decision_id

            def observe_outcomes(user_id, market, option_payload):
                created = original_observe(user_id, market, option_payload)
                try:
                    _sync_outcomes(int(user_id))
                except Exception as exc:
                    global _LAST_ERROR
                    _LAST_ERROR = f"OUTCOME_SYNC:{type(exc).__name__}:{str(exc)[:180]}"
                return created

            def get_advanced_summary(user_id, recent_limit=20):
                result = dict(original_summary(user_id, recent_limit=recent_limit) or {})
                sector = training_summary(int(user_id), recent_limit=recent_limit)
                recent = list(result.get("recent_decisions") or [])
                by_decision = dict(sector.pop("recent_by_decision", {}) or {})
                for item in recent:
                    item["sector_rotation_training"] = by_decision.get(str(item.get("id")))
                result["recent_decisions"] = recent
                result["sector_rotation_ai_training"] = sector
                return result

            def advanced_health():
                result = dict(original_health() or {})
                result["sector_rotation_ai_training"] = training_health()
                return result

            register_snapshot._sector_rotation_ai_training_v1 = True
            observe_outcomes._sector_rotation_ai_training_v1 = True
            get_advanced_summary._sector_rotation_ai_training_v1 = True
            advanced_health._sector_rotation_ai_training_v1 = True
            runtime.register_snapshot = register_snapshot
            runtime.observe_outcomes = observe_outcomes
            runtime.get_advanced_summary = get_advanced_summary
            runtime.advanced_health = advanced_health

            routes = sys.modules.get("bot.ai_routes")
            if routes is not None:
                routes.get_advanced_summary = get_advanced_summary
                routes.advanced_health = advanced_health

            ensure_schema()
            _PATCHED = True
            _LAST_PATCHED_AT = _iso()
            _LAST_ERROR = None
            print(
                f"SECTOR ROTATION AI {VERSION} active | training collection ON | "
                "trade blocking OFF | orders OFF"
            )
            return True
        except Exception as exc:
            _LAST_ERROR = f"PATCH:{type(exc).__name__}:{str(exc)[:220]}"
            return False


def _watch_for_runtime() -> None:
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if apply_sector_rotation_ai_training_patch():
            routes = sys.modules.get("bot.ai_routes")
            runtime = sys.modules.get("bot.advanced_intelligence_v2")
            if routes is not None and runtime is not None:
                routes.get_advanced_summary = runtime.get_advanced_summary
                routes.advanced_health = runtime.advanced_health
            return
        time.sleep(0.05)


def schedule_sector_rotation_ai_training_patch() -> None:
    global _WATCHER_STARTED
    with _PATCH_LOCK:
        if _WATCHER_STARTED:
            return
        _WATCHER_STARTED = True
        threading.Thread(
            target=_watch_for_runtime,
            name="okai-sector-rotation-ai-training-v1-loader",
            daemon=True,
        ).start()

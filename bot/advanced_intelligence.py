"""Railway broker-neutral advanced AI shadow intelligence.

Collects option OI/Greeks/depth, global-market context, quote-based option
outcomes and calibrated model labels for Angel One, Upstox and Zerodha.
This module never changes a strategy signal or sends an order.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from bot.advanced_broker_data import (
    CAPABILITIES,
    active_broker_name,
    broker_session,
    direction,
    drop_session,
    global_health,
    global_snapshot,
    integer,
    num,
    option_snapshot,
    quote_contracts,
)
from bot.advanced_model import MIN_MODEL_SAMPLES, dumps, fuse, loads, train
from bot.news_intelligence import aggregate as aggregate_news
from bot.shared_ai import predict
from database import get_db, get_db_storage_info

VERSION = "OKAI-BROKER-NEUTRAL-ADVANCED-AI-V1"
MODEL_VERSION = VERSION + "-SOFTMAX-V1"
HORIZONS_MINUTES = (5, 15, 30)
PRIMARY_HORIZON_MINUTES = 15
POLL_SECONDS = 15
FEATURE_REFRESH_SECONDS = 60
DECISION_SPACING_SECONDS = 300
RETRAIN_STEP_SAMPLES = 50
MAX_RECENT_ROWS = 50
DEFAULT_SLIPPAGE_PERCENT = 0.15

_LOCK = threading.RLock()
_STARTED = False
_THREAD: Optional[threading.Thread] = None
_SNAPSHOT_BUILDER: Optional[Callable[[int], Dict[str, Any]]] = None
_INSTANCE_ID = uuid.uuid4().hex[:12]
_LAST_CYCLE_AT: Optional[str] = None
_LAST_ERROR: Optional[str] = None
_LAST_FEATURE_AT: Dict[int, float] = {}
_LAST_TRAIN_CHECK_MONO = 0.0


def _iso(value: Optional[datetime] = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except Exception:
        return None


def ensure_schema() -> None:
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_advanced_decisions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                broker TEXT NOT NULL,
                symbol TEXT NOT NULL,
                spot_price REAL NOT NULL,
                base_decision TEXT,
                base_confidence INTEGER DEFAULT 0,
                base_probabilities_json TEXT DEFAULT '{}',
                news_json TEXT DEFAULT '{}',
                global_json TEXT DEFAULT '{}',
                option_json TEXT DEFAULT '{}',
                feature_json TEXT DEFAULT '{}',
                model_json TEXT DEFAULT '{}',
                fusion_decision TEXT,
                fusion_confidence INTEGER DEFAULT 0,
                fusion_probabilities_json TEXT DEFAULT '{}',
                reasons_json TEXT DEFAULT '[]',
                ce_contract_json TEXT DEFAULT '{}',
                pe_contract_json TEXT DEFAULT '{}',
                complete INTEGER DEFAULT 0,
                completed_at TEXT,
                trade_blocking INTEGER DEFAULT 0,
                order_execution INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_ai_advanced_decisions_user_created
            ON ai_advanced_decisions(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_ai_advanced_decisions_pending
            ON ai_advanced_decisions(user_id, complete, created_at);

            CREATE TABLE IF NOT EXISTS ai_advanced_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                evaluated_at TEXT NOT NULL,
                spot_exit REAL NOT NULL,
                ce_exit_bid REAL DEFAULT 0,
                pe_exit_bid REAL DEFAULT 0,
                ce_gross_pnl REAL DEFAULT 0,
                pe_gross_pnl REAL DEFAULT 0,
                ce_net_pnl REAL DEFAULT 0,
                pe_net_pnl REAL DEFAULT 0,
                fusion_net_pnl REAL DEFAULT 0,
                base_net_pnl REAL DEFAULT 0,
                best_label TEXT,
                fusion_vs_base_benefit REAL DEFAULT 0,
                UNIQUE(decision_id, horizon_minutes),
                FOREIGN KEY(decision_id) REFERENCES ai_advanced_decisions(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ai_advanced_outcomes_user_horizon
            ON ai_advanced_outcomes(user_id, horizon_minutes, evaluated_at DESC);

            CREATE TABLE IF NOT EXISTS ai_advanced_model_registry (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                model_version TEXT,
                trained_at TEXT,
                sample_count INTEGER DEFAULT 0,
                validation_count INTEGER DEFAULT 0,
                validation_accuracy REAL DEFAULT 0,
                majority_baseline_accuracy REAL DEFAULT 0,
                validation_net_utility REAL DEFAULT 0,
                active INTEGER DEFAULT 0,
                model_json TEXT DEFAULT '{}',
                calibration_json TEXT DEFAULT '{}',
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS ai_advanced_runtime (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                intelligence_version TEXT,
                instance_id TEXT,
                started_at TEXT,
                heartbeat_at TEXT,
                last_error TEXT,
                trade_blocking INTEGER DEFAULT 0,
                order_execution INTEGER DEFAULT 0
            );
            """
        )
        stamp = _iso()
        conn.execute(
            """
            INSERT INTO ai_advanced_runtime (
                singleton, intelligence_version, instance_id, started_at,
                heartbeat_at, last_error, trade_blocking, order_execution
            ) VALUES (1, ?, ?, ?, ?, NULL, 0, 0)
            ON CONFLICT(singleton) DO UPDATE SET
                intelligence_version=excluded.intelligence_version,
                instance_id=excluded.instance_id,
                heartbeat_at=excluded.heartbeat_at,
                trade_blocking=0,
                order_execution=0
            """,
            (VERSION, _INSTANCE_ID, stamp, stamp),
        )
        conn.commit()
    finally:
        conn.close()


def _update_runtime(error: Optional[str] = None) -> None:
    global _LAST_CYCLE_AT, _LAST_ERROR
    _LAST_CYCLE_AT = _iso()
    _LAST_ERROR = str(error)[:400] if error else None
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO ai_advanced_runtime (
                singleton, intelligence_version, instance_id, started_at,
                heartbeat_at, last_error, trade_blocking, order_execution
            ) VALUES (1, ?, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(singleton) DO UPDATE SET
                intelligence_version=excluded.intelligence_version,
                instance_id=excluded.instance_id,
                heartbeat_at=excluded.heartbeat_at,
                last_error=excluded.last_error,
                trade_blocking=0,
                order_execution=0
            """,
            (VERSION, _INSTANCE_ID, _LAST_CYCLE_AT, _LAST_CYCLE_AT, _LAST_ERROR),
        )
        conn.commit()
    finally:
        conn.close()


def _running_user_ids() -> List[int]:
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id FROM (
                SELECT user_id FROM user_bot_state WHERE is_running=1
                UNION ALL
                SELECT user_id FROM bot_status WHERE is_running=1
            ) ORDER BY user_id
            """
        ).fetchall()
        return [int(row["user_id"]) for row in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _previous_option_snapshot(user_id: int) -> Dict[str, Any]:
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT option_json FROM ai_advanced_decisions
            WHERE user_id=? ORDER BY datetime(created_at) DESC, rowid DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        return loads(row["option_json"], {}) if row else {}
    finally:
        conn.close()


def _load_model_registry() -> Dict[str, Any]:
    ensure_schema()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM ai_advanced_model_registry WHERE singleton=1"
        ).fetchone()
        if not row:
            return {"active": False, "sample_count": 0}
        item = dict(row)
        item["active"] = bool(item.get("active"))
        item["model"] = loads(item.pop("model_json", "{}"), {})
        item["calibration"] = loads(item.pop("calibration_json", "{}"), {})
        return item
    finally:
        conn.close()


def _entry_buy_price(contract: Mapping[str, Any]) -> float:
    ask = num(contract.get("ask"))
    ltp = num(contract.get("ltp"))
    return ask if ask > 0 else ltp * (1 + DEFAULT_SLIPPAGE_PERCENT / 100)


def _exit_sell_price(contract: Mapping[str, Any]) -> float:
    bid = num(contract.get("bid"))
    ltp = num(contract.get("ltp"))
    return bid if bid > 0 else ltp * (1 - DEFAULT_SLIPPAGE_PERCENT / 100)


def _charge_estimate(broker: str, entry: float, exit_price: float, quantity: int) -> float:
    env_key = f"OKAI_{str(broker).upper()}_OPTION_ROUND_TRIP_CHARGES_PER_LOT"
    fixed = num(os.getenv(env_key), 80.0)
    turnover = max(0.0, entry + exit_price) * max(1, quantity)
    buffer_rate = num(os.getenv("OKAI_OPTION_CHARGE_BUFFER_RATE"), 0.00005)
    return round(fixed + turnover * buffer_rate, 2)


def _outcome_quotes(
    user_id: int,
    expected_broker: str,
    contracts: Sequence[Mapping[str, Any]],
    spot: float,
) -> Dict[str, Dict[str, Any]]:
    session = broker_session(user_id)
    if session["broker"] != expected_broker:
        session = broker_session(user_id, force=True)
    rows = quote_contracts(session, contracts, spot)
    return {str(row.get("side")): row for row in rows}


def register_decision(
    user_id: int,
    market: Mapping[str, Any],
    base: Mapping[str, Any],
    news: Mapping[str, Any],
    global_data: Mapping[str, Any],
    options: Mapping[str, Any],
    fusion: Mapping[str, Any],
) -> Optional[str]:
    if (
        not market.get("market_open")
        or not market.get("feed_connected")
        or num(market.get("price")) <= 0
    ):
        return None
    ce = dict(options.get("best_ce") or {})
    pe = dict(options.get("best_pe") or {})
    if num(ce.get("ltp")) <= 0 or num(pe.get("ltp")) <= 0:
        return None
    broker = str(options.get("broker") or "")
    symbol = str(market.get("symbol") or "NIFTY").upper()
    current = datetime.now(timezone.utc)
    conn = get_db()
    try:
        last = conn.execute(
            """
            SELECT created_at, broker, symbol, fusion_decision
            FROM ai_advanced_decisions WHERE user_id=?
            ORDER BY datetime(created_at) DESC, rowid DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if last:
            created = _parse_iso(last["created_at"])
            if (
                created
                and str(last["broker"]) == broker
                and str(last["symbol"]) == symbol
                and str(last["fusion_decision"]) == str(fusion.get("decision"))
                and (current - created).total_seconds() < DECISION_SPACING_SECONDS
            ):
                return None
        decision_id = uuid.uuid4().hex[:20]
        conn.execute(
            """
            INSERT INTO ai_advanced_decisions (
                id, user_id, created_at, broker, symbol, spot_price,
                base_decision, base_confidence, base_probabilities_json,
                news_json, global_json, option_json, feature_json, model_json,
                fusion_decision, fusion_confidence, fusion_probabilities_json,
                reasons_json, ce_contract_json, pe_contract_json,
                complete, trade_blocking, order_execution
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0)
            """,
            (
                decision_id,
                int(user_id),
                _iso(current),
                broker,
                symbol,
                round(num(market.get("price")), 2),
                direction(base.get("decision")),
                integer(base.get("confidence")),
                dumps(base.get("probabilities") or {}),
                dumps(news),
                dumps(global_data),
                dumps(options),
                dumps(fusion.get("features") or {}),
                dumps(fusion.get("calibrated_model") or {}),
                direction(fusion.get("decision")),
                integer(fusion.get("confidence")),
                dumps(fusion.get("probabilities") or {}),
                dumps(fusion.get("reasons") or []),
                dumps(ce),
                dumps(pe),
            ),
        )
        conn.commit()
        print(
            "ADVANCED AI RAILWAY | decision logged | "
            f"user={user_id} | broker={broker} | base={base.get('decision')} | "
            f"advanced={fusion.get('decision')} {fusion.get('confidence')}% | "
            "trade blocking OFF | order execution OFF"
        )
        return decision_id
    finally:
        conn.close()


def observe_outcomes(user_id: int, market: Mapping[str, Any]) -> int:
    if not market.get("feed_connected") or num(market.get("price")) <= 0:
        return 0
    current = datetime.now(timezone.utc)
    spot = num(market.get("price"))
    symbol = str(market.get("symbol") or "NIFTY").upper()
    conn = get_db()
    created_count = 0
    try:
        rows = conn.execute(
            """
            SELECT * FROM ai_advanced_decisions
            WHERE user_id=? AND symbol=? AND complete=0
            ORDER BY datetime(created_at) ASC
            """,
            (user_id, symbol),
        ).fetchall()
        for row in rows:
            created = _parse_iso(row["created_at"])
            if created is None:
                continue
            elapsed = (current - created).total_seconds()
            due = [
                horizon
                for horizon in HORIZONS_MINUTES
                if elapsed >= horizon * 60
                and not conn.execute(
                    "SELECT 1 FROM ai_advanced_outcomes WHERE decision_id=? AND horizon_minutes=?",
                    (row["id"], horizon),
                ).fetchone()
            ]
            if not due:
                continue
            ce_entry = loads(row["ce_contract_json"], {})
            pe_entry = loads(row["pe_contract_json"], {})
            try:
                quotes = _outcome_quotes(
                    user_id, str(row["broker"]), [ce_entry, pe_entry], spot
                )
            except Exception as exc:
                drop_session(user_id)
                print(
                    "ADVANCED AI RAILWAY | outcome quote warning | "
                    f"user={user_id} | {str(exc)[:180]}"
                )
                continue
            ce_exit = quotes.get("CE") or {}
            pe_exit = quotes.get("PE") or {}
            if num(ce_exit.get("ltp")) <= 0 or num(pe_exit.get("ltp")) <= 0:
                continue

            def pnl(
                entry_contract: Mapping[str, Any],
                exit_contract: Mapping[str, Any],
            ) -> Tuple[float, float, float]:
                quantity = max(1, integer(entry_contract.get("lot_size"), 1))
                entry_price = _entry_buy_price(entry_contract)
                exit_price = _exit_sell_price(exit_contract)
                gross = (exit_price - entry_price) * quantity
                charges = _charge_estimate(
                    str(row["broker"]), entry_price, exit_price, quantity
                )
                return round(exit_price, 4), round(gross, 2), round(gross - charges, 2)

            ce_exit_bid, ce_gross, ce_net = pnl(ce_entry, ce_exit)
            pe_exit_bid, pe_gross, pe_net = pnl(pe_entry, pe_exit)
            best_label = "NO_TRADE"
            if max(ce_net, pe_net) > 0:
                best_label = "CE" if ce_net >= pe_net else "PE"

            def decision_net(value: Any) -> float:
                side = direction(value)
                return ce_net if side == "CE" else pe_net if side == "PE" else 0.0

            fusion_net = decision_net(row["fusion_decision"])
            base_net = decision_net(row["base_decision"])
            benefit = fusion_net - base_net
            for horizon in due:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ai_advanced_outcomes (
                        decision_id, user_id, horizon_minutes, evaluated_at,
                        spot_exit, ce_exit_bid, pe_exit_bid, ce_gross_pnl,
                        pe_gross_pnl, ce_net_pnl, pe_net_pnl, fusion_net_pnl,
                        base_net_pnl, best_label, fusion_vs_base_benefit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], user_id, horizon, _iso(current), round(spot, 2),
                        ce_exit_bid, pe_exit_bid, ce_gross, pe_gross,
                        ce_net, pe_net, fusion_net, base_net, best_label,
                        round(benefit, 2),
                    ),
                )
                created_count += 1
            outcome_count = conn.execute(
                "SELECT COUNT(*) AS n FROM ai_advanced_outcomes WHERE decision_id=?",
                (row["id"],),
            ).fetchone()["n"]
            if integer(outcome_count) >= len(HORIZONS_MINUTES):
                conn.execute(
                    "UPDATE ai_advanced_decisions SET complete=1, completed_at=? WHERE id=?",
                    (_iso(current), row["id"]),
                )
        conn.execute(
            """
            DELETE FROM ai_advanced_outcomes WHERE decision_id IN (
                SELECT id FROM ai_advanced_decisions
                WHERE user_id=? AND datetime(created_at)<datetime('now','-180 days')
            )
            """,
            (user_id,),
        )
        conn.execute(
            "DELETE FROM ai_advanced_decisions WHERE user_id=? AND datetime(created_at)<datetime('now','-180 days')",
            (user_id,),
        )
        conn.commit()
        return created_count
    finally:
        conn.close()


def maybe_train_model(force: bool = False) -> Dict[str, Any]:
    ensure_schema()
    conn = get_db()
    try:
        current = conn.execute(
            "SELECT * FROM ai_advanced_model_registry WHERE singleton=1"
        ).fetchone()
        last_count = integer(current["sample_count"]) if current else 0
        rows = conn.execute(
            """
            SELECT d.created_at, d.feature_json, o.best_label,
                   o.ce_net_pnl, o.pe_net_pnl
            FROM ai_advanced_outcomes o
            JOIN ai_advanced_decisions d ON d.id=o.decision_id
            WHERE o.horizon_minutes=?
            ORDER BY datetime(d.created_at) ASC, d.rowid ASC
            """,
            (PRIMARY_HORIZON_MINUTES,),
        ).fetchall()
        if len(rows) < MIN_MODEL_SAMPLES:
            return {
                "success": False,
                "reason": "COLLECTING_DATA",
                "sample_count": len(rows),
                "minimum_required": MIN_MODEL_SAMPLES,
            }
        if not force and len(rows) < last_count + RETRAIN_STEP_SAMPLES:
            return {
                "success": True,
                "reason": "MODEL_CURRENT",
                "sample_count": len(rows),
                "last_trained_sample_count": last_count,
            }
        result = train(rows)
        if not result.get("success"):
            return result
        conn.execute(
            """
            INSERT INTO ai_advanced_model_registry (
                singleton, model_version, trained_at, sample_count,
                validation_count, validation_accuracy,
                majority_baseline_accuracy, validation_net_utility,
                active, model_json, calibration_json, last_error
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(singleton) DO UPDATE SET
                model_version=excluded.model_version,
                trained_at=excluded.trained_at,
                sample_count=excluded.sample_count,
                validation_count=excluded.validation_count,
                validation_accuracy=excluded.validation_accuracy,
                majority_baseline_accuracy=excluded.majority_baseline_accuracy,
                validation_net_utility=excluded.validation_net_utility,
                active=excluded.active,
                model_json=excluded.model_json,
                calibration_json=excluded.calibration_json,
                last_error=NULL
            """,
            (
                MODEL_VERSION, _iso(), result["sample_count"],
                result["validation_count"], result["validation_accuracy"],
                result["majority_baseline_accuracy"],
                result["validation_net_utility"], 1 if result["active"] else 0,
                dumps(result["model"]), dumps(result["calibration"]),
            ),
        )
        conn.commit()
        print(
            "ADVANCED AI RAILWAY | model trained | "
            f"samples={result['sample_count']} | validation={result['validation_accuracy']}% | "
            f"active={result['active']} | trade blocking OFF"
        )
        return result
    except Exception as exc:
        try:
            conn.execute(
                """
                INSERT INTO ai_advanced_model_registry (
                    singleton, model_version, trained_at, active, last_error
                ) VALUES (1, ?, ?, 0, ?)
                ON CONFLICT(singleton) DO UPDATE SET last_error=excluded.last_error
                """,
                (MODEL_VERSION, _iso(), f"{type(exc).__name__}:{str(exc)[:350]}"),
            )
            conn.commit()
        except Exception:
            pass
        return {"success": False, "reason": f"{type(exc).__name__}:{str(exc)[:240]}"}
    finally:
        conn.close()


def _decode_decision(row: Mapping[str, Any], conn) -> Dict[str, Any]:
    item = dict(row)
    for source, target, default in (
        ("base_probabilities_json", "base_probabilities", {}),
        ("news_json", "news", {}),
        ("global_json", "global_market", {}),
        ("option_json", "option_intelligence", {}),
        ("feature_json", "features", {}),
        ("model_json", "model", {}),
        ("fusion_probabilities_json", "fusion_probabilities", {}),
        ("reasons_json", "reasons", []),
        ("ce_contract_json", "ce_contract", {}),
        ("pe_contract_json", "pe_contract", {}),
    ):
        item[target] = loads(item.pop(source), default)
    for name in ("complete", "trade_blocking", "order_execution"):
        item[name] = bool(item.get(name))
    item["outcomes"] = [
        dict(outcome)
        for outcome in conn.execute(
            "SELECT * FROM ai_advanced_outcomes WHERE decision_id=? ORDER BY horizon_minutes",
            (item["id"],),
        ).fetchall()
    ]
    return item


def get_advanced_summary(user_id: int, recent_limit: int = 20) -> Dict[str, Any]:
    ensure_schema()
    limit = max(1, min(integer(recent_limit, 20), MAX_RECENT_ROWS))
    conn = get_db()
    try:
        decisions = conn.execute(
            """
            SELECT * FROM ai_advanced_decisions WHERE user_id=?
            ORDER BY datetime(created_at) DESC, rowid DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        primary = conn.execute(
            "SELECT * FROM ai_advanced_outcomes WHERE user_id=? AND horizon_minutes=?",
            (user_id, PRIMARY_HORIZON_MINUTES),
        ).fetchall()
        recent = [_decode_decision(row, conn) for row in decisions]
        registry = _load_model_registry()
        broker = active_broker_name(user_id)
        return {
            "success": True,
            "intelligence_version": VERSION,
            "mode": "BROKER_NEUTRAL_ADVANCED_SHADOW_ONLY",
            "location": "RAILWAY",
            "active_broker": broker,
            "supported_brokers": list(CAPABILITIES),
            "broker_capabilities": CAPABILITIES.get(broker or "", {}),
            "trade_blocking": False,
            "order_execution": False,
            "primary_horizon_minutes": PRIMARY_HORIZON_MINUTES,
            "charges_note": (
                "Option P&L uses live ask/bid when available and configurable "
                "estimated round-trip charges; it is not a broker contract note."
            ),
            "model": {
                "status": "ACTIVE" if registry.get("active") else "COLLECTING_DATA",
                "active": bool(registry.get("active")),
                "model_version": registry.get("model_version"),
                "sample_count": integer(registry.get("sample_count")),
                "minimum_samples": MIN_MODEL_SAMPLES,
                "validation_accuracy": num(registry.get("validation_accuracy")),
                "majority_baseline_accuracy": num(
                    registry.get("majority_baseline_accuracy")
                ),
                "validation_net_utility": num(
                    registry.get("validation_net_utility")
                ),
                "last_error": registry.get("last_error"),
            },
            "summary": {
                "evaluated_15m": len(primary),
                "fusion_profitable_count": sum(
                    num(row["fusion_net_pnl"]) > 0 for row in primary
                ),
                "fusion_loss_count": sum(
                    num(row["fusion_net_pnl"]) < 0 for row in primary
                ),
                "base_profitable_count": sum(
                    num(row["base_net_pnl"]) > 0 for row in primary
                ),
                "fusion_net_estimated_pnl": round(
                    sum(num(row["fusion_net_pnl"]) for row in primary), 2
                ),
                "base_net_estimated_pnl": round(
                    sum(num(row["base_net_pnl"]) for row in primary), 2
                ),
                "fusion_vs_base_estimated_benefit": round(
                    sum(num(row["fusion_vs_base_benefit"]) for row in primary), 2
                ),
            },
            "storage": get_db_storage_info(),
            "recent_decisions": recent,
        }
    finally:
        conn.close()


def advanced_health() -> Dict[str, Any]:
    ensure_schema()
    conn = get_db()
    try:
        runtime = conn.execute(
            "SELECT * FROM ai_advanced_runtime WHERE singleton=1"
        ).fetchone()
        model = conn.execute(
            "SELECT * FROM ai_advanced_model_registry WHERE singleton=1"
        ).fetchone()
        decision_count = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_advanced_decisions"
        ).fetchone()["n"]
        outcome_count = conn.execute(
            "SELECT COUNT(*) AS n FROM ai_advanced_outcomes"
        ).fetchone()["n"]
    finally:
        conn.close()
    model_dict = dict(model) if model else None
    if model_dict:
        model_dict.pop("model_json", None)
        model_dict.pop("calibration_json", None)
        model_dict["active"] = bool(model_dict.get("active"))
    return {
        "success": True,
        "intelligence_version": VERSION,
        "started": bool(_STARTED),
        "thread_alive": bool(_THREAD and _THREAD.is_alive()),
        "instance_id": _INSTANCE_ID,
        "last_cycle_at": _LAST_CYCLE_AT,
        "last_error": _LAST_ERROR,
        "runtime": dict(runtime) if runtime else None,
        "model": model_dict,
        "decision_count": integer(decision_count),
        "outcome_count": integer(outcome_count),
        "supported_brokers": CAPABILITIES,
        "global_market": global_health(),
        "storage": get_db_storage_info(),
        "location": "RAILWAY",
        "mode": "SHADOW_ONLY",
        "trade_blocking": False,
        "order_execution": False,
    }


def _monitor_user(user_id: int, market: Mapping[str, Any]) -> None:
    observe_outcomes(user_id, market)
    last_feature = _LAST_FEATURE_AT.get(user_id, 0.0)
    if time.monotonic() - last_feature < FEATURE_REFRESH_SECONDS:
        return
    _LAST_FEATURE_AT[user_id] = time.monotonic()
    if (
        not market.get("market_open")
        or not market.get("feed_connected")
        or num(market.get("price")) <= 0
    ):
        return
    try:
        base = predict(dict(market))
        news = aggregate_news()
        global_data = global_snapshot()
        options = option_snapshot(
            user_id, market, previous=_previous_option_snapshot(user_id)
        )
        fusion = fuse(
            market, base, news, global_data, options, _load_model_registry()
        )
        register_decision(
            user_id, market, base, news, global_data, options, fusion
        )
    except Exception as exc:
        drop_session(user_id)
        print(
            "ADVANCED AI RAILWAY | cycle warning | "
            f"user={user_id} | {type(exc).__name__}:{str(exc)[:220]}"
        )


def _monitor_cycle() -> None:
    global _LAST_TRAIN_CHECK_MONO
    builder = _SNAPSHOT_BUILDER
    if not callable(builder):
        return
    for user_id in _running_user_ids():
        try:
            _monitor_user(user_id, dict(builder(user_id) or {}))
        except Exception as exc:
            print(
                "ADVANCED AI RAILWAY | user warning | "
                f"user={user_id} | {type(exc).__name__}:{str(exc)[:180]}"
            )
    current = time.monotonic()
    if current - _LAST_TRAIN_CHECK_MONO >= 1800:
        _LAST_TRAIN_CHECK_MONO = current
        maybe_train_model(force=False)


def _monitor_loop() -> None:
    while True:
        error = None
        try:
            _monitor_cycle()
        except Exception as exc:
            error = f"{type(exc).__name__}:{str(exc)[:300]}"
            print(f"ADVANCED AI RAILWAY | monitor warning | {error}")
        try:
            _update_runtime(error)
        except Exception as exc:
            print(
                "ADVANCED AI RAILWAY | heartbeat warning | "
                f"{str(exc)[:180]}"
            )
        time.sleep(POLL_SECONDS)


def start_advanced_intelligence(
    snapshot_builder: Callable[[int], Dict[str, Any]],
) -> Dict[str, Any]:
    global _STARTED, _THREAD, _SNAPSHOT_BUILDER
    with _LOCK:
        _SNAPSHOT_BUILDER = snapshot_builder
        if _STARTED and _THREAD and _THREAD.is_alive():
            return advanced_health()
        ensure_schema()
        _STARTED = True
        _THREAD = threading.Thread(
            target=_monitor_loop,
            name="okai-broker-neutral-advanced-ai",
            daemon=True,
        )
        _THREAD.start()
        print(
            f"ADVANCED AI RAILWAY {VERSION} active | "
            "Angel One + Upstox + Zerodha | "
            "option P&L + OI/Greeks/depth + global context + auto calibration | "
            "monitor only, trade blocking OFF | order execution OFF"
        )
        return advanced_health()

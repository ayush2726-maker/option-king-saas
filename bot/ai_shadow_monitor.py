"""Railway-resident, non-blocking AI shadow outcome monitor.

The monitor runs beside the SaaS market engine. It records shared-AI decisions,
evaluates them after 5/15/30 minutes, and persists results in the Railway SQLite
volume. It never changes signals, entries, exits, quantities, or orders.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from database import get_db, get_db_storage_info
from bot.shared_ai import predict


MONITOR_VERSION = "OKAI-RAILWAY-AI-SHADOW-MONITOR-V1"
HORIZONS_MINUTES = (5, 15, 30)
PRIMARY_HORIZON_MINUTES = 15
MIN_DIRECTIONAL_CONFIDENCE = 75
MIN_RECORD_SPACING_SECONDS = 300
POLL_SECONDS = 15
MAX_RECENT_ROWS = 100

_LOCK = threading.RLock()
_STARTED = False
_THREAD: Optional[threading.Thread] = None
_SNAPSHOT_BUILDER: Optional[Callable[[int], Dict[str, Any]]] = None
_LAST_CYCLE_AT: Optional[str] = None
_LAST_ERROR: Optional[str] = None
_RECOVERY_ATTEMPTS: Dict[int, float] = {}
_INSTANCE_ID = uuid.uuid4().hex[:12]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number == number and abs(number) != float("inf"):
            return number
    except (TypeError, ValueError):
        pass
    return float(default)


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _direction(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"CE", "CALL", "CALL_BUY", "BULL", "BULLISH", "UP", "UPTREND", "LONG_CE"}:
        return "CE"
    if text in {"PE", "PUT", "PUT_BUY", "BEAR", "BEARISH", "DOWN", "DOWNTREND", "SHORT", "LONG_PE"}:
        return "PE"
    if text in {"NO_TRADE", "NO TRADE", "WAIT", "WAITING", "HOLD", "SKIP"}:
        return "NO_TRADE"
    return "WAIT"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return "{}" if isinstance(value, dict) else "[]"


def _json_load(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _signed_points(direction: str, entry: float, exit_price: float) -> float:
    raw = exit_price - entry
    if direction == "CE":
        return raw
    if direction == "PE":
        return -raw
    return 0.0


def _noise_threshold(entry_price: float, horizon_minutes: int) -> float:
    base = max(4.0, abs(entry_price) * 0.00018)
    scale = {5: 0.8, 15: 1.0, 30: 1.25}.get(int(horizon_minutes), 1.0)
    return round(base * scale, 2)


def _relation(strategy_signal: str, strategy_allowed: bool, ai_decision: str) -> str:
    if strategy_allowed and strategy_signal in {"CE", "PE"}:
        if ai_decision == strategy_signal:
            return "AGREE"
        if ai_decision == "NO_TRADE":
            return "AI_WOULD_BLOCK"
        if ai_decision in {"CE", "PE"}:
            return "AI_OPPOSITE"
    if ai_decision in {"CE", "PE"}:
        return "AI_ONLY"
    return "OBSERVE"


def ensure_shadow_schema() -> None:
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_shadow_decisions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                entry_spot REAL NOT NULL,
                ai_decision TEXT NOT NULL,
                ai_confidence INTEGER NOT NULL DEFAULT 0,
                ai_probabilities_json TEXT NOT NULL DEFAULT '{}',
                ai_reasons_json TEXT NOT NULL DEFAULT '[]',
                model_version TEXT,
                strategy_signal TEXT,
                strategy_score REAL DEFAULT 0,
                strategy_min_score REAL DEFAULT 0,
                strategy_trade_allowed INTEGER DEFAULT 0,
                relation TEXT NOT NULL,
                market_regime TEXT,
                adx REAL DEFAULT 0,
                volume_ratio REAL DEFAULT 0,
                mfe_spot_points REAL DEFAULT 0,
                mae_spot_points REAL DEFAULT 0,
                complete INTEGER DEFAULT 0,
                completed_at TEXT,
                trade_blocking INTEGER DEFAULT 0,
                order_execution INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_ai_shadow_decisions_user_created
            ON ai_shadow_decisions(user_id, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_ai_shadow_decisions_pending
            ON ai_shadow_decisions(user_id, complete, created_at);

            CREATE TABLE IF NOT EXISTS ai_shadow_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                evaluated_at TEXT NOT NULL,
                exit_spot REAL NOT NULL,
                spot_change REAL NOT NULL,
                ai_signed_spot_points REAL NOT NULL,
                strategy_signed_spot_points REAL NOT NULL,
                noise_threshold_points REAL NOT NULL,
                outcome TEXT NOT NULL,
                counterfactual TEXT NOT NULL,
                estimated_benefit_spot_points REAL NOT NULL DEFAULT 0,
                UNIQUE(decision_id, horizon_minutes),
                FOREIGN KEY (decision_id) REFERENCES ai_shadow_decisions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ai_shadow_outcomes_user_horizon
            ON ai_shadow_outcomes(user_id, horizon_minutes, evaluated_at DESC);

            CREATE TABLE IF NOT EXISTS ai_shadow_runtime (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                monitor_version TEXT NOT NULL,
                instance_id TEXT,
                started_at TEXT,
                heartbeat_at TEXT,
                last_error TEXT,
                trade_blocking INTEGER DEFAULT 0,
                order_execution INTEGER DEFAULT 0
            );
            """
        )
        now = _iso()
        conn.execute(
            """
            INSERT INTO ai_shadow_runtime (
                singleton, monitor_version, instance_id, started_at, heartbeat_at,
                last_error, trade_blocking, order_execution
            ) VALUES (1, ?, ?, ?, ?, NULL, 0, 0)
            ON CONFLICT(singleton) DO UPDATE SET
                monitor_version=excluded.monitor_version,
                instance_id=excluded.instance_id,
                heartbeat_at=excluded.heartbeat_at,
                trade_blocking=0,
                order_execution=0
            """,
            (MONITOR_VERSION, _INSTANCE_ID, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _update_runtime(last_error: Optional[str] = None) -> None:
    global _LAST_CYCLE_AT, _LAST_ERROR
    _LAST_CYCLE_AT = _iso()
    _LAST_ERROR = str(last_error)[:300] if last_error else None
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO ai_shadow_runtime (
                singleton, monitor_version, instance_id, started_at, heartbeat_at,
                last_error, trade_blocking, order_execution
            ) VALUES (1, ?, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(singleton) DO UPDATE SET
                monitor_version=excluded.monitor_version,
                instance_id=excluded.instance_id,
                heartbeat_at=excluded.heartbeat_at,
                last_error=excluded.last_error,
                trade_blocking=0,
                order_execution=0
            """,
            (MONITOR_VERSION, _INSTANCE_ID, _LAST_CYCLE_AT, _LAST_CYCLE_AT, _LAST_ERROR),
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
            )
            ORDER BY user_id
            """
        ).fetchall()
        return [int(row["user_id"]) for row in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _ensure_user_runtime(user_id: int) -> None:
    now = time.monotonic()
    last = _RECOVERY_ATTEMPTS.get(user_id, 0.0)
    if now - last < 120:
        return
    try:
        from bot.angel_fetcher import get_user_bot_state

        state = dict(get_user_bot_state(user_id) or {})
        if state.get("running"):
            return
        _RECOVERY_ATTEMPTS[user_id] = now
        from bot.routes import _start_saved_runtime_engine

        result = _start_saved_runtime_engine(user_id)
        if result.get("started"):
            print(f"AI SHADOW RAILWAY | runtime restored | user={user_id}")
    except Exception as exc:
        _RECOVERY_ATTEMPTS[user_id] = now
        print(f"AI SHADOW RAILWAY | runtime restore warning | user={user_id} | {str(exc)[:160]}")


def register_shadow_decision(
    user_id: int,
    snapshot: Mapping[str, Any],
    result: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> Optional[str]:
    current = (now or _utc_now()).astimezone(timezone.utc)
    price = _number(snapshot.get("price"), 0.0)
    ai_decision = _direction(result.get("decision"))
    confidence = _integer(result.get("confidence"), 0)
    strategy_signal = _direction(snapshot.get("signal_direction") or snapshot.get("signal"))
    strategy_allowed = bool(snapshot.get("server_trade_allowed", False))
    market_open = bool(snapshot.get("market_open", False))
    feed_connected = bool(snapshot.get("feed_connected", False))
    success = bool(result.get("success", True))

    if not success or not market_open or not feed_connected or price <= 0:
        return None
    if ai_decision not in {"CE", "PE", "NO_TRADE"}:
        return None
    if ai_decision in {"CE", "PE"} and confidence < MIN_DIRECTIONAL_CONFIDENCE:
        return None

    relation = _relation(strategy_signal, strategy_allowed, ai_decision)
    if relation == "OBSERVE":
        return None

    symbol = str(snapshot.get("symbol") or snapshot.get("underlying") or "NIFTY").upper()
    bucket = int(price / 5.0)
    dedupe_key = f"{symbol}|{ai_decision}|{strategy_signal}|{int(strategy_allowed)}|{relation}|{bucket}"

    conn = get_db()
    try:
        last = conn.execute(
            """
            SELECT created_at, symbol, ai_decision, strategy_signal,
                   strategy_trade_allowed, relation, entry_spot
            FROM ai_shadow_decisions
            WHERE user_id=?
            ORDER BY datetime(created_at) DESC, rowid DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if last:
            last_time = _parse_iso(last["created_at"])
            last_key = (
                f"{str(last['symbol']).upper()}|{last['ai_decision']}|{last['strategy_signal']}|"
                f"{int(last['strategy_trade_allowed'] or 0)}|{last['relation']}|{int(_number(last['entry_spot']) / 5.0)}"
            )
            if last_time and last_key == dedupe_key:
                if (current - last_time).total_seconds() < MIN_RECORD_SPACING_SECONDS:
                    return None

        decision_id = uuid.uuid4().hex[:20]
        conn.execute(
            """
            INSERT INTO ai_shadow_decisions (
                id, user_id, created_at, symbol, entry_spot,
                ai_decision, ai_confidence, ai_probabilities_json,
                ai_reasons_json, model_version, strategy_signal,
                strategy_score, strategy_min_score, strategy_trade_allowed,
                relation, market_regime, adx, volume_ratio,
                mfe_spot_points, mae_spot_points, complete,
                trade_blocking, order_execution
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0)
            """,
            (
                decision_id,
                int(user_id),
                _iso(current),
                symbol,
                round(price, 2),
                ai_decision,
                confidence,
                _json(dict(result.get("probabilities") or {})),
                _json([str(item)[:120] for item in (result.get("reasons") or [])[:8]]),
                str(result.get("model_version") or "unknown"),
                strategy_signal,
                round(_number(snapshot.get("strategy_score"), 0.0), 2),
                round(_number(snapshot.get("min_strategy_score"), 0.0), 2),
                1 if strategy_allowed else 0,
                relation,
                str(snapshot.get("market_regime") or ""),
                round(_number(snapshot.get("adx"), 0.0), 2),
                round(_number(snapshot.get("volume_ratio"), 0.0), 4),
            ),
        )
        conn.commit()
        print(
            "AI SHADOW RAILWAY | decision logged | "
            f"user={user_id} | {ai_decision} {confidence}% | strategy={strategy_signal} | {relation} | trade blocking OFF"
        )
        return decision_id
    finally:
        conn.close()


def observe_shadow_outcomes(
    user_id: int,
    snapshot: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> int:
    current = (now or _utc_now()).astimezone(timezone.utc)
    price = _number(snapshot.get("price"), 0.0)
    symbol = str(snapshot.get("symbol") or snapshot.get("underlying") or "NIFTY").upper()
    feed_connected = bool(snapshot.get("feed_connected", False))
    if price <= 0 or not feed_connected:
        return 0

    conn = get_db()
    created_count = 0
    try:
        pending = conn.execute(
            """
            SELECT * FROM ai_shadow_decisions
            WHERE user_id=? AND complete=0 AND symbol=?
            ORDER BY datetime(created_at) ASC
            """,
            (user_id, symbol),
        ).fetchall()

        for row in pending:
            created_at = _parse_iso(row["created_at"])
            if created_at is None:
                continue
            elapsed_seconds = max(0.0, (current - created_at).total_seconds())
            entry = _number(row["entry_spot"], 0.0)
            ai_decision = str(row["ai_decision"] or "NO_TRADE")
            strategy_signal = str(row["strategy_signal"] or "WAIT")
            relation = str(row["relation"] or "OBSERVE")
            ai_points = _signed_points(ai_decision, entry, price)
            strategy_points = _signed_points(strategy_signal, entry, price)

            if ai_decision in {"CE", "PE"}:
                conn.execute(
                    """
                    UPDATE ai_shadow_decisions
                    SET mfe_spot_points=MAX(COALESCE(mfe_spot_points, 0), ?),
                        mae_spot_points=MIN(COALESCE(mae_spot_points, 0), ?)
                    WHERE id=?
                    """,
                    (round(ai_points, 2), round(ai_points, 2), row["id"]),
                )

            for horizon in HORIZONS_MINUTES:
                if elapsed_seconds < horizon * 60:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM ai_shadow_outcomes WHERE decision_id=? AND horizon_minutes=?",
                    (row["id"], horizon),
                ).fetchone()
                if exists:
                    continue

                threshold = _noise_threshold(entry, horizon)
                spot_change = round(price - entry, 2)
                if ai_decision in {"CE", "PE"}:
                    if ai_points >= threshold:
                        outcome = "WIN"
                    elif ai_points <= -threshold:
                        outcome = "LOSS"
                    else:
                        outcome = "FLAT"
                else:
                    outcome = "CORRECT_SKIP" if abs(spot_change) < threshold else "MISSED_MOVE"

                counterfactual = "OBSERVE"
                benefit = 0.0
                if relation == "AI_WOULD_BLOCK":
                    if strategy_points <= -threshold:
                        counterfactual = "AI_BLOCK_WOULD_HELP"
                        benefit = abs(strategy_points)
                    elif strategy_points >= threshold:
                        counterfactual = "AI_BLOCK_WOULD_HURT"
                        benefit = -abs(strategy_points)
                    else:
                        counterfactual = "AI_BLOCK_NEUTRAL"
                elif relation == "AI_OPPOSITE":
                    difference = ai_points - strategy_points
                    if difference > threshold:
                        counterfactual = "AI_OPPOSITE_BETTER"
                    elif difference < -threshold:
                        counterfactual = "AI_OPPOSITE_WORSE"
                    else:
                        counterfactual = "AI_OPPOSITE_NEUTRAL"
                    benefit = difference
                elif relation == "AGREE":
                    if ai_points >= threshold:
                        counterfactual = "AI_AGREEMENT_WIN"
                    elif ai_points <= -threshold:
                        counterfactual = "AI_AGREEMENT_LOSS"
                    else:
                        counterfactual = "AI_AGREEMENT_FLAT"
                elif relation == "AI_ONLY":
                    counterfactual = "AI_ONLY_" + outcome

                conn.execute(
                    """
                    INSERT OR IGNORE INTO ai_shadow_outcomes (
                        decision_id, user_id, horizon_minutes, evaluated_at,
                        exit_spot, spot_change, ai_signed_spot_points,
                        strategy_signed_spot_points, noise_threshold_points,
                        outcome, counterfactual, estimated_benefit_spot_points
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        user_id,
                        horizon,
                        _iso(current),
                        round(price, 2),
                        spot_change,
                        round(ai_points, 2),
                        round(strategy_points, 2),
                        threshold,
                        outcome,
                        counterfactual,
                        round(benefit, 2),
                    ),
                )
                created_count += 1

            outcome_count = conn.execute(
                "SELECT COUNT(*) AS n FROM ai_shadow_outcomes WHERE decision_id=?",
                (row["id"],),
            ).fetchone()["n"]
            if int(outcome_count or 0) >= len(HORIZONS_MINUTES):
                conn.execute(
                    "UPDATE ai_shadow_decisions SET complete=1, completed_at=? WHERE id=?",
                    (_iso(current), row["id"]),
                )

        conn.execute(
            """
            DELETE FROM ai_shadow_outcomes
            WHERE decision_id IN (
                SELECT id FROM ai_shadow_decisions
                WHERE user_id=? AND datetime(created_at) < datetime('now', '-120 days')
            )
            """,
            (user_id,),
        )
        conn.execute(
            "DELETE FROM ai_shadow_decisions WHERE user_id=? AND datetime(created_at) < datetime('now', '-120 days')",
            (user_id,),
        )
        conn.commit()
        return created_count
    finally:
        conn.close()


def _row_dict(row: Any) -> Dict[str, Any]:
    item = dict(row)
    if "ai_probabilities_json" in item:
        item["ai_probabilities"] = _json_load(item.pop("ai_probabilities_json"), {})
    if "ai_reasons_json" in item:
        item["ai_reasons"] = _json_load(item.pop("ai_reasons_json"), [])
    for name in ("strategy_trade_allowed", "complete", "trade_blocking", "order_execution"):
        if name in item:
            item[name] = bool(item[name])
    return item


def get_shadow_summary(user_id: int, recent_limit: int = 20) -> Dict[str, Any]:
    ensure_shadow_schema()
    limit = max(1, min(int(recent_limit or 20), MAX_RECENT_ROWS))
    conn = get_db()
    try:
        decisions = conn.execute(
            "SELECT * FROM ai_shadow_decisions WHERE user_id=? ORDER BY datetime(created_at) DESC, rowid DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        counts = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN complete=0 THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN relation='AGREE' THEN 1 ELSE 0 END) AS agree_count,
                SUM(CASE WHEN relation='AI_WOULD_BLOCK' THEN 1 ELSE 0 END) AS block_count,
                SUM(CASE WHEN relation='AI_OPPOSITE' THEN 1 ELSE 0 END) AS opposite_count,
                SUM(CASE WHEN relation='AI_ONLY' THEN 1 ELSE 0 END) AS ai_only_count
            FROM ai_shadow_decisions WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
        primary = conn.execute(
            """
            SELECT outcome, counterfactual, estimated_benefit_spot_points
            FROM ai_shadow_outcomes
            WHERE user_id=? AND horizon_minutes=?
            """,
            (user_id, PRIMARY_HORIZON_MINUTES),
        ).fetchall()

        directional = [row for row in primary if row["outcome"] in {"WIN", "LOSS", "FLAT"}]
        wins = sum(1 for row in directional if row["outcome"] == "WIN")
        losses = sum(1 for row in directional if row["outcome"] == "LOSS")
        flat = sum(1 for row in directional if row["outcome"] == "FLAT")
        resolved_directional = wins + losses
        hit_rate = round((wins / resolved_directional) * 100.0, 2) if resolved_directional else None
        blocks_helped = sum(1 for row in primary if row["counterfactual"] == "AI_BLOCK_WOULD_HELP")
        blocks_hurt = sum(1 for row in primary if row["counterfactual"] == "AI_BLOCK_WOULD_HURT")
        net_benefit = round(sum(_number(row["estimated_benefit_spot_points"]) for row in primary), 2)

        recent = []
        for row in decisions:
            item = _row_dict(row)
            outcomes = conn.execute(
                "SELECT * FROM ai_shadow_outcomes WHERE decision_id=? ORDER BY horizon_minutes",
                (item["id"],),
            ).fetchall()
            item["outcomes"] = [dict(outcome) for outcome in outcomes]
            recent.append(item)

        storage = get_db_storage_info()
        return {
            "success": True,
            "monitor_version": MONITOR_VERSION,
            "mode": "RAILWAY_SHADOW_ONLY",
            "location": "RAILWAY",
            "trade_blocking": False,
            "order_execution": False,
            "primary_horizon_minutes": PRIMARY_HORIZON_MINUTES,
            "summary": {
                "total_decisions": int(counts["total"] or 0),
                "pending_decisions": int(counts["pending"] or 0),
                "agree_count": int(counts["agree_count"] or 0),
                "ai_would_block_count": int(counts["block_count"] or 0),
                "ai_opposite_count": int(counts["opposite_count"] or 0),
                "ai_only_count": int(counts["ai_only_count"] or 0),
                "directional_15m_wins": wins,
                "directional_15m_losses": losses,
                "directional_15m_flat": flat,
                "directional_15m_hit_rate_percent": hit_rate,
                "ai_blocks_that_would_help": blocks_helped,
                "ai_blocks_that_would_hurt": blocks_hurt,
                "estimated_net_benefit_spot_points_15m": net_benefit,
            },
            "storage": storage,
            "recent_decisions": recent,
        }
    finally:
        conn.close()


def shadow_monitor_health() -> Dict[str, Any]:
    runtime = None
    try:
        ensure_shadow_schema()
        conn = get_db()
        try:
            row = conn.execute("SELECT * FROM ai_shadow_runtime WHERE singleton=1").fetchone()
            runtime = dict(row) if row else None
        finally:
            conn.close()
    except Exception as exc:
        runtime = {"last_error": str(exc)[:300]}
    return {
        "success": True,
        "monitor_version": MONITOR_VERSION,
        "started": bool(_STARTED),
        "thread_alive": bool(_THREAD and _THREAD.is_alive()),
        "instance_id": _INSTANCE_ID,
        "last_cycle_at": _LAST_CYCLE_AT,
        "last_error": _LAST_ERROR,
        "runtime": runtime,
        "storage": get_db_storage_info(),
        "location": "RAILWAY",
        "mode": "SHADOW_ONLY",
        "trade_blocking": False,
        "order_execution": False,
    }


def _monitor_cycle() -> None:
    builder = _SNAPSHOT_BUILDER
    if not callable(builder):
        return
    for user_id in _running_user_ids():
        _ensure_user_runtime(user_id)
        try:
            snapshot = dict(builder(user_id) or {})
            observe_shadow_outcomes(user_id, snapshot)
            result = predict(snapshot)
            register_shadow_decision(user_id, snapshot, result)
        except Exception as exc:
            print(f"AI SHADOW RAILWAY | cycle warning | user={user_id} | {str(exc)[:180]}")


def _monitor_loop() -> None:
    while True:
        error = None
        try:
            _monitor_cycle()
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:240]}"
            print(f"AI SHADOW RAILWAY | monitor warning | {error}")
        try:
            _update_runtime(error)
        except Exception as exc:
            print(f"AI SHADOW RAILWAY | heartbeat warning | {str(exc)[:160]}")
        time.sleep(POLL_SECONDS)


def start_railway_shadow_monitor(
    snapshot_builder: Callable[[int], Dict[str, Any]],
) -> Dict[str, Any]:
    global _STARTED, _THREAD, _SNAPSHOT_BUILDER
    with _LOCK:
        _SNAPSHOT_BUILDER = snapshot_builder
        if _STARTED and _THREAD and _THREAD.is_alive():
            return shadow_monitor_health()
        ensure_shadow_schema()
        _STARTED = True
        _THREAD = threading.Thread(
            target=_monitor_loop,
            name="okai-railway-ai-shadow-monitor",
            daemon=True,
        )
        _THREAD.start()
        print(
            f"AI SHADOW RAILWAY {MONITOR_VERSION} active | DB persistent monitor | "
            "monitor only, trade blocking OFF | order execution OFF"
        )
        return shadow_monitor_health()

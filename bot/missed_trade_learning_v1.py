"""Counterfactual learning for qualified strategy setups that did not open.

This module is deliberately shadow-only.  It records completed-candle AUTO
setups after the normal strategy has made its decision, resolves the exact ATM
CE/PE contracts in a background worker, and evaluates cost-adjusted 5/15/30
minute outcomes.  On-time outcomes are added to the existing chronological
Advanced-AI dataset.  The module never changes a signal, opens an order, blocks
an order, changes quantity, or weakens an execution/risk guard.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping, Optional

from database import get_db, get_db_storage_info
from bot import advanced_intelligence_v2 as advanced
from bot import auto_portfolio_runtime as runtime
from bot.adaptive_model_v2 import maybe_train_models
from bot.angel_fetcher import get_user_bot_state
from bot.market_routes import _get_active_broker, _get_ltp_session, _get_multi_session
from bot.news_intelligence import aggregate as aggregate_news
from bot.shared_ai import predict


VERSION = "OKAI-MISSED-TRADE-LEARNING-SHADOW-V3.1"
SAMPLE_SOURCE = "MISSED_TRADE_SHADOW_V1"
HORIZONS = (5, 15, 30)
PRIMARY_HORIZON = 15
POLL_SECONDS = 15
CAPTURE_SPACING_SECONDS = 300
MAX_ENTRY_QUOTE_DELAY_SECONDS = 90
MAX_TRAINING_QUOTE_DELAY_SECONDS = 120
MAX_HYDRATION_ATTEMPTS = 6
HYDRATION_RETRY_SECONDS = 30
MAX_EVENT_AGE_MINUTES = 240
MAX_HYDRATIONS_PER_CYCLE = 1
MAX_OUTCOMES_PER_CYCLE = 1
RATE_LIMIT_RETRY_SECONDS = 45
MAX_RATE_LIMIT_RETRY_SECONDS = 180
REUSED_OPTION_SNAPSHOT_MAX_AGE_SECONDS = 180

NON_TRAINING_OPERATIONAL_MARKERS = (
    "MARKET_CLOSED",
    "ENTRY_CUTOFF",
    "WEEKEND",
    "FEED_",
    "STALE_",
    "BROKER_",
    "OPTION_CONTRACT",
    "OPTION_QUOTE",
    "OPTION_LTP",
    "PREMIUM_CONFIRMATION",
    "CAPITAL_",
    "SIZING_",
    "ORDER_",
    "COOLDOWN",
    "DAILY_TRADE_LIMIT",
    "MAX_OPEN_POSITION",
    "OPEN_POSITION",
    "LOSS_CIRCUIT",
    "EOD_",
    "EXPIRY_HARDLOCK",
)

_lock = threading.RLock()
_schema_ready = False
_started = False
_thread: Optional[threading.Thread] = None
_last_cycle_at: Optional[str] = None
_last_error: Optional[str] = None
_last_capture_at: Optional[str] = None
_quote_cooldown_until: Optional[datetime] = None
_quote_rate_limit_streak = 0


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        try:
            return float(default)
        except (TypeError, ValueError):
            return 0.0


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def _b(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime] = None) -> str:
    return (
        (value or _now())
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return "{}" if isinstance(value, dict) else "[]"


def _loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except Exception:
        return default


def _unique(values: Iterable[Any], limit: int = 20) -> list[str]:
    output: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text[:220])
        if len(output) >= limit:
            break
    return output


def _market_minutes(now: datetime) -> tuple[int, int]:
    ist = now.astimezone(timezone.utc) + timedelta(hours=5, minutes=30)
    return ist.weekday(), ist.hour * 60 + ist.minute


def _capture_window_open(now: datetime) -> bool:
    weekday, minute = _market_minutes(now)
    return weekday < 5 and (9 * 60 + 15) <= minute < (14 * 60 + 45)


def _quote_window_open(now: datetime) -> bool:
    weekday, minute = _market_minutes(now)
    return weekday < 5 and (9 * 60 + 15) <= minute <= (15 * 60 + 40)


def _missing_due_horizons(
    event: Mapping[str, Any],
    existing: Iterable[int],
    now: datetime,
) -> list[int]:
    started = _parse(event.get("created_at"))
    if started is None:
        return []
    completed = {_i(value) for value in existing or []}
    return [
        horizon
        for horizon in HORIZONS
        if horizon not in completed
        and now >= started + timedelta(minutes=horizon)
    ]


def _due_tracking_events(now: datetime, limit: int = 80) -> list[Dict[str, Any]]:
    """Return only TRACKING rows that have a missing horizon due now.

    Ordering by ``updated_at`` provides round-robin recovery when one contract
    quote fails: the failed row is moved behind other due rows instead of
    consuming every cycle forever.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT * FROM ai_missed_trade_signals_v1
            WHERE status='TRACKING'
              AND (next_retry_at IS NULL OR datetime(next_retry_at)<=datetime(?))
            ORDER BY datetime(updated_at),datetime(created_at),rowid
            LIMIT ?""",
            (_iso(now), max(1, int(limit))),
        ).fetchall()
        events = [dict(row) for row in rows]
        decision_ids = [
            str(event.get("advanced_decision_id") or "")
            for event in events
            if event.get("advanced_decision_id")
        ]
        existing_by_decision: Dict[str, set[int]] = {}
        if decision_ids:
            placeholders = ",".join("?" for _ in decision_ids)
            outcome_rows = conn.execute(
                f"""SELECT decision_id,horizon_minutes
                FROM ai_advanced_v2_contract_outcomes
                WHERE decision_id IN ({placeholders})""",
                decision_ids,
            ).fetchall()
            for row in outcome_rows:
                existing_by_decision.setdefault(
                    str(row["decision_id"]),
                    set(),
                ).add(_i(row["horizon_minutes"]))
    finally:
        conn.close()

    return [
        event
        for event in events
        if _missing_due_horizons(
            event,
            existing_by_decision.get(
                str(event.get("advanced_decision_id") or ""),
                set(),
            ),
            now,
        )
    ]


def ensure_missed_trade_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _lock:
        if _schema_ready:
            return
        advanced.ensure_advanced_schema()
        conn = get_db()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS ai_missed_trade_signals_v1(
                  id TEXT PRIMARY KEY,
                  user_id INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  candle_id TEXT NOT NULL,
                  underlying TEXT NOT NULL,
                  candidate_side TEXT NOT NULL,
                  strategy_score INTEGER NOT NULL,
                  display_score INTEGER NOT NULL,
                  min_score INTEGER NOT NULL,
                  strategy_signal TEXT NOT NULL,
                  trade_allowed INTEGER NOT NULL DEFAULT 0,
                  execution_allowed INTEGER NOT NULL DEFAULT 0,
                  decision_kind TEXT NOT NULL,
                  block_stage TEXT,
                  block_reasons_json TEXT NOT NULL DEFAULT '[]',
                  warnings_json TEXT NOT NULL DEFAULT '[]',
                  market_json TEXT NOT NULL DEFAULT '{}',
                  signal_json TEXT NOT NULL DEFAULT '{}',
                  entry_spot REAL NOT NULL,
                  entry_mode TEXT,
                  learning_eligible INTEGER NOT NULL DEFAULT 0,
                  advanced_decision_id TEXT,
                  status TEXT NOT NULL DEFAULT 'PENDING_CONTRACT',
                  broker TEXT,
                  entry_quote_delay_seconds INTEGER,
                  hydration_attempts INTEGER NOT NULL DEFAULT 0,
                  next_retry_at TEXT,
                  last_error TEXT,
                  completed_at TEXT,
                  version TEXT NOT NULL,
                  UNIQUE(user_id,underlying,candle_id,candidate_side)
                );
                CREATE INDEX IF NOT EXISTS idx_ai_missed_trade_user_created_v1
                  ON ai_missed_trade_signals_v1(user_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ai_missed_trade_pending_v1
                  ON ai_missed_trade_signals_v1(status,next_retry_at,created_at);
                CREATE INDEX IF NOT EXISTS idx_ai_missed_trade_advanced_v1
                  ON ai_missed_trade_signals_v1(advanced_decision_id);
                """
            )
            conn.commit()
            _schema_ready = True
        finally:
            conn.close()


def _candidate_side(signal: Mapping[str, Any]) -> str:
    for value in (
        signal.get("candidate_signal"),
        signal.get("pullback_entry_side"),
        signal.get("signal"),
    ):
        side = str(value or "").upper().strip()
        if side in {"CE", "PE"}:
            return side
    return "WAIT"


def _score_values(signal: Mapping[str, Any]) -> tuple[int, int, int]:
    breakdown = dict(signal.get("live_score_breakdown") or {})
    raw_score = _i(signal.get("score"), 0)
    decision = _i(breakdown.get("decision_score", raw_score), raw_score)
    display = _i(breakdown.get("display_score", breakdown.get("score", raw_score)), raw_score)
    minimum = _i(
        breakdown.get(
            "min_score",
            signal.get("min_score", signal.get("min_score_required", 82)),
        ),
        82,
    )
    return decision, display, minimum


def _reason_values(signal: Mapping[str, Any], state: Mapping[str, Any], attempt: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    reasons: list[Any] = []
    warnings: list[Any] = []
    for key in (
        "execution_block_reason",
        "entry_block_reason",
        "safety_gate_reasons",
        "fresh_entry_block_reasons",
        "entry_timing_block_reasons",
        "missing_mandatory_confirmations",
    ):
        value = signal.get(key)
        if isinstance(value, (list, tuple)):
            reasons.extend(value)
        elif value:
            reasons.append(value)
    raw_warnings = signal.get("warnings") or []
    warnings.extend(raw_warnings if isinstance(raw_warnings, (list, tuple)) else [raw_warnings])
    if attempt.get("reason"):
        reasons.append(attempt.get("reason"))
    permission = dict(state.get("entry_permission") or {})
    if permission and not permission.get("allowed", True):
        reasons.append(permission.get("reason") or "ENTRY_PERMISSION_BLOCKED")
    return _unique(reasons), _unique(warnings)


def _compact_market(market: Mapping[str, Any], underlying: str) -> Dict[str, Any]:
    keys = (
        "price", "vwap", "ema9", "ema21", "adx", "rsi", "atr",
        "volume_ratio", "spread_percent", "supertrend_dir", "trend",
        "mtf_confirmed", "structure_direction", "market_regime", "orb_high",
        "orb_low", "c1_bullish", "c2_bullish", "gap_day",
    )
    result = {key: market.get(key) for key in keys if market.get(key) is not None}
    result["symbol"] = str(underlying).upper()
    return result


def _compact_signal(signal: Mapping[str, Any]) -> Dict[str, Any]:
    keys = (
        "signal", "candidate_signal", "score", "base_score", "min_score",
        "trade_allowed", "execution_allowed", "strategy_qualified",
        "mtf_confirmed", "real_mtf_5m", "entry_window_open",
        "ema_chase_blocked", "vwap_chase_blocked", "chase_blocked",
        "sideways_blocked", "session_counter_trend_blocked",
        "safety_gate_passed", "safety_gate_reasons", "fresh_entry_ok",
        "fresh_entry_block_reasons", "entry_timing_blocked",
        "entry_timing_block_reasons", "warnings", "strategy_profile_key",
        "strategy_profile_name", "pullback_entry_mode", "pullback_entry_state",
        "pullback_entry_reason", "pullback_entry_side", "pullback_entry_ready",
    )
    return {key: signal.get(key) for key in keys if signal.get(key) is not None}


def _row_underlying(row: Any) -> str:
    try:
        value = row["underlying"]
        if value:
            return str(value).upper()
    except Exception:
        pass
    try:
        return str(runtime._underlying(row) or "").upper()
    except Exception:
        return ""


def _matching_attempt(state: Mapping[str, Any], underlying: str, side: str) -> Dict[str, Any]:
    for attempt in state.get("entry_candidate_attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("underlying") or "").upper() != underlying:
            continue
        attempt_side = str(attempt.get("side") or side).upper()
        if attempt_side == side:
            return dict(attempt)
    return {}


def _entry_mode(signal: Mapping[str, Any]) -> str:
    if signal.get("pullback_entry_mode"):
        return "PULLBACK_" + str(signal.get("pullback_entry_state") or "TRACKING")
    return str(signal.get("entry_timing_mode") or "NORMAL_STRATEGY")


def _is_learning_eligible(
    decision_kind: str,
    score: int,
    minimum: int,
    reasons: Iterable[Any],
    signal: Mapping[str, Any],
) -> bool:
    if decision_kind != "STRATEGY_BLOCKED" or score < minimum:
        return False
    if signal.get("pullback_entry_ready", False):
        return False
    joined = "|".join(str(value or "").upper() for value in reasons or [])
    return not any(marker in joined for marker in NON_TRAINING_OPERATIONAL_MARKERS)


def _recent_duplicate(user_id: int, underlying: str, side: str, now: datetime) -> bool:
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT created_at FROM ai_missed_trade_signals_v1
            WHERE user_id=? AND underlying=? AND candidate_side=?
            ORDER BY datetime(created_at) DESC,rowid DESC LIMIT 1""",
            (int(user_id), underlying, side),
        ).fetchone()
    finally:
        conn.close()
    previous = _parse(row["created_at"]) if row else None
    return bool(previous and (now - previous).total_seconds() < CAPTURE_SPACING_SECONDS)


def capture_scan_misses(
    state: Mapping[str, Any],
    scans: Iterable[Mapping[str, Any]],
    rows: Iterable[Any],
    *,
    now: Optional[datetime] = None,
) -> int:
    """Persist strategy decisions after the normal entry attempt has finished."""
    global _last_capture_at
    current = (now or _now()).astimezone(timezone.utc)
    if not _capture_window_open(current):
        return 0
    user_id = _i(state.get("user_id"), 0)
    if user_id <= 0:
        return 0
    ensure_missed_trade_schema()
    active_underlyings = {_row_underlying(row) for row in rows or []}
    created = 0

    for raw_scan in scans or []:
        scan = dict(raw_scan or {})
        if str(scan.get("status") or "").upper() != "OK":
            continue
        underlying = str(scan.get("underlying") or "").upper()
        signal = dict(scan.get("signal_data") or {})
        market = dict(scan.get("market_data") or {})
        side = _candidate_side(signal)
        score, display_score, minimum = _score_values(signal)
        candle_id = str(scan.get("candle_id") or "").strip()
        if (
            not underlying
            or not candle_id
            or side not in {"CE", "PE"}
            or max(score, display_score) < minimum
            or _f(market.get("price")) <= 0
            or signal.get("entry_window_open", True) is False
        ):
            continue

        attempt = _matching_attempt(state, underlying, side)
        if attempt.get("opened"):
            continue
        # A signal in an index that is already held is exposure management, not
        # a missed fresh trade.  Do not teach the model to duplicate positions.
        if underlying in active_underlyings:
            continue
        if _recent_duplicate(user_id, underlying, side, current):
            continue

        trade_allowed = _b(signal.get("trade_allowed"), False)
        execution_allowed = _b(
            signal.get("execution_allowed", scan.get("execution_allowed")),
            trade_allowed,
        )
        permission = dict(state.get("entry_permission") or {})
        if not trade_allowed:
            decision_kind = "STRATEGY_BLOCKED"
            block_stage = "STRATEGY_QUALITY_OR_TIMING"
        elif attempt:
            decision_kind = "EXECUTION_ATTEMPT_FAILED"
            block_stage = str(attempt.get("stage") or "EXECUTION")
        elif permission and not permission.get("allowed", True):
            decision_kind = "PORTFOLIO_OR_SESSION_BLOCKED"
            block_stage = "ENTRY_PERMISSION"
        else:
            decision_kind = "QUALIFIED_NOT_SELECTED"
            block_stage = "PORTFOLIO_SELECTION"

        reasons, warnings = _reason_values(signal, state, attempt)
        if not reasons:
            reasons = [decision_kind]
        learning_eligible = _is_learning_eligible(
            decision_kind,
            score,
            minimum,
            reasons,
            signal,
        )
        event_id = uuid.uuid4().hex[:20]
        now_iso = _iso(current)
        conn = get_db()
        try:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO ai_missed_trade_signals_v1(
                id,user_id,created_at,updated_at,candle_id,underlying,
                candidate_side,strategy_score,display_score,min_score,
                strategy_signal,trade_allowed,execution_allowed,decision_kind,
                block_stage,block_reasons_json,warnings_json,market_json,
                signal_json,entry_spot,entry_mode,learning_eligible,
                advanced_decision_id,status,broker,entry_quote_delay_seconds,
                hydration_attempts,next_retry_at,last_error,completed_at,version)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,
                'PENDING_CONTRACT',NULL,NULL,0,?,NULL,NULL,?)""",
                (
                    event_id,
                    user_id,
                    now_iso,
                    now_iso,
                    candle_id,
                    underlying,
                    side,
                    score,
                    display_score,
                    minimum,
                    str(signal.get("signal") or "WAIT").upper(),
                    1 if trade_allowed else 0,
                    1 if execution_allowed else 0,
                    decision_kind,
                    block_stage,
                    _dumps(reasons),
                    _dumps(warnings),
                    _dumps(_compact_market(market, underlying)),
                    _dumps(_compact_signal(signal)),
                    round(_f(market.get("price")), 2),
                    _entry_mode(signal),
                    1 if learning_eligible else 0,
                    now_iso,
                    VERSION,
                ),
            )
            conn.commit()
            if cursor.rowcount:
                created += 1
                _last_capture_at = now_iso
        finally:
            conn.close()
    return created


def _market_for_ai(event: Mapping[str, Any]) -> Dict[str, Any]:
    market = dict(_loads(event.get("market_json"), {}))
    signal = dict(_loads(event.get("signal_json"), {}))
    price = _f(market.get("price"), _f(event.get("entry_spot")))
    atr = _f(market.get("atr"))
    return {
        **market,
        "source": SAMPLE_SOURCE,
        "symbol": str(event.get("underlying") or "NIFTY").upper(),
        "underlying": str(event.get("underlying") or "NIFTY").upper(),
        "price": price,
        "signal": str(event.get("candidate_side") or "WAIT"),
        "signal_direction": str(event.get("candidate_side") or "WAIT"),
        "strategy_score": _i(event.get("strategy_score")),
        "min_strategy_score": _i(event.get("min_score"), 82),
        "server_trade_allowed": _b(event.get("trade_allowed")),
        "ema_fast": _f(market.get("ema9"), price),
        "ema_slow": _f(market.get("ema21"), price),
        "supertrend_direction": market.get("supertrend_dir") or "",
        "mtf_direction": (signal.get("real_mtf_5m") or {}).get("side") or "",
        "atr_percent": (atr / price * 100.0) if price > 0 and atr > 0 else 0.0,
        "warnings": _loads(event.get("warnings_json"), []),
        "feed_connected": True,
        "market_open": True,
        "has_open_position": False,
        "counterfactual_shadow_sample": True,
    }


def _is_rate_limit_error(value: Any) -> bool:
    text = str(value or "").upper()
    return "429" in text or "TOO MANY REQUEST" in text or "RATE_LIMIT" in text


def _activate_quote_cooldown(now: datetime) -> int:
    global _quote_cooldown_until, _quote_rate_limit_streak
    with _lock:
        _quote_rate_limit_streak = min(_quote_rate_limit_streak + 1, 3)
        delay = min(
            MAX_RATE_LIMIT_RETRY_SECONDS,
            RATE_LIMIT_RETRY_SECONDS * (2 ** (_quote_rate_limit_streak - 1)),
        )
        _quote_cooldown_until = now + timedelta(seconds=delay)
        return int(delay)


def _clear_quote_cooldown() -> None:
    global _quote_cooldown_until, _quote_rate_limit_streak
    with _lock:
        _quote_cooldown_until = None
        _quote_rate_limit_streak = 0


def _quote_cooldown_active(now: datetime) -> bool:
    with _lock:
        return bool(_quote_cooldown_until and now < _quote_cooldown_until)


def _set_hydration_failure(event: Mapping[str, Any], error: str, now: datetime) -> None:
    rate_limited = _is_rate_limit_error(error)
    # A broker throttle is transient and must not permanently invalidate an
    # otherwise exact contract after six automatic retries.
    attempts = _i(event.get("hydration_attempts")) + (0 if rate_limited else 1)
    terminal = not rate_limited and attempts >= MAX_HYDRATION_ATTEMPTS
    retry_seconds = HYDRATION_RETRY_SECONDS
    if rate_limited:
        retry_seconds = _activate_quote_cooldown(now)
    conn = get_db()
    try:
        conn.execute(
            """UPDATE ai_missed_trade_signals_v1
            SET hydration_attempts=?,status=?,last_error=?,next_retry_at=?,updated_at=?
            WHERE id=?""",
            (
                attempts,
                "CONTRACT_UNAVAILABLE" if terminal else "PENDING_CONTRACT",
                str(error or "OPTION_CONTRACT_UNAVAILABLE")[:300],
                None if terminal else _iso(now + timedelta(seconds=retry_seconds)),
                _iso(now),
                event["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _reused_option_payload(event: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Reuse the nearest successful live option snapshot before calling broker.

    The advanced monitor already resolves the same user's ATM option pair. A
    missed-trade row captured within three minutes can safely reuse that exact
    pair, avoiding duplicate option-chain/LTP calls during broker throttling.
    """
    created_at = _parse(event.get("created_at"))
    if created_at is None:
        return None
    lower = _iso(created_at - timedelta(seconds=REUSED_OPTION_SNAPSHOT_MAX_AGE_SECONDS))
    upper = _iso(created_at + timedelta(seconds=REUSED_OPTION_SNAPSHOT_MAX_AGE_SECONDS))
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT created_at,broker,option_summary_json,global_market_json
            FROM ai_advanced_v2_snapshots
            WHERE user_id=? AND symbol=?
              AND COALESCE(sample_source,?)=?
              AND datetime(created_at) BETWEEN datetime(?) AND datetime(?)
            ORDER BY ABS(julianday(created_at)-julianday(?)),rowid DESC
            LIMIT 1""",
            (
                _i(event.get("user_id")),
                str(event.get("underlying") or "NIFTY").upper(),
                advanced.LIVE_SAMPLE_SOURCE,
                advanced.LIVE_SAMPLE_SOURCE,
                lower,
                upper,
                _iso(created_at),
            ),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    option = dict(_loads(row["option_summary_json"], {}))
    if not advanced.selected_contract(option, "CE") or not advanced.selected_contract(option, "PE"):
        return None
    return {
        "success": True,
        "broker": str(row["broker"] or "").lower(),
        "underlying": str(event.get("underlying") or "NIFTY").upper(),
        "option_intelligence": option,
        "global_market": _loads(row["global_market_json"], {}),
        "as_of": row["created_at"],
        "missed_trade_snapshot_reused": True,
    }


def _hydrate_event(event: Mapping[str, Any], now: Optional[datetime] = None) -> bool:
    current = (now or _now()).astimezone(timezone.utc)
    market = _market_for_ai(event)
    try:
        option_payload = dict(
            _reused_option_payload(event)
            or advanced._option_payload(_i(event["user_id"]), market)
            or {}
        )
        option = dict(option_payload.get("option_intelligence") or {})
        if not option_payload.get("success"):
            raise RuntimeError(option_payload.get("reason") or "OPTION_INTELLIGENCE_UNAVAILABLE")
        if not advanced.selected_contract(option, "CE") or not advanced.selected_contract(option, "PE"):
            raise RuntimeError("ATM_CE_PE_CONTRACT_PAIR_UNAVAILABLE")
        base = predict(market)
        news = aggregate_news()
        fused = advanced.fuse_advanced(market, base, option_payload, news)
        source_time = _parse(option_payload.get("as_of")) or current
        event_time = _parse(event.get("created_at")) or current
        quote_delay = max(0, _i(abs((source_time - event_time).total_seconds())))
        learning_eligible = bool(
            _b(event.get("learning_eligible"))
            and quote_delay <= MAX_ENTRY_QUOTE_DELAY_SECONDS
        )
        decision_id = advanced.register_snapshot(
            _i(event["user_id"]),
            market,
            base,
            option_payload,
            news,
            fused,
            sample_source=SAMPLE_SOURCE,
            source_event_id=str(event["id"]),
            strategy_context={
                "candidate_side": event.get("candidate_side"),
                "score": event.get("strategy_score"),
                "min_score": event.get("min_score"),
                "trade_allowed": _b(event.get("trade_allowed")),
                "execution_allowed": _b(event.get("execution_allowed")),
                "block_reasons": _loads(event.get("block_reasons_json"), []),
                "learning_eligible": learning_eligible,
            },
            force_record=True,
            created_at=event.get("created_at"),
        )
        if not decision_id:
            raise RuntimeError("ADVANCED_COUNTERFACTUAL_SNAPSHOT_NOT_RECORDED")
        conn = get_db()
        try:
            conn.execute(
                """UPDATE ai_missed_trade_signals_v1
                SET advanced_decision_id=?,status='TRACKING',broker=?,
                    entry_quote_delay_seconds=?,learning_eligible=?,
                    hydration_attempts=hydration_attempts+1,next_retry_at=NULL,
                    last_error=NULL,updated_at=? WHERE id=?""",
                (
                    str(decision_id),
                    str(option_payload.get("broker") or "").lower(),
                    quote_delay,
                    1 if learning_eligible else 0,
                    _iso(current),
                    event["id"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as exc:
        _set_hydration_failure(
            event,
            f"{type(exc).__name__}:{str(exc)[:240]}",
            current,
        )
        return False


def _quote_contract(user_id: int, broker_name: str, contract: Mapping[str, Any]) -> Dict[str, Any]:
    active_broker, creds = _get_active_broker(int(user_id))
    active = str(active_broker or "").lower()
    expected = str(broker_name or "").lower()
    if not active or not creds:
        return {"success": False, "reason": "ACTIVE_BROKER_NOT_CONNECTED"}
    if expected and active != expected:
        return {"success": False, "reason": f"BROKER_CHANGED:{expected}->{active}"}
    try:
        symbol = str(contract.get("symbol") or "")
        token = str(contract.get("token") or contract.get("instrument_key") or "")
        exchange = str(contract.get("exchange") or ("BFO" if active == "zerodha" else "NFO"))
        if active == "angelone":
            obj = _get_ltp_session(int(user_id), creds)
            raw = obj.ltpData(exchange, symbol, token)
            if not isinstance(raw, dict) or not raw.get("status"):
                raise RuntimeError(str((raw or {}).get("message") or "ANGEL_OPTION_LTP_FAILED"))
            ltp = _f((raw.get("data") or {}).get("ltp"))
        else:
            obj = _get_multi_session(int(user_id), active, creds)
            identifier = token if active == "upstox" and token else symbol
            raw = obj.get_ltp(identifier, exchange=exchange)
            if not isinstance(raw, dict) or not raw.get("success"):
                raise RuntimeError(str((raw or {}).get("message") or "OPTION_LTP_FAILED"))
            ltp = _f(raw.get("ltp"))
        if ltp <= 0:
            raise RuntimeError("INVALID_OPTION_LTP")
        return {"success": True, "ltp": round(ltp, 4), "source": f"{active.upper()}_LIVE_LTP"}
    except Exception as exc:
        return {"success": False, "reason": f"{type(exc).__name__}:{str(exc)[:220]}"}


def _quote_contract_pair(
    user_id: int,
    broker_name: str,
    ce_contract: Mapping[str, Any],
    pe_contract: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Quote an ATM CE/PE pair with one Upstox request when possible."""
    active_broker, creds = _get_active_broker(int(user_id))
    active = str(active_broker or "").lower()
    expected = str(broker_name or "").lower()
    if not active or not creds:
        failure = {"success": False, "reason": "ACTIVE_BROKER_NOT_CONNECTED"}
        return {"ce": dict(failure), "pe": dict(failure)}
    if expected and active != expected:
        failure = {
            "success": False,
            "reason": f"BROKER_CHANGED:{expected}->{active}",
        }
        return {"ce": dict(failure), "pe": dict(failure)}

    exchanges = [
        str(contract.get("exchange") or "NFO")
        for contract in (ce_contract, pe_contract)
    ]
    if active == "upstox" and exchanges[0] == exchanges[1]:
        try:
            obj = _get_multi_session(int(user_id), active, creds)
            if hasattr(obj, "get_ltps"):
                identifiers = [
                    str(
                        contract.get("token")
                        or contract.get("instrument_key")
                        or contract.get("symbol")
                        or ""
                    )
                    for contract in (ce_contract, pe_contract)
                ]
                raw = obj.get_ltps(identifiers, exchange=exchanges[0])
                quotes = dict((raw or {}).get("quotes") or {})
                if not isinstance(raw, dict) or not raw.get("success"):
                    reason = str((raw or {}).get("message") or "OPTION_LTP_FAILED")
                    if (raw or {}).get("rate_limited") or _is_rate_limit_error(reason):
                        reason = "UPSTOX_RATE_LIMITED: automatic retry scheduled"
                    failure = {"success": False, "reason": reason[:220]}
                    return {"ce": dict(failure), "pe": dict(failure)}

                segment = (
                    "BSE_FO"
                    if exchanges[0].upper().startswith(("BSE", "BFO"))
                    else "NSE_FO"
                )
                result: Dict[str, Dict[str, Any]] = {}
                for side, identifier in zip(("ce", "pe"), identifiers):
                    full_key = identifier if "|" in identifier else f"{segment}|{identifier}"
                    quote = quotes.get(identifier) or quotes.get(full_key) or {}
                    ltp = _f(quote.get("ltp"))
                    if ltp <= 0:
                        result[side] = {
                            "success": False,
                            "reason": f"OPTION_LTP_MISSING:{full_key}"[:220],
                        }
                    else:
                        result[side] = {
                            "success": True,
                            "ltp": round(ltp, 4),
                            "source": str(
                                quote.get("quote_source")
                                or raw.get("quote_source")
                                or "UPSTOX_BATCH_LTP"
                            ),
                        }
                return result
        except Exception as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:200]}"
            if _is_rate_limit_error(reason):
                reason = "UPSTOX_RATE_LIMITED: automatic retry scheduled"
            failure = {"success": False, "reason": reason}
            return {"ce": dict(failure), "pe": dict(failure)}

    # Other brokers do not expose a compatible multi-quote API here. This also
    # preserves safe fallback behavior for older Upstox session objects.
    return {
        "ce": _quote_contract(user_id, broker_name, ce_contract),
        "pe": _quote_contract(user_id, broker_name, pe_contract),
    }


def _latest_spot(user_id: int, underlying: str, fallback: float) -> float:
    state = dict(get_user_bot_state(int(user_id)) or {})
    for scan in state.get("scan_results") or []:
        if (
            isinstance(scan, dict)
            and str(scan.get("underlying") or "").upper() == underlying.upper()
            and _f(scan.get("price")) > 0
        ):
            return _f(scan.get("price"))
    if str(state.get("underlying") or "").upper() == underlying.upper():
        return _f(state.get("price"), fallback)
    return float(fallback)


def _set_outcome_failure(event: Mapping[str, Any], reason: str, now: datetime) -> None:
    rate_limited = _is_rate_limit_error(reason)
    retry_seconds = (
        _activate_quote_cooldown(now) if rate_limited else POLL_SECONDS
    )
    message = (
        f"UPSTOX_RATE_LIMITED: retrying automatically after {retry_seconds}s"
        if rate_limited
        else str(reason or "OPTION_QUOTE_UNAVAILABLE")[:300]
    )
    conn = get_db()
    try:
        conn.execute(
            """UPDATE ai_missed_trade_signals_v1
            SET last_error=?,next_retry_at=?,updated_at=? WHERE id=?""",
            (
                message,
                _iso(now + timedelta(seconds=retry_seconds)),
                _iso(now),
                event["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_counterfactual_outcome(
    event: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    horizon: int,
    now: datetime,
) -> bool:
    ce_entry = _loads(snapshot.get("ce_contract_json"), {})
    pe_entry = _loads(snapshot.get("pe_contract_json"), {})
    quote_pair = _quote_contract_pair(
        _i(event["user_id"]),
        str(event.get("broker")),
        ce_entry,
        pe_entry,
    )
    ce_quote = dict(quote_pair.get("ce") or {})
    pe_quote = dict(quote_pair.get("pe") or {})
    if not (ce_quote.get("success") and pe_quote.get("success")):
        reason = ce_quote.get("reason") or pe_quote.get("reason") or "OPTION_QUOTE_UNAVAILABLE"
        _set_outcome_failure(event, str(reason), now)
        return False
    _clear_quote_cooldown()

    ce_result = advanced._contract_pnl(
        str(event.get("broker") or "angelone"),
        str(event.get("underlying") or "NIFTY"),
        ce_entry,
        {"ltp": ce_quote["ltp"]},
    )
    pe_result = advanced._contract_pnl(
        str(event.get("broker") or "angelone"),
        str(event.get("underlying") or "NIFTY"),
        pe_entry,
        {"ltp": pe_quote["ltp"]},
    )
    if not (ce_result.get("available") and pe_result.get("available")):
        return False

    ce_net = _f(ce_result.get("net_pnl"))
    pe_net = _f(pe_result.get("net_pnl"))
    candidate_side = str(event.get("candidate_side") or "WAIT").upper()
    candidate_net = ce_net if candidate_side == "CE" else pe_net
    candidate_outcome = "WIN" if candidate_net > 0 else "LOSS" if candidate_net < 0 else "FLAT"
    verdict = (
        "MISSED_PROFIT"
        if candidate_net > 0
        else "BLOCK_AVOIDED_LOSS"
        if candidate_net < 0
        else "NO_EDGE_AFTER_COSTS"
    )
    best_label = max(
        (("CE", ce_net), ("PE", pe_net), ("NO_TRADE", 0.0)),
        key=lambda item: item[1],
    )[0]
    advanced_net = advanced._decision_pnl(snapshot.get("advanced_decision"), ce_net, pe_net)
    base_net = advanced._decision_pnl(snapshot.get("base_decision"), ce_net, pe_net)
    started = _parse(event.get("created_at")) or now
    quote_delay = max(0, _i((now - (started + timedelta(minutes=horizon))).total_seconds()))
    training_eligible = bool(
        _b(event.get("learning_eligible"))
        and _i(event.get("entry_quote_delay_seconds"), 999999)
        <= MAX_ENTRY_QUOTE_DELAY_SECONDS
        and quote_delay <= MAX_TRAINING_QUOTE_DELAY_SECONDS
    )
    spot_exit = _latest_spot(
        _i(event["user_id"]),
        str(event.get("underlying") or "NIFTY"),
        _f(event.get("entry_spot")),
    )
    details = {
        "ce": ce_result,
        "pe": pe_result,
        "entry_spot": _f(event.get("entry_spot")),
        "exit_spot": spot_exit,
        "candidate_side": candidate_side,
        "candidate_net_pnl": round(candidate_net, 2),
        "candidate_outcome": candidate_outcome,
        "missed_trade_verdict": verdict,
        "should_have_taken": candidate_net > 0,
        "decision_kind": event.get("decision_kind"),
        "block_reasons": _loads(event.get("block_reasons_json"), []),
        "entry_quote_delay_seconds": _i(event.get("entry_quote_delay_seconds")),
        "outcome_quote_delay_seconds": quote_delay,
        "training_eligible": training_eligible,
        "counterfactual_only": True,
        "premium_basis": "ENTRY_ASK_OR_LTP_EXIT_LIVE_LTP",
        "cost_model": "INDIA_INDEX_OPTIONS_ALL_COSTS_V2",
    }

    conn = get_db()
    try:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO ai_advanced_v2_contract_outcomes(
            decision_id,user_id,horizon_minutes,evaluated_at,spot_exit,
            ce_entry_price,ce_exit_price,ce_net_pnl,pe_entry_price,
            pe_exit_price,pe_net_pnl,no_trade_net_pnl,advanced_net_pnl,
            base_net_pnl,advanced_vs_base_benefit,best_label,
            advanced_outcome,base_outcome,charge_model,details_json,
            sample_source,training_eligible,quote_delay_seconds) VALUES(
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot["id"],
                _i(event["user_id"]),
                int(horizon),
                _iso(now),
                round(spot_exit, 2),
                ce_result["entry_price"],
                ce_result["exit_price"],
                round(ce_net, 2),
                pe_result["entry_price"],
                pe_result["exit_price"],
                round(pe_net, 2),
                0.0,
                round(advanced_net, 2),
                round(base_net, 2),
                round(advanced_net - base_net, 2),
                best_label,
                advanced._outcome(advanced_net),
                advanced._outcome(base_net),
                "INDIA_INDEX_OPTIONS_ALL_COSTS_V2",
                _dumps(details),
                SAMPLE_SOURCE,
                1 if training_eligible else 0,
                quote_delay,
            ),
        )
        if cursor.rowcount:
            conn.execute(
                """UPDATE ai_missed_trade_signals_v1
                SET updated_at=?,last_error=NULL,next_retry_at=NULL WHERE id=?""",
                (_iso(now), event["id"]),
            )
        conn.commit()
        return bool(cursor.rowcount)
    finally:
        conn.close()


def _evaluate_event(event: Mapping[str, Any], now: Optional[datetime] = None) -> int:
    current = (now or _now()).astimezone(timezone.utc)
    decision_id = str(event.get("advanced_decision_id") or "")
    if not decision_id:
        return 0
    conn = get_db()
    try:
        snapshot_row = conn.execute(
            "SELECT * FROM ai_advanced_v2_snapshots WHERE id=?",
            (decision_id,),
        ).fetchone()
        existing = {
            _i(row["horizon_minutes"])
            for row in conn.execute(
                "SELECT horizon_minutes FROM ai_advanced_v2_contract_outcomes WHERE decision_id=?",
                (decision_id,),
            ).fetchall()
        }
    finally:
        conn.close()
    if not snapshot_row:
        return 0
    snapshot = dict(snapshot_row)
    started = _parse(event.get("created_at")) or current
    created = 0
    for horizon in HORIZONS:
        if horizon in existing or current < started + timedelta(minutes=horizon):
            continue
        if _insert_counterfactual_outcome(event, snapshot, horizon, current):
            created += 1
        # One broker quote pair is enough work for this event in one pass.
        break

    conn = get_db()
    try:
        count = _i(
            conn.execute(
                "SELECT COUNT(*) n FROM ai_advanced_v2_contract_outcomes WHERE decision_id=?",
                (decision_id,),
            ).fetchone()["n"]
        )
        if count >= len(HORIZONS):
            completed_at = _iso(current)
            conn.execute(
                "UPDATE ai_advanced_v2_snapshots SET complete=1,completed_at=? WHERE id=?",
                (completed_at, decision_id),
            )
            conn.execute(
                """UPDATE ai_missed_trade_signals_v1
                SET status='COMPLETE',completed_at=?,updated_at=?,last_error=NULL
                WHERE id=?""",
                (completed_at, completed_at, event["id"]),
            )
        conn.commit()
    finally:
        conn.close()
    return created


def _expire_stale_events(now: datetime) -> None:
    cutoff = _iso(now - timedelta(minutes=MAX_EVENT_AGE_MINUTES))
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id,advanced_decision_id FROM ai_missed_trade_signals_v1
            WHERE status IN('TRACKING','PENDING_CONTRACT')
              AND datetime(created_at)<datetime(?)""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """UPDATE ai_missed_trade_signals_v1
                SET status='PARTIAL_OR_UNAVAILABLE',completed_at=?,updated_at=?,
                    last_error=COALESCE(last_error,'OUTCOME_WINDOW_EXPIRED')
                WHERE id=?""",
                (_iso(now), _iso(now), row["id"]),
            )
            if row["advanced_decision_id"]:
                conn.execute(
                    "UPDATE ai_advanced_v2_snapshots SET complete=1,completed_at=? WHERE id=?",
                    (_iso(now), row["advanced_decision_id"]),
                )
        conn.commit()
    finally:
        conn.close()


def run_learning_cycle(now: Optional[datetime] = None) -> Dict[str, Any]:
    global _last_cycle_at, _last_error
    current = (now or _now()).astimezone(timezone.utc)
    ensure_missed_trade_schema()
    hydrated = outcomes = 0
    try:
        if _quote_window_open(current):
            # Due outcomes are time-sensitive and use already resolved exact
            # contracts, so process one before the heavier contract hydration.
            # A CE/PE pair is one batched Upstox request, preventing quote bursts.
            if not _quote_cooldown_active(current):
                tracking = _due_tracking_events(current)
                attempted = 0
                for event in tracking:
                    created = _evaluate_event(event, current)
                    if created:
                        outcomes += created
                    attempted += 1
                    if attempted >= MAX_OUTCOMES_PER_CYCLE:
                        break

            # Do not immediately spend more broker quota after an observed 429.
            if not _quote_cooldown_active(current):
                conn = get_db()
                try:
                    pending = conn.execute(
                        """SELECT * FROM ai_missed_trade_signals_v1
                        WHERE status='PENDING_CONTRACT'
                          AND (next_retry_at IS NULL OR datetime(next_retry_at)<=datetime(?))
                        ORDER BY datetime(created_at),rowid LIMIT ?""",
                        (_iso(current), MAX_HYDRATIONS_PER_CYCLE),
                    ).fetchall()
                finally:
                    conn.close()
                for row in pending:
                    hydrated += 1 if _hydrate_event(dict(row), current) else 0
        _expire_stale_events(current)
        if outcomes:
            maybe_train_models(force=False)
        _last_error = None
    except Exception as exc:
        _last_error = f"{type(exc).__name__}:{str(exc)[:300]}"
    _last_cycle_at = _iso(current)
    return {
        "success": _last_error is None,
        "hydrated": hydrated,
        "outcomes_created": outcomes,
        "quote_cooldown_until": (
            _iso(_quote_cooldown_until) if _quote_cooldown_until else None
        ),
        "last_error": _last_error,
    }


def _loop() -> None:
    while True:
        result = run_learning_cycle()
        if not result.get("success"):
            print(f"MISSED TRADE LEARNING | warning | {result.get('last_error')}")
        time.sleep(POLL_SECONDS)


def start_missed_trade_learning() -> Dict[str, Any]:
    global _started, _thread
    with _lock:
        ensure_missed_trade_schema()
        if _started and _thread and _thread.is_alive():
            return missed_trade_health()
        _started = True
        _thread = threading.Thread(
            target=_loop,
            name="okai-missed-trade-learning-shadow-v1",
            daemon=True,
        )
        _thread.start()
        print(
            f"MISSED TRADE LEARNING {VERSION} active | 5/15/30m exact option outcomes | "
            "trade blocking OFF | orders OFF"
        )
        return missed_trade_health()


def _outcome_view(row: Mapping[str, Any], candidate_side: str) -> Dict[str, Any]:
    item = dict(row)
    details = _loads(item.pop("details_json", "{}"), {})
    candidate_net = _f(
        details.get("candidate_net_pnl"),
        item.get("ce_net_pnl") if candidate_side == "CE" else item.get("pe_net_pnl"),
    )
    verdict = str(details.get("missed_trade_verdict") or "")
    if not verdict:
        verdict = (
            "MISSED_PROFIT"
            if candidate_net > 0
            else "BLOCK_AVOIDED_LOSS"
            if candidate_net < 0
            else "NO_EDGE_AFTER_COSTS"
        )
    item.update(
        {
            "candidate_side": candidate_side,
            "candidate_net_pnl": round(candidate_net, 2),
            "candidate_outcome": details.get("candidate_outcome")
            or ("WIN" if candidate_net > 0 else "LOSS" if candidate_net < 0 else "FLAT"),
            "verdict": verdict,
            "should_have_taken": bool(details.get("should_have_taken", candidate_net > 0)),
            "details": details,
            "training_eligible": bool(item.get("training_eligible")),
        }
    )
    return item


def _contract_view(raw: Mapping[str, Any]) -> Dict[str, Any]:
    contract = dict(raw or {})
    symbol = str(
        contract.get("symbol")
        or contract.get("trading_symbol")
        or contract.get("tradingsymbol")
        or ""
    )
    token = str(contract.get("token") or contract.get("instrument_key") or "")
    return {
        "symbol": symbol,
        "token": token,
        "instrument_key": str(contract.get("instrument_key") or token),
        "exchange": str(contract.get("exchange") or ""),
        "side": str(contract.get("side") or "").upper(),
        "strike": _f(contract.get("strike")),
        "expiry": str(contract.get("expiry") or ""),
        "lot_size": max(1, _i(contract.get("lot_size"), 1)),
        "ltp": _f(contract.get("ltp")),
        "bid": _f(contract.get("bid")),
        "ask": _f(contract.get("ask")),
    }


def get_missed_trade_summary(user_id: int, recent_limit: int = 20) -> Dict[str, Any]:
    ensure_missed_trade_schema()
    limit = max(1, min(_i(recent_limit, 20), 50))
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT m.*,s.ce_contract_json AS snapshot_ce_contract_json,
            s.pe_contract_json AS snapshot_pe_contract_json
            FROM ai_missed_trade_signals_v1 m
            LEFT JOIN ai_advanced_v2_snapshots s ON s.id=m.advanced_decision_id
            WHERE m.user_id=?
            ORDER BY datetime(m.created_at) DESC,m.rowid DESC LIMIT ?""",
            (int(user_id), limit),
        ).fetchall()
        all_rows = conn.execute(
            "SELECT status FROM ai_missed_trade_signals_v1 WHERE user_id=?",
            (int(user_id),),
        ).fetchall()
        primary_rows = conn.execute(
            """SELECT o.*,m.candidate_side FROM ai_advanced_v2_contract_outcomes o
            JOIN ai_missed_trade_signals_v1 m ON m.advanced_decision_id=o.decision_id
            WHERE m.user_id=? AND o.horizon_minutes=?
              AND COALESCE(o.sample_source,?)=?""",
            (int(user_id), PRIMARY_HORIZON, SAMPLE_SOURCE, SAMPLE_SOURCE),
        ).fetchall()
        recent = []
        for raw in rows:
            item = dict(raw)
            item["block_reasons"] = _loads(item.pop("block_reasons_json"), [])
            item["warnings"] = _loads(item.pop("warnings_json"), [])
            item["market"] = _loads(item.pop("market_json"), {})
            item["signal_snapshot"] = _loads(item.pop("signal_json"), {})
            outcome_rows = []
            candidate_contract = {}
            if item.get("advanced_decision_id"):
                contract_column = (
                    "snapshot_ce_contract_json"
                    if str(item.get("candidate_side") or "").upper() == "CE"
                    else "snapshot_pe_contract_json"
                )
                candidate_contract = _contract_view(
                    _loads(item.get(contract_column), {})
                )
                outcome_rows = conn.execute(
                    """SELECT * FROM ai_advanced_v2_contract_outcomes
                    WHERE decision_id=? ORDER BY horizon_minutes""",
                    (item["advanced_decision_id"],),
                ).fetchall()
            item.pop("snapshot_ce_contract_json", None)
            item.pop("snapshot_pe_contract_json", None)
            item["outcomes"] = [
                _outcome_view(dict(outcome), str(item["candidate_side"]))
                for outcome in outcome_rows
            ]
            item["primary_outcome"] = next(
                (
                    outcome
                    for outcome in item["outcomes"]
                    if _i(outcome.get("horizon_minutes")) == PRIMARY_HORIZON
                ),
                item["outcomes"][-1] if item["outcomes"] else None,
            )
            primary_details = dict((item["primary_outcome"] or {}).get("details") or {})
            side_details = dict(
                primary_details.get(str(item.get("candidate_side") or "").lower()) or {}
            )
            candidate_entry_price = _f(
                side_details.get("entry_price"),
                candidate_contract.get("ask") or candidate_contract.get("ltp"),
            )
            item["candidate_contract"] = candidate_contract
            item["candidate_entry_price"] = round(candidate_entry_price, 2)
            item["candidate_lot_size"] = max(
                1,
                _i(
                    side_details.get("quantity"),
                    candidate_contract.get("lot_size") or 1,
                ),
            )
            item["learning_eligible"] = bool(item.get("learning_eligible"))
            item["trade_allowed"] = bool(item.get("trade_allowed"))
            item["execution_allowed"] = bool(item.get("execution_allowed"))
            recent.append(item)
    finally:
        conn.close()

    primary = [
        _outcome_view(dict(row), str(row["candidate_side"]).upper())
        for row in primary_rows
    ]
    missed_profit = sum(item["verdict"] == "MISSED_PROFIT" for item in primary)
    avoided_loss = sum(item["verdict"] == "BLOCK_AVOIDED_LOSS" for item in primary)
    no_edge = sum(item["verdict"] == "NO_EDGE_AFTER_COSTS" for item in primary)
    active_statuses = {"PENDING_CONTRACT", "TRACKING"}
    return {
        "success": True,
        "version": VERSION,
        "mode": "COUNTERFACTUAL_SHADOW_ONLY",
        "counterfactual_only": True,
        "trade_blocking": False,
        "order_execution": False,
        "horizons_minutes": list(HORIZONS),
        "primary_horizon_minutes": PRIMARY_HORIZON,
        "summary": {
            "captured_total": len(all_rows),
            "tracking": sum(str(row["status"]) in active_statuses for row in all_rows),
            "evaluated_15m": len(primary),
            "would_have_profited_15m": missed_profit,
            "block_avoided_loss_15m": avoided_loss,
            "no_edge_after_costs_15m": no_edge,
            "training_samples_added_15m": sum(item["training_eligible"] for item in primary),
            "candidate_net_pnl_rupees_per_lot_15m": round(
                sum(_f(item.get("candidate_net_pnl")) for item in primary),
                2,
            ),
        },
        "recent_missed_setups": recent,
        "storage": get_db_storage_info(),
    }


def missed_trade_health() -> Dict[str, Any]:
    ensure_missed_trade_schema()
    conn = get_db()
    try:
        counts = conn.execute(
            """SELECT COUNT(*) total,
            SUM(CASE WHEN status='PENDING_CONTRACT' THEN 1 ELSE 0 END) pending_contract,
            SUM(CASE WHEN status='TRACKING' THEN 1 ELSE 0 END) tracking,
            SUM(CASE WHEN status='COMPLETE' THEN 1 ELSE 0 END) complete
            FROM ai_missed_trade_signals_v1"""
        ).fetchone()
        outcomes = conn.execute(
            """SELECT COUNT(*) total,
            SUM(CASE WHEN training_eligible=1 THEN 1 ELSE 0 END) trainable
            FROM ai_advanced_v2_contract_outcomes
            WHERE COALESCE(sample_source,?)=?""",
            (SAMPLE_SOURCE, SAMPLE_SOURCE),
        ).fetchone()
    finally:
        conn.close()
    due_outcome_backlog = len(_due_tracking_events(_now(), limit=200))
    return {
        "success": True,
        "version": VERSION,
        "started": bool(_started),
        "thread_alive": bool(_thread and _thread.is_alive()),
        "last_cycle_at": _last_cycle_at,
        "last_capture_at": _last_capture_at,
        "last_error": _last_error,
        "captured_total": _i(counts["total"] if counts else 0),
        "pending_contract": _i(counts["pending_contract"] if counts else 0),
        "tracking": _i(counts["tracking"] if counts else 0),
        "complete": _i(counts["complete"] if counts else 0),
        "outcome_count": _i(outcomes["total"] if outcomes else 0),
        "trainable_outcome_count": _i(outcomes["trainable"] if outcomes else 0),
        "due_outcome_backlog": due_outcome_backlog,
        "storage": get_db_storage_info(),
        "location": "RAILWAY",
        "mode": "SHADOW_ONLY",
        "trade_blocking": False,
        "order_execution": False,
    }


def apply_missed_trade_learning_patch() -> None:
    if getattr(runtime, "_okai_missed_trade_learning_v1", False):
        start_missed_trade_learning()
        return
    original_state_update = runtime._state_update

    def state_update_with_missed_learning(state, scans, selected, settings, rows):
        original_state_update(state, scans, selected, settings, rows)
        try:
            captured = capture_scan_misses(state, scans, rows)
            if captured:
                state["missed_trade_learning"] = {
                    "captured": captured,
                    "mode": "SHADOW_ONLY",
                    "trade_blocking": False,
                    "order_execution": False,
                }
        except Exception as exc:
            state["missed_trade_learning_error"] = f"{type(exc).__name__}:{str(exc)[:180]}"

    runtime._state_update = state_update_with_missed_learning
    runtime._okai_missed_trade_learning_v1 = True
    start_missed_trade_learning()

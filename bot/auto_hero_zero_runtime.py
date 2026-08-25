"""Automatic same-day-expiry Hero Zero PAPER execution.

This is deliberately separate from normal AUTO Portfolio entry sizing.  It can
open at most one one-lot PAPER observation per user/day during 14:30-15:00 IST,
only from a high-confidence completed-candle setup and an exact contract that
expires today.  It never sends a broker BUY order.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from bot import angel_fetcher
from bot import auto_portfolio_runtime as runtime
from bot import option_chain
from bot import expiry_day_risk_mode_patch as expiry_risk


VERSION = "OKAI-AUTO-HERO-ZERO-PAPER-V1"
MIN_SCORE = 90
WINDOW_START_MINUTE = 14 * 60 + 30
WINDOW_END_MINUTE = 15 * 60
FORCE_EXIT_MINUTE = 15 * 60 + 15
MAX_PREMIUM_CAPITAL = 2000.0
SL_PREMIUM_PERCENT = 50.0
TARGET_MULTIPLE = 2.0
MIN_OPTION_PREMIUM = 2.0
OTM_OFFSETS = (1, 2, 3, 4, 5, 6, 8, 10)
IST = timezone(timedelta(hours=5, minutes=30))


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except Exception:
        return float(default)


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_expiry(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip().upper()
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    return None


def _window_open(value: datetime | None = None) -> bool:
    now = value or _now_ist()
    minute = now.hour * 60 + now.minute
    return bool(
        now.weekday() < 5
        and WINDOW_START_MINUTE <= minute < WINDOW_END_MINUTE
    )


def _candidate_side(scan: dict[str, Any]) -> str:
    signal = dict(scan.get("signal_data") or {})
    side = str(
        signal.get("candidate_signal") or signal.get("signal") or "WAIT"
    ).upper()
    return side if side in {"CE", "PE"} else "WAIT"


def _momentum_matches(side: str, market: dict[str, Any]) -> bool:
    first = bool(market.get("c1_bullish", False))
    second = bool(market.get("c2_bullish", False))
    if side == "CE":
        return first and second
    if side == "PE":
        return not first and not second
    return False


def _real_5m_matches(side: str, scan: dict[str, Any]) -> bool:
    signal = dict(scan.get("signal_data") or {})
    snapshot = signal.get("real_mtf_5m") or scan.get("real_mtf_5m") or {}
    return bool(
        isinstance(snapshot, dict)
        and snapshot.get("available")
        and str(snapshot.get("side") or "WAIT").upper() == side
    )


def _eligible_candidate(scan: Any) -> bool:
    if not isinstance(scan, dict) or str(scan.get("status") or "").upper() != "OK":
        return False
    signal = dict(scan.get("signal_data") or {})
    market = dict(scan.get("market_data") or {})
    side = _candidate_side(scan)
    return bool(
        side in {"CE", "PE"}
        and _i(signal.get("score"), 0) >= MIN_SCORE
        and _momentum_matches(side, market)
        and _real_5m_matches(side, scan)
    )


def _best_candidate(scans: Any) -> dict[str, Any] | None:
    eligible = [scan for scan in (scans or []) if _eligible_candidate(scan)]
    eligible.sort(
        key=lambda scan: (
            _i((scan.get("signal_data") or {}).get("score"), 0),
            _f((scan.get("market_data") or {}).get("adx"), 0.0),
        ),
        reverse=True,
    )
    return eligible[0] if eligible else None


def _is_exact_today(resolved: Any, today: date) -> bool:
    if not isinstance(resolved, dict):
        return False
    return _parse_expiry(
        resolved.get("expiry_date") or resolved.get("expiry")
    ) == today


def _atm_strike(underlying: str, spot: float) -> int:
    step = int(option_chain.STRIKE_STEP.get(underlying, 50))
    return int(round(_f(spot) / step) * step)


def _otm_strike(underlying: str, spot: float, side: str, offset: int) -> int:
    step = int(option_chain.STRIKE_STEP.get(underlying, 50))
    direction = 1 if side == "CE" else -1
    return _atm_strike(underlying, spot) + direction * step * int(offset)


def _pick_affordable_contract(
    underlying: str,
    side: str,
    spot: float,
    today: date,
    resolve: Callable[[int], Any],
    quote: Callable[[dict[str, Any]], Any],
    fallback_lot_size: int,
) -> dict[str, Any] | None:
    for offset in OTM_OFFSETS:
        strike = _otm_strike(underlying, spot, side, offset)
        resolved = resolve(strike)
        if not _is_exact_today(resolved, today):
            continue
        resolved = dict(resolved)
        quoted = quote(resolved)
        if not isinstance(quoted, dict) or not quoted.get("success"):
            continue
        premium = _f(quoted.get("ltp"), 0.0)
        lot_size = max(
            1,
            _i(resolved.get("lot_size"), fallback_lot_size),
        )
        premium_capital = premium * lot_size
        if (
            premium + 1e-9 >= MIN_OPTION_PREMIUM
            and premium_capital <= MAX_PREMIUM_CAPITAL + 1e-9
        ):
            resolved.update({
                "quote_price": round(premium, 2),
                "lot_size": lot_size,
                "hero_otm_offset": int(offset),
                "hero_premium_capital": round(premium_capital, 2),
            })
            return resolved
    return None


def _today_hero_count(conn, user_id: int, now_ist: datetime) -> int:
    _ensure_schema(conn)
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM auto_hero_zero_daily_v1
        WHERE user_id=? AND trade_day=?
        """,
        (int(user_id), now_ist.date().isoformat()),
    ).fetchone()
    return _i(row["total"] if row is not None else 0, 0)


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_hero_zero_daily_v1 (
            user_id INTEGER NOT NULL,
            trade_day TEXT NOT NULL,
            trade_id INTEGER NOT NULL,
            underlying TEXT NOT NULL,
            side TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, trade_day)
        )
        """
    )
    conn.commit()


def _expiry_risk_permission(
    conn,
    user_id: int,
    settings: dict[str, Any],
    now_ist: datetime,
) -> tuple[bool, str, dict[str, Any]]:
    rows = expiry_risk._today_expiry_rows(conn, user_id, now_ist)
    if expiry_risk._expiry_trade_count_limit_reached(len(rows)):
        return False, "EXPIRY_MAX_TRADES_PER_DAY_REACHED", {
            "today_expiry_trades": len(rows),
        }
    capital = max(0.0, runtime._paper_base(conn, user_id, settings))
    realized = sum(
        expiry_risk._net(row)
        for row in rows
        if str(runtime._v(row, "status", "")).upper() == "CLOSED"
    )
    loss_limit = capital * expiry_risk.EXPIRY_DAILY_LOSS_LIMIT_PERCENT / 100.0
    if capital <= 0:
        return False, "HERO_ZERO_CAPITAL_UNAVAILABLE", {"capital": capital}
    if realized <= -loss_limit + 1e-9:
        return False, "EXPIRY_DAILY_LOSS_LIMIT_REACHED", {
            "realized_net_pnl": round(realized, 2),
            "loss_limit": round(loss_limit, 2),
        }
    return True, "EXPIRY_RISK_PERMISSION_OK", {
        "capital": round(capital, 2),
        "realized_net_pnl": round(realized, 2),
        "loss_limit": round(loss_limit, 2),
    }


def _state(
    state: dict[str, Any],
    status: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> None:
    state["auto_hero_zero"] = {
        "enabled": True,
        "paper_only": True,
        "status": status,
        "reason": reason,
        "window_ist": "14:30-15:00",
        "force_exit_ist": "15:15",
        "minimum_score": MIN_SCORE,
        "max_attempts_per_day": 1,
        "max_premium_capital": MAX_PREMIUM_CAPITAL,
        "version": VERSION,
        "details": details or {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _insert_trade(
    conn,
    user_id: int,
    broker_name: str,
    scan: dict[str, Any],
    contract: dict[str, Any],
    capital: float,
) -> int:
    _ensure_schema(conn)
    signal = dict(scan.get("signal_data") or {})
    underlying = str(scan.get("underlying") or "NIFTY").upper()
    side = _candidate_side(scan)
    entry = _f(contract.get("quote_price"), 0.0)
    lot_size = max(1, _i(contract.get("lot_size"), 1))
    sl_price = round(max(0.05, entry * (1.0 - SL_PREMIUM_PERCENT / 100.0)), 2)
    target_price = round(entry * TARGET_MULTIPLE, 2)
    risk = round(entry - sl_price, 2)
    reason = (
        "AUTO HERO ZERO PAPER"
        f" | score={_i(signal.get('score'), 0)}"
        " | TWO_CANDLE_MOMENTUM"
        " | REAL_5M_CONFIRMED"
        f" | OTM={_i(contract.get('hero_otm_offset'), 0)}"
        f" | MAX_CAPITAL={MAX_PREMIUM_CAPITAL:.0f}"
        f" | SL={SL_PREMIUM_PERCENT:.0f}PCT"
        f" | TARGET={TARGET_MULTIPLE:.0f}X"
        " | FORCE_EXIT_1515"
    )
    cur = conn.execute(
        """
        INSERT INTO paper_trades (
            user_id, symbol, side, entry_price, qty, pnl, status,
            reason, sl_price, target_price, token, exch_seg,
            expiry, strike, created_at, initial_risk,
            peak_price, trail_stage, trail_updates, last_ltp,
            broker_name, underlying, trading_mode, capital_slot,
            allocation_pct, capital_base, lot_size, lots,
            capital_used, live_order_status
        )
        VALUES (
            ?,?,?,?,?,0,'OPEN',?,?,?,?,?,?,?,?,?,?,
            'HERO_ZERO_INITIAL_50PCT_SL',0,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            int(user_id),
            contract.get("symbol"),
            side,
            entry,
            lot_size,
            reason,
            sl_price,
            target_price,
            contract.get("token"),
            contract.get("exchange") or contract.get("exch_seg"),
            contract.get("expiry_date") or contract.get("expiry"),
            contract.get("strike"),
            datetime.now(timezone.utc).isoformat(),
            risk,
            entry,
            entry,
            str(broker_name or "unknown").lower(),
            underlying,
            "paper",
            1,
            round(entry * lot_size / max(0.01, capital) * 100.0, 2),
            capital,
            lot_size,
            1,
            round(entry * lot_size, 2),
            "HERO_ZERO_PAPER_OPEN",
        ),
    )
    trade_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO auto_hero_zero_daily_v1 (
            user_id, trade_day, trade_id, underlying, side, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            _now_ist().date().isoformat(),
            trade_id,
            underlying,
            side,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return trade_id


def _precheck(
    conn,
    user_id: int,
    scans: Any,
    settings: dict[str, Any],
    state: dict[str, Any],
) -> tuple[dict[str, Any] | None, datetime, float]:
    now_ist = _now_ist()
    if not _window_open(now_ist):
        _state(state, "WAIT", "OUTSIDE_HERO_WINDOW")
        return None, now_ist, 0.0
    if runtime._open_rows(conn, user_id):
        _state(state, "BLOCKED", "OPEN_POSITION_EXISTS")
        return None, now_ist, 0.0
    if _today_hero_count(conn, user_id, now_ist) >= 1:
        _state(state, "BLOCKED", "HERO_ZERO_DAILY_ATTEMPT_USED")
        return None, now_ist, 0.0
    allowed, reason, risk = _expiry_risk_permission(
        conn, user_id, settings, now_ist
    )
    if not allowed:
        _state(state, "BLOCKED", reason, risk)
        return None, now_ist, 0.0
    selected = _best_candidate(scans)
    if selected is None:
        _state(state, "WAIT", "NO_SCORE_90_MOMENTUM_5M_SETUP")
        return None, now_ist, 0.0
    return selected, now_ist, _f(risk.get("capital"), 0.0)


def _finish_open(
    conn,
    user_id: int,
    broker_name: str,
    selected: dict[str, Any],
    contract: dict[str, Any] | None,
    capital: float,
    state: dict[str, Any],
) -> bool:
    if contract is None:
        _state(state, "BLOCKED", "NO_AFFORDABLE_EXACT_EXPIRY_OTM_CONTRACT")
        return False
    trade_id = _insert_trade(
        conn, user_id, broker_name, selected, contract, capital
    )
    details = {
        "trade_id": trade_id,
        "underlying": selected.get("underlying"),
        "side": _candidate_side(selected),
        "score": _i((selected.get("signal_data") or {}).get("score"), 0),
        "symbol": contract.get("symbol"),
        "entry_price": contract.get("quote_price"),
        "qty": contract.get("lot_size"),
        "capital_used": contract.get("hero_premium_capital"),
    }
    _state(state, "OPENED", "AUTO_HERO_ZERO_PAPER_OPENED", details)
    try:
        runtime.notify_user(
            user_id,
            "\n".join([
                "🚀 <b>Automatic Hero Zero PAPER Started</b>",
                f"Index: {details['underlying']}",
                f"Side: {details['side']} | Score: {details['score']}",
                f"Symbol: {details['symbol']}",
                f"Qty: {details['qty']} | Entry: ₹{details['entry_price']:.2f}",
                f"Capital used: ₹{details['capital_used']:.2f}",
                "SL: 50% premium | Target: 2x | Force exit: 15:15 IST",
            ]),
        )
    except Exception:
        pass
    return True


def attempt_auto_hero_zero_angel(
    conn,
    user_id: int,
    obj,
    scans: Any,
    settings: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    selected, now_ist, capital = _precheck(
        conn, user_id, scans, settings, state
    )
    if selected is None:
        return False
    underlying = str(selected.get("underlying") or "NIFTY").upper()
    side = _candidate_side(selected)
    spot = _f((selected.get("market_data") or {}).get("price"), 0.0)
    lot = angel_fetcher.LOT_SIZES.get(underlying, 1)

    def resolve(strike):
        return option_chain.resolve_option_for_date(
            underlying, now_ist.date(), strike, side
        )

    def quote(contract):
        try:
            payload = obj.ltpData(
                contract.get("exch_seg") or contract.get("exchange"),
                contract.get("symbol"),
                contract.get("token"),
            )
            return {"success": True, "ltp": payload["data"]["ltp"]}
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    contract = _pick_affordable_contract(
        underlying, side, spot, now_ist.date(), resolve, quote, lot
    )
    return _finish_open(
        conn, user_id, "angelone", selected, contract, capital, state
    )


def attempt_auto_hero_zero_multi(
    conn,
    user_id: int,
    broker_name: str,
    obj,
    scans: Any,
    settings: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    selected, now_ist, capital = _precheck(
        conn, user_id, scans, settings, state
    )
    if selected is None:
        return False
    underlying = str(selected.get("underlying") or "NIFTY").upper()
    side = _candidate_side(selected)
    spot = _f((selected.get("market_data") or {}).get("price"), 0.0)
    lot = angel_fetcher.LOT_SIZES.get(underlying, 1)

    def resolve(strike):
        return obj.search_option(
            underlying, now_ist.date().isoformat(), strike, side
        )

    def quote(contract):
        try:
            if str(broker_name).lower() == "upstox":
                return obj.get_ltp(
                    contract.get("token") or contract.get("symbol"),
                    exchange=contract.get("exchange", "NSE_FO"),
                )
            return obj.get_ltp(
                contract.get("symbol"),
                exchange=contract.get("exchange", "NFO"),
            )
        except Exception as exc:
            return {"success": False, "message": str(exc)}

    contract = _pick_affordable_contract(
        underlying, side, spot, now_ist.date(), resolve, quote, lot
    )
    return _finish_open(
        conn, user_id, broker_name, selected, contract, capital, state
    )


def is_auto_hero_zero_trade(trade: Any) -> bool:
    return str(runtime._v(trade, "reason", "") or "").startswith(
        "AUTO HERO ZERO PAPER"
    )


def auto_hero_zero_exit_reason(
    trade: Any,
    ltp: float,
    value: datetime | None = None,
) -> str | None:
    if not is_auto_hero_zero_trade(trade):
        return None
    entry = max(0.0, _f(runtime._v(trade, "entry_price", 0), 0))
    if entry > 0 and _f(ltp, 0) + 1e-9 >= entry * TARGET_MULTIPLE:
        return "AUTO HERO ZERO TARGET 2X HIT"
    now = value or _now_ist()
    if now.hour * 60 + now.minute >= FORCE_EXIT_MINUTE:
        return "AUTO HERO ZERO FORCE EXIT 15:15 IST"
    return None


__all__ = [
    "FORCE_EXIT_MINUTE",
    "MAX_PREMIUM_CAPITAL",
    "MIN_SCORE",
    "TARGET_MULTIPLE",
    "VERSION",
    "attempt_auto_hero_zero_angel",
    "attempt_auto_hero_zero_multi",
    "auto_hero_zero_exit_reason",
    "is_auto_hero_zero_trade",
]

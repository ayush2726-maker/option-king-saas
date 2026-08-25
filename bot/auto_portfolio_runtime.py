"""
AUTO Portfolio Runtime V1

- Scan NIFTY, BANKNIFTY and SENSEX.
- Pick the highest-quality eligible setup.
- Slot 1 uses 50% capital.
- Slot 2 uses 40% capital in a different index.
- 10% remains reserve.
- Maximum two concurrent positions.
- Same selector works for Paper and explicitly enabled Live mode.
"""

import math
import time
from datetime import date, datetime, timezone, timedelta

from database import get_db
from telegram.routes import notify_user

def _legacy():
    import bot.angel_fetcher as module
    return module



AUTO_ENGINE_MODE = "AUTO_PORTFOLIO_V1"
ALLOWED_INSTRUMENTS = ("NIFTY", "BANKNIFTY", "SENSEX")
SLOT_ALLOCATIONS = {1: 0.50, 2: 0.40, 3: 0.00}
RESERVE_ALLOCATION = 0.10
# One position per enabled index; removes the old hard two-position cap.
MAX_OPEN_POSITIONS = len(ALLOWED_INSTRUMENTS)
COMPLETE_STATUSES = {"complete", "completed", "filled", "success"}
REJECT_STATUSES = {"rejected", "cancelled", "canceled", "failed"}


def _now_ist():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _resolved_expires_today(resolved, value=None):
    """Use the resolved option contract date, never a global weekday guess."""
    raw = (resolved or {}).get("expiry_date") or (resolved or {}).get("expiry")
    if isinstance(raw, datetime):
        expiry = raw.date()
    elif isinstance(raw, date):
        expiry = raw
    else:
        expiry = None
        text = str(raw or "").strip().upper()
        for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%m-%Y"):
            try:
                expiry = datetime.strptime(text, fmt).date()
                break
            except Exception:
                pass
    current = value or _now_ist()
    return bool(expiry and expiry == current.date())


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _i(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def _v(row, key, default=None):
    try:
        if row is not None and key in row.keys() and row[key] is not None:
            return row[key]
    except Exception:
        pass
    return default


def _ensure_schema(conn):
    for name, kind in [
        ("underlying", "TEXT"),
        ("trading_mode", "TEXT DEFAULT 'paper'"),
        ("capital_slot", "INTEGER"),
        ("allocation_pct", "REAL"),
        ("capital_base", "REAL"),
        ("lot_size", "INTEGER"),
        ("lots", "INTEGER"),
        ("capital_used", "REAL"),
        ("entry_order_id", "TEXT"),
        ("exit_order_id", "TEXT"),
        ("live_order_status", "TEXT"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE paper_trades ADD COLUMN {name} {kind}"
            )
        except Exception:
            pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            trade_id INTEGER,
            order_id TEXT,
            action TEXT,
            status TEXT,
            message TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()


def _underlying(row):
    saved = str(_v(row, "underlying", "") or "").upper()
    if saved in ALLOWED_INSTRUMENTS:
        return saved

    symbol = str(_v(row, "symbol", "") or "").upper()
    for name in ("BANKNIFTY", "SENSEX", "NIFTY"):
        if name in symbol:
            return name
    return "NIFTY"


def _mode(row):
    return "live" if str(_v(row, "trading_mode", "paper")).lower() == "live" else "paper"


def _enabled(settings):
    raw = settings.get("enabled_instruments", list(ALLOWED_INSTRUMENTS))
    if isinstance(raw, str):
        raw = [x.strip().upper() for x in raw.split(",")]
    if not isinstance(raw, list):
        raw = list(ALLOWED_INSTRUMENTS)

    result = []
    for item in raw:
        name = str(item).upper()
        if name in ALLOWED_INSTRUMENTS and name not in result:
            result.append(name)
    return result or ["NIFTY"]


def _open_rows(conn, user_id):
    _ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT *
        FROM paper_trades
        WHERE user_id=? AND status='OPEN'
        ORDER BY id ASC
        """,
        (user_id,),
    ).fetchall()

    used = {
        _i(_v(row, "capital_slot", 0))
        for row in rows
        if _i(_v(row, "capital_slot", 0)) in tuple(range(1, MAX_OPEN_POSITIONS + 1))
    }
    free = [slot for slot in range(1, MAX_OPEN_POSITIONS + 1) if slot not in used]

    for row in rows:
        if _i(_v(row, "capital_slot", 0)) in tuple(range(1, MAX_OPEN_POSITIONS + 1)):
            continue
        if not free:
            break
        slot = free.pop(0)
        conn.execute(
            """
            UPDATE paper_trades
            SET capital_slot=?,
                allocation_pct=?,
                underlying=COALESCE(underlying, ?),
                trading_mode=COALESCE(trading_mode, 'paper')
            WHERE id=?
            """,
            (slot, SLOT_ALLOCATIONS[slot] * 100, _underlying(row), row["id"]),
        )
    conn.commit()

    return conn.execute(
        """
        SELECT *
        FROM paper_trades
        WHERE user_id=? AND status='OPEN'
        ORDER BY capital_slot ASC, id ASC
        """,
        (user_id,),
    ).fetchall()


def _free_slot(rows):
    used = {_i(_v(row, "capital_slot", 0)) for row in rows}
    for slot in range(1, MAX_OPEN_POSITIONS + 1):
        if slot not in used:
            return slot
    return None


def _today_count(conn, user_id):
    start = _now_ist().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM paper_trades
        WHERE user_id=?
          AND datetime(created_at) >= datetime(?)
        """,
        (user_id, start.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return _i(row["c"] if row else 0)


def _paper_base(conn, user_id, settings):
    start = max(1000.0, _f(settings.get("paper_capital", 100000), 100000))
    row = conn.execute(
        """
        SELECT COALESCE(SUM(pnl), 0) AS p
        FROM paper_trades
        WHERE user_id=?
          AND status='CLOSED'
          AND COALESCE(trading_mode, 'paper')='paper'
        """,
        (user_id,),
    ).fetchone()
    return max(1000.0, start + _f(row["p"] if row else 0))


def _live_base_from_rows(rows):
    values = [
        _f(_v(row, "capital_base", 0))
        for row in rows
        if _mode(row) == "live" and _f(_v(row, "capital_base", 0)) > 0
    ]
    return max(values) if values else 0.0


def _angel_cash(obj):
    try:
        payload = obj.rmsLimit()
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        for key in ("availablecash", "availableCash", "net"):
            value = _f(data.get(key), 0)
            if value > 0:
                return value
    except Exception:
        pass
    return 0.0


def _multi_cash(obj):
    try:
        result = obj.get_funds()
        if result.get("success"):
            return max(0.0, _f(result.get("available_cash"), 0))
    except Exception:
        pass
    return 0.0


def _row_capital_used(row):
    saved = _f(_v(row, "capital_used", 0), 0)
    if saved > 0:
        return saved
    return max(
        0.0,
        _f(_v(row, "entry_price", 0), 0)
        * max(0, _i(_v(row, "qty", 0), 0)),
    )


def _size(capital_base, slot, premium, lot_size, rows=None, risk_points=None):
    """Lot-aware sizing with a hard 10% reserve.

    Slot 1/2 percentages are target allocations, not a reason to reject an
    otherwise affordable complete exchange lot. When one lot is above the
    target slot budget but still fits after preserving the reserve and existing
    positions, allow exactly that lot. Never create fractional lots.
    """
    lot_size = max(1, _i(lot_size, 1))
    premium = max(0.0, _f(premium, 0))
    capital_base = max(0.0, _f(capital_base, 0))
    target_budget = max(0.0, capital_base * SLOT_ALLOCATIONS.get(slot, 0))
    reserve_floor = max(0.0, capital_base * RESERVE_ALLOCATION)
    committed = sum(_row_capital_used(row) for row in (rows or []))
    available_after_reserve = max(0.0, capital_base - reserve_floor - committed)
    one_lot = premium * lot_size

    flex_used = bool(
        one_lot > target_budget + 1e-9
        and one_lot <= available_after_reserve + 1e-9
    )
    if flex_used:
        # Permit the minimum complete lot only; do not consume the whole
        # remaining portfolio merely because the target slot was too small.
        budget = one_lot
        sizing_mode = "LOT_AWARE_FLEX_ONE_LOT"
    else:
        budget = min(target_budget, available_after_reserve)
        sizing_mode = "TARGET_SLOT_BUDGET"

    lots = int(math.floor((budget + 1e-9) / one_lot)) if one_lot > 0 else 0
    qty = lots * lot_size
    capital_used = premium * qty
    actual_pct = (
        capital_used / capital_base * 100.0
        if capital_base > 0
        else 0.0
    )
    return {
        "lot_size": lot_size,
        "lots": lots,
        "qty": qty,
        "target_slot_budget": round(target_budget, 2),
        "slot_budget": round(budget, 2),
        "reserve_floor": round(reserve_floor, 2),
        "committed_capital": round(committed, 2),
        "available_after_reserve": round(available_after_reserve, 2),
        "one_lot_cost": round(one_lot, 2),
        "capital_used": round(capital_used, 2),
        "actual_allocation_pct": round(actual_pct, 2),
        "flex_used": flex_used,
        "sizing_mode": sizing_mode,
    }


def _build_scan(user_id, underlying, df, profile, loss_streak):
    if df is None or len(df) < 28:
        return {"underlying": underlying, "status": "WAITING_CANDLES"}

    result = _legacy().calculate_indicators(df)
    if result is None:
        return {"underlying": underlying, "status": "INDICATOR_WAIT"}

    df, trend = result
    last = df.iloc[-2]
    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    orb_high, orb_low = _legacy().calculate_orb_levels(df)

    market = {
        "price": float(last["close"]),
        "vwap": float(last["VWAP"]),
        "ema9": float(last["EMA9"]),
        "ema21": float(last["EMA21"]),
        "adx": float(last["ADX"]),
        "volume_ratio": float(last["VOL_RATIO"]),
        "vwap_fallback_used": bool(last["VWAP_FALLBACK_USED"]),
        "supertrend_dir": str(last["ST_DIR"]),
        "trend": trend,
        "mtf_confirmed": trend != "SIDEWAYS",
        "c1_bullish": float(c1["close"]) > float(c1["open"]),
        "c2_bullish": float(c2["close"]) > float(c2["open"]),
        "gap_day": False,
        "orb_high": orb_high,
        "orb_low": orb_low,
        "atr": float(last["ATR"]),
    }

    signal = _legacy().get_full_signal(
        market,
        consecutive_losses=loss_streak,
        profile=profile,
    )
    signal.setdefault(
        "strategy_profile_key",
        profile.get("profile_key", "okai_default_82"),
    )
    signal.setdefault(
        "strategy_profile_name",
        profile.get("profile_name", "OKAI Default 82"),
    )

    market["signal"] = signal.get("signal", "WAIT")
    market["signal_score"] = signal.get("score", 0)
    market["signal_min_score"] = signal.get("min_score", 82)

    return {
        "underlying": underlying,
        "status": "OK",
        "market_data": market,
        "signal_data": signal,
        "chart_candles": _legacy().build_chart_candles(df, limit=390),
        "candle_id": str(last["time"]),
    }


def _scan_angel(user_id, obj, settings, profile, streak):
    scans = []
    for underlying in _enabled(settings):
        try:
            df = _legacy().get_candles(
                obj,
                _legacy().INDEX_TOKENS[underlying],
                exchange=_legacy().INDEX_EXCHANGE[underlying],
            )
            scans.append(_build_scan(user_id, underlying, df, profile, streak))
        except Exception as exc:
            scans.append(
                {
                    "underlying": underlying,
                    "status": "ERROR",
                    "error": str(exc)[:160],
                }
            )
    return scans


def _scan_multi(user_id, broker_name, obj, settings, profile, streak):
    scans = []
    for underlying in _enabled(settings):
        try:
            df = _legacy().get_candles_multi(broker_name, obj, underlying)
            scans.append(_build_scan(user_id, underlying, df, profile, streak))
        except Exception as exc:
            scans.append(
                {
                    "underlying": underlying,
                    "status": "ERROR",
                    "error": str(exc)[:160],
                }
            )
    return scans


def _summary(scan):
    signal = scan.get("signal_data", {})
    market = scan.get("market_data", {})
    return {
        "underlying": scan.get("underlying"),
        "status": scan.get("status"),
        "signal": signal.get("signal", "WAIT"),
        "candidate_signal": signal.get("candidate_signal", "WAIT"),
        "score": _i(signal.get("score", 0)),
        "min_score": _i(signal.get("min_score", 82), 82),
        "trade_allowed": bool(signal.get("trade_allowed", False)),
        "price": _f(market.get("price", 0)),
        "adx": _f(market.get("adx", 0)),
        "volume_ratio": _f(market.get("volume_ratio", 0)),
        "warnings": list(signal.get("warnings", []) or [])[:5],
        "error": scan.get("error"),
    }


def _eligible_candidates(scans, blocked_underlyings):
    """Return every qualified setup in execution priority order.

    A high-score candidate can still fail later at contract, LTP, premium
    momentum, option-quality or sizing gates. Do not let that one candidate
    starve another index that is also fully qualified in the same scan.
    """
    candidates = []
    for scan in scans:
        if scan.get("status") != "OK":
            continue
        if scan.get("underlying") in blocked_underlyings:
            continue

        signal = scan.get("signal_data", {})
        if signal.get("signal") not in ("CE", "PE"):
            continue
        if not signal.get("trade_allowed", False):
            continue
        candidates.append(scan)

    candidates.sort(
        key=lambda scan: (
            _i(scan["signal_data"].get("score", 0)),
            _i(scan["signal_data"].get("base_score", 0)),
            _f(scan["market_data"].get("adx", 0)),
            _f(scan["market_data"].get("volume_ratio", 0)),
        ),
        reverse=True,
    )
    return candidates


def _best_candidate(scans, blocked_underlyings):
    candidates = _eligible_candidates(scans, blocked_underlyings)
    return candidates[0] if candidates else None


def _attempt_entry_candidates(candidates, opener, state):
    """Attempt qualified candidates in order and return the one that opened."""
    attempts = []
    state["entry_candidate_attempts"] = attempts

    for candidate in candidates:
        state.pop("last_entry_attempt", None)
        state.pop("entry_attempt", None)
        state.pop("entry_block_reason", None)
        state.pop("last_entry_block_reason", None)

        opened = bool(opener(candidate))
        guard = dict(
            state.get("last_entry_attempt")
            or state.get("entry_attempt")
            or state.get("entry_guard")
            or {}
        )
        signal = dict(candidate.get("signal_data") or {})
        attempts.append(
            {
                "underlying": candidate.get("underlying"),
                "side": signal.get("signal"),
                "score": signal.get("score"),
                "opened": opened,
                "reason": guard.get("reason") or (
                    "ENTRY_OPENED" if opened else "ENTRY_NOT_OPENED"
                ),
                "stage": guard.get("stage"),
            }
        )

        if opened:
            return candidate

        if state.get("live_order_lock") or state.get("mode_change_blocked"):
            break

    return None


def _display_scan(scans, selected, settings):
    if selected is not None:
        return selected

    primary = str(settings.get("primary_instrument", "NIFTY")).upper()
    for scan in scans:
        if scan.get("status") == "OK" and scan.get("underlying") == primary:
            return scan

    valid = [scan for scan in scans if scan.get("status") == "OK"]
    valid.sort(
        key=lambda scan: _i(scan.get("signal_data", {}).get("score", 0)),
        reverse=True,
    )
    return valid[0] if valid else None


def _scan_map(scans):
    return {
        scan["underlying"]: scan
        for scan in scans
        if scan.get("status") == "OK"
    }


def _ltp_angel(obj, trade):
    try:
        quote = obj.ltpData(
            trade["exch_seg"], trade["symbol"], trade["token"]
        )
        return {"success": True, "ltp": float(quote["data"]["ltp"])}
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def _ltp_multi(broker_name, obj, trade):
    try:
        if broker_name == "upstox":
            ref = _v(trade, "token", trade["symbol"]) or trade["symbol"]
            return obj.get_ltp(
                ref,
                exchange=_v(trade, "exch_seg", "NSE_FO"),
            )
        return obj.get_ltp(
            trade["symbol"],
            exchange=_v(trade, "exch_seg", "NFO"),
        )
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def _record_event(conn, user_id, trade_id, order_id, action, status, message):
    conn.execute(
        """
        INSERT INTO live_order_events (
            user_id, trade_id, order_id, action,
            status, message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            trade_id,
            str(order_id or ""),
            action,
            status,
            str(message or "")[:500],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def _angel_status(obj, order_id):
    try:
        payload = obj.orderBook()
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        for row in rows or []:
            if str(row.get("orderid") or row.get("order_id") or "") != str(order_id):
                continue
            return {
                "success": True,
                "status": str(row.get("status") or "").lower(),
                "filled_qty": _i(
                    row.get("filledshares") or row.get("filled_quantity") or 0
                ),
                "avg_price": _f(
                    row.get("averageprice") or row.get("average_price") or 0
                ),
            }
    except Exception as exc:
        return {"success": False, "message": str(exc)}
    return {"success": False, "message": "Order not found"}


def _wait_fill(fetch_status, order_id, fallback):
    last = {}
    for _ in range(10):
        result = fetch_status(order_id) or {}
        last = result

        if result.get("success"):
            status = str(result.get("status") or "").lower()
            if status in COMPLETE_STATUSES:
                avg = _f(result.get("avg_price"), 0)
                return {
                    "success": True,
                    "status": status,
                    "avg_price": avg if avg > 0 else fallback,
                    "filled_qty": _i(result.get("filled_qty"), 0),
                    "order_id": str(order_id),
                }
            if status in REJECT_STATUSES:
                return {
                    "success": False,
                    "status": status,
                    "message": "Broker rejected or cancelled the order",
                    "order_id": str(order_id),
                }
        time.sleep(1)

    return {
        "success": False,
        "status": str(last.get("status") or "PENDING"),
        "message": "Order fill not confirmed within 10 seconds",
        "order_id": str(order_id),
        "pending": True,
    }


def _place_angel(obj, resolved, action, qty, fallback):
    params = {
        "variety": "NORMAL",
        "tradingsymbol": resolved["symbol"],
        "symboltoken": str(resolved.get("token") or ""),
        "transactiontype": action,
        "exchange": resolved.get("exchange")
        or resolved.get("exch_seg")
        or "NFO",
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "price": "0",
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(qty),
    }
    try:
        response = obj.placeOrder(params)
        if isinstance(response, dict):
            order_id = (
                response.get("data", {}).get("orderid")
                or response.get("orderid")
                or response.get("order_id")
            )
        else:
            order_id = response

        if not order_id:
            return {"success": False, "message": "Angel order id missing"}
        return _wait_fill(
            lambda oid: _angel_status(obj, oid),
            order_id,
            fallback,
        )
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def _place_multi(obj, resolved, action, qty, fallback):
    try:
        result = obj.place_order(
            resolved["symbol"],
            resolved.get("token"),
            action,
            qty,
            order_type="MARKET",
            price=0,
            exchange=resolved.get("exchange") or "NFO",
        )
        if not result.get("success"):
            return result

        order_id = result.get("order_id")
        if not order_id:
            return {"success": False, "message": "Broker order id missing"}
        return _wait_fill(obj.get_order_status, order_id, fallback)
    except Exception as exc:
        return {"success": False, "message": str(exc)}


def _evaluate_exit(trade, ltp, market_data, candle_id):
    entry = _f(trade["entry_price"])
    old_sl = _f(_v(trade, "sl_price", max(0.05, entry - 0.05)))
    risk = _f(_v(trade, "initial_risk", max(0.05, entry - old_sl)))
    peak = _f(_v(trade, "peak_price", entry))
    updates = _i(_v(trade, "trail_updates", 0))

    trail = _legacy()._dynamic_profit_lock(entry, risk, old_sl, peak, ltp)
    if trail["updated"]:
        updates += 1

    reversal = _legacy()._dynamic_reversal_state(
        trade, market_data, candle_id
    )
    eod = _now_ist().hour * 60 + _now_ist().minute >= 15 * 60 + 25
    hit = ltp <= trail["sl_price"]

    if hit and trail["sl_price"] >= entry:
        reason = (
            "PROFIT LOCK TRAIL HIT"
            f" | {trail['stage']} | locked={trail['locked_r']}R"
        )
    elif hit:
        reason = "PURE ATR SL HIT"
    elif reversal["exit"]:
        reason = (
            "TWO CANDLE STRUCTURAL REVERSAL EXIT"
            f" | count={reversal['count']}"
        )
    elif eod:
        reason = "EOD EXIT 15:25 IST"
    else:
        reason = None

    return {
        "trail": trail,
        "risk": risk,
        "updates": updates,
        "reversal": reversal,
        "reason": reason,
    }


def _update_open(conn, trade, ltp, evaluation):
    trail = evaluation["trail"]
    reversal = evaluation["reversal"]
    conn.execute(
        """
        UPDATE paper_trades
        SET sl_price=?,
            target_price=CASE
                WHEN reason LIKE 'AUTO HERO ZERO PAPER%' THEN target_price
                ELSE NULL
            END,
            initial_risk=?,
            peak_price=?,
            trail_stage=?,
            trail_updates=?,
            last_ltp=?,
            reversal_count=?,
            reversal_last_candle=?
        WHERE id=?
        """,
        (
            trail["sl_price"],
            evaluation["risk"],
            trail["peak_price"],
            trail["stage"],
            evaluation["updates"],
            ltp,
            reversal["count"],
            reversal["last_candle"],
            trade["id"],
        ),
    )
    conn.commit()


def _close(conn, user_id, trade, price, reason, order_id=None):
    qty = max(1, _i(trade["qty"], 1))
    pnl = round((price - _f(trade["entry_price"])) * qty, 2)

    conn.execute(
        """
        UPDATE paper_trades
        SET exit_price=?,
            pnl=?,
            status='CLOSED',
            reason=?,
            last_ltp=?,
            exit_order_id=?,
            live_order_status=?
        WHERE id=?
        """,
        (
            price,
            pnl,
            reason,
            price,
            order_id,
            "EXIT_FILLED" if _mode(trade) == "live" else "PAPER_CLOSED",
            trade["id"],
        ),
    )
    conn.commit()

    try:
        notify_user(
            user_id,
            "\n".join(
                [
                    "📤 <b>Portfolio Exit</b>",
                    f"Mode: {_mode(trade).upper()}",
                    f"Index: {_underlying(trade)}",
                    f"Symbol: {trade['symbol']}",
                    f"Qty: {qty}",
                    f"Exit: ₹{price:.2f}",
                    f"P&L: ₹{pnl:.2f}",
                    f"Reason: {reason}",
                ]
            ),
        )
    except Exception:
        pass


def _manage_rows(
    conn,
    user_id,
    rows,
    scans,
    quote_fetcher,
    live_order,
    state,
):
    scan_lookup = _scan_map(scans)
    quote_failures = []
    quote_successes = 0

    for trade in rows:
        quote = quote_fetcher(trade)
        if not quote.get("success"):
            message = str(quote.get("message") or "OPTION_LTP_FAILED")[:400]
            quote_failures.append({"trade_id": trade["id"], "message": message})
            try:
                conn.execute(
                    """
                    UPDATE paper_trades
                    SET quote_failed_at=?, quote_error=?,
                        quote_failure_count=COALESCE(quote_failure_count, 0) + 1
                    WHERE id=? AND status='OPEN'
                    """,
                    (datetime.now(timezone.utc).isoformat(), message, trade["id"]),
                )
                conn.commit()
            except Exception:
                pass
            continue

        ltp = _f(quote.get("ltp"), 0)
        if ltp <= 0:
            message = "INVALID_OPTION_LTP"
            quote_failures.append({"trade_id": trade["id"], "message": message})
            try:
                conn.execute(
                    """
                    UPDATE paper_trades
                    SET quote_failed_at=?, quote_error=?,
                        quote_failure_count=COALESCE(quote_failure_count, 0) + 1
                    WHERE id=? AND status='OPEN'
                    """,
                    (datetime.now(timezone.utc).isoformat(), message, trade["id"]),
                )
                conn.commit()
            except Exception:
                pass
            continue
        quote_successes += 1

        scan = scan_lookup.get(_underlying(trade))
        market = scan.get("market_data") if scan else None
        candle_id = scan.get("candle_id") if scan else None
        evaluation = _evaluate_exit(trade, ltp, market, candle_id)
        _update_open(conn, trade, ltp, evaluation)

        if not evaluation["reason"]:
            continue

        if _mode(trade) == "paper":
            _close(conn, user_id, trade, ltp, evaluation["reason"])
            continue

        resolved = {
            "symbol": trade["symbol"],
            "token": trade["token"],
            "exchange": trade["exch_seg"],
            "exch_seg": trade["exch_seg"],
        }
        order = live_order(
            resolved,
            "SELL",
            _i(trade["qty"], 0),
            ltp,
        )
        if order.get("success"):
            exit_price = _f(order.get("avg_price"), ltp)
            _close(
                conn,
                user_id,
                trade,
                exit_price,
                evaluation["reason"],
                order.get("order_id"),
            )
            _record_event(
                conn,
                user_id,
                trade["id"],
                order.get("order_id"),
                "SELL",
                "FILLED",
                evaluation["reason"],
            )
        else:
            status = "PENDING" if order.get("pending") else "FAILED"
            _record_event(
                conn,
                user_id,
                trade["id"],
                order.get("order_id"),
                "SELL",
                status,
                order.get("message"),
            )
            conn.execute(
                """
                UPDATE paper_trades
                SET exit_order_id=?,
                    live_order_status=?
                WHERE id=?
                """,
                (
                    order.get("order_id"),
                    f"EXIT_{status}",
                    trade["id"],
                ),
            )
            conn.commit()
            state["live_order_error"] = order.get("message") or "Live SELL failed"
            if order.get("pending"):
                state["live_order_lock"] = True

    if rows:
        if quote_successes:
            state["consecutive_open_quote_failures"] = 0
            state.pop("open_quote_error", None)
        elif quote_failures:
            state["consecutive_open_quote_failures"] = (
                _i(state.get("consecutive_open_quote_failures"), 0) + 1
            )
            state["open_quote_error"] = quote_failures[-1]["message"]

    return {
        "open_count": len(rows),
        "quote_successes": quote_successes,
        "quote_failures": quote_failures,
        "all_quotes_failed": bool(rows) and quote_successes == 0,
    }


def _insert(
    conn,
    user_id,
    broker_name,
    resolved,
    underlying,
    side,
    entry,
    sizing,
    score,
    spot,
    atr,
    mode,
    slot,
    capital_base,
    order_id=None,
):
    levels = _legacy()._dynamic_atr_levels(
        spot,
        entry,
        atr,
        is_expiry_day=_resolved_expires_today(resolved),
    )
    if not levels["atr_available"]:
        return None

    reason = (
        f"Real entry score {score}"
        f" | {levels['mode']} | R={levels['risk_points']}"
        " | DYNAMIC_PROFIT_LOCK"
        f" | SLOT_{slot}_{int(SLOT_ALLOCATIONS[slot]*100)}"
        f" | {AUTO_ENGINE_MODE}"
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
            capital_used, entry_order_id, live_order_status
        )
        VALUES (
            ?,?,?,?,?,0,'OPEN',?,?,NULL,?,?,?,?,?,?,?,
            'INITIAL_ATR',0,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            user_id,
            resolved["symbol"],
            side,
            entry,
            sizing["qty"],
            reason,
            levels["sl_price"],
            resolved.get("token"),
            resolved.get("exchange") or resolved.get("exch_seg"),
            resolved.get("expiry"),
            resolved.get("strike"),
            datetime.now(timezone.utc).isoformat(),
            levels["risk_points"],
            entry,
            entry,
            broker_name,
            underlying,
            mode,
            slot,
            sizing.get("actual_allocation_pct", SLOT_ALLOCATIONS[slot] * 100),
            capital_base,
            sizing["lot_size"],
            sizing["lots"],
            sizing["capital_used"],
            order_id,
            "ENTRY_FILLED" if mode == "live" else "PAPER_OPEN",
        ),
    )
    conn.commit()
    return cur.lastrowid


def _open_common(
    conn,
    user_id,
    broker_name,
    selected,
    settings,
    resolved,
    quote_price,
    quality,
    lot_size,
    live_order,
    live_cash,
    state,
):
    state["entry_guard"] = quality
    if not quality.get("allowed", True):
        return False

    state.pop("position_size_block", None)
    if not state.get("live_order_lock"):
        state.pop("live_order_error", None)

    rows = _open_rows(conn, user_id)
    slot = _free_slot(rows)
    if slot is None or len(rows) >= MAX_OPEN_POSITIONS:
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "MAX_OPEN_POSITIONS_REACHED",
            "POSITION_LIMIT",
            {"open_positions": len(rows)},
        )

    current_mode = (
        "live"
        if str(settings.get("trading_mode", "paper")).lower() == "live"
        else "paper"
    )
    modes = {_mode(row) for row in rows}
    if modes and current_mode not in modes:
        state["mode_change_blocked"] = (
            "Existing position close hone ke baad mode change apply hoga."
        )
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "TRADING_MODE_CHANGE_BLOCKED",
            "MODE_GUARD",
            {"existing_modes": sorted(modes), "requested_mode": current_mode},
        )
    # Do not let a mode-change message from an earlier scan stop candidate
    # fallback after the conflicting positions have been closed.
    state.pop("mode_change_blocked", None)

    if current_mode == "paper":
        capital_base = _paper_base(conn, user_id, settings)
    else:
        capital_base = _live_base_from_rows(rows) or live_cash()
        if capital_base <= 0:
            state["live_order_error"] = "Broker available funds read nahi hue."
            return _record_preopen_failure(
                state,
                broker_name,
                selected,
                "BROKER_FUNDS_UNAVAILABLE",
                "CAPITAL",
            )

    signal = selected["signal_data"]
    market = selected["market_data"]
    planned_levels = _legacy()._dynamic_atr_levels(
        market["price"],
        quote_price,
        market["atr"],
        is_expiry_day=_resolved_expires_today(resolved),
    )
    if not planned_levels.get("atr_available"):
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "ATR_LEVELS_OR_TRADE_INSERT_FAILED",
            "TRADE_INSERT",
            {"spot_atr": market.get("atr"), "option_ltp": quote_price},
        )

    sizing = _size(
        capital_base,
        slot,
        quote_price,
        lot_size,
        rows=rows,
        risk_points=planned_levels.get("risk_points"),
    )
    sizing["planned_risk_points"] = round(
        _f(planned_levels.get("risk_points"), 0.0),
        2,
    )
    sizing["same_day_expiry_contract"] = _resolved_expires_today(resolved)
    state["entry_sizing"] = dict(sizing)
    if sizing["lots"] < 1:
        risk_blocked = bool(
            sizing.get("risk_lots") == 0
            and sizing.get("affordability_lots", 0) >= 1
        )
        state["position_size_block"] = {
            "slot": slot,
            "slot_budget": sizing["slot_budget"],
            "target_slot_budget": sizing.get("target_slot_budget"),
            "available_after_reserve": sizing.get("available_after_reserve"),
            "reserve_floor": sizing.get("reserve_floor"),
            "committed_capital": sizing.get("committed_capital"),
            "one_lot_cost": sizing.get("one_lot_cost"),
            "capital_base": round(capital_base, 2),
            "reason": (
                "Ek complete lot ka planned SL loss 1% equity limit se zyada hai"
                if risk_blocked
                else "10% reserve bachane ke baad ek complete lot afford nahi hota"
            ),
            "risk_lots": sizing.get("risk_lots"),
            "planned_risk_per_lot": sizing.get("planned_risk_per_lot"),
            "max_planned_loss_amount": sizing.get("max_planned_loss_amount"),
        }
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "POSITION_SIZE_BLOCK",
            "CAPITAL",
            dict(state["position_size_block"]),
        )

    entry = quote_price
    entry_order_id = None

    if current_mode == "live":
        if state.get("live_order_lock"):
            return _record_preopen_failure(
                state,
                broker_name,
                selected,
                "LIVE_ORDER_PENDING_LOCK",
                "LIVE_ORDER",
            )
        order = live_order(resolved, "BUY", sizing["qty"], quote_price)
        if not order.get("success"):
            status = "PENDING" if order.get("pending") else "FAILED"
            _record_event(
                conn,
                user_id,
                None,
                order.get("order_id"),
                "BUY",
                status,
                order.get("message"),
            )
            state["live_order_error"] = order.get("message") or "Live BUY failed"
            if order.get("pending"):
                state["live_order_lock"] = True
                state["live_order_lock_source"] = "ENTRY_BUY_PENDING"
            return _record_preopen_failure(
                state,
                broker_name,
                selected,
                order.get("message") or "LIVE_BUY_FAILED",
                "LIVE_ORDER",
                {"status": status, "order_id": order.get("order_id")},
            )

        entry = _f(order.get("avg_price"), quote_price)
        entry_order_id = order.get("order_id")
        sizing["capital_used"] = round(
            entry * sizing["qty"],
            2,
        )

    trade_id = _insert(
        conn,
        user_id,
        broker_name,
        resolved,
        selected["underlying"],
        signal["signal"],
        entry,
        sizing,
        signal.get("score", 0),
        market["price"],
        market["atr"],
        current_mode,
        slot,
        capital_base,
        entry_order_id,
    )
    if not trade_id:
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "ATR_LEVELS_OR_TRADE_INSERT_FAILED",
            "TRADE_INSERT",
            {"spot_atr": market.get("atr"), "option_ltp": quote_price},
        )

    if current_mode == "live":
        _record_event(
            conn,
            user_id,
            trade_id,
            entry_order_id,
            "BUY",
            "FILLED",
            "Live entry filled",
        )

    state["last_opened_trade"] = {
        "trade_id": trade_id,
        "underlying": selected["underlying"],
        "side": signal["signal"],
        "slot": slot,
        "allocation_percent": sizing.get("actual_allocation_pct", int(SLOT_ALLOCATIONS[slot] * 100)),
        "sizing_mode": sizing.get("sizing_mode"),
        "flex_used": bool(sizing.get("flex_used")),
        "lots": sizing["lots"],
        "qty": sizing["qty"],
        "capital_used": sizing["capital_used"],
        "mode": current_mode,
    }

    try:
        notify_user(
            user_id,
            "\n".join(
                [
                    "📥 <b>Portfolio Entry</b>",
                    f"Mode: {current_mode.upper()}",
                    f"Index: {selected['underlying']}",
                    f"Side: {signal['signal']}",
                    f"Score: {signal.get('score')}",
                    f"Slot: {slot} ({int(SLOT_ALLOCATIONS[slot]*100)}%)",
                    f"Lots: {sizing['lots']}",
                    f"Qty: {sizing['qty']}",
                    f"Capital used: ₹{sizing['capital_used']:.2f}",
                ]
            ),
        )
    except Exception:
        pass

    return True


def _record_preopen_failure(
    state,
    broker_name,
    selected,
    reason,
    stage,
    details=None,
):
    signal = dict((selected or {}).get("signal_data") or {})
    attempt = {
        "allowed": False,
        "reason": str(reason or "ENTRY_NOT_OPENED")[:240],
        "stage": str(stage or "PRE_OPEN"),
        "broker": str(broker_name or "unknown").lower(),
        "underlying": (selected or {}).get("underlying"),
        "side": signal.get("signal"),
        "score": signal.get("score"),
        "details": details or {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    state["entry_guard"] = dict(attempt)
    state["entry_attempt"] = dict(attempt)
    state["last_entry_attempt"] = dict(attempt)
    state["entry_block_reason"] = attempt["reason"]
    state["last_entry_block_reason"] = attempt["reason"]
    return False


def _open_angel(conn, user_id, obj, selected, settings, state):
    underlying = selected["underlying"]
    signal = selected["signal_data"]
    market = selected["market_data"]

    resolved = _legacy().resolve_option(
        underlying,
        market["price"],
        signal["signal"],
    )
    if not resolved:
        return _record_preopen_failure(
            state,
            "angelone",
            selected,
            "OPTION_CONTRACT_NOT_RESOLVED",
            "OPTION_CONTRACT",
        )

    resolved = dict(resolved)
    resolved["exchange"] = resolved.get("exch_seg") or resolved.get("exchange")

    try:
        q = obj.ltpData(
            resolved["exch_seg"],
            resolved["symbol"],
            resolved["token"],
        )
        quote_price = float(q["data"]["ltp"])
    except Exception as exc:
        return _record_preopen_failure(
            state,
            "angelone",
            selected,
            "OPTION_LTP_FAILED",
            "OPTION_QUOTE",
            {"message": str(exc)[:180]},
        )

    quality = _legacy()._option_entry_quality_angel(
        obj, resolved, quote_price
    )

    return _open_common(
        conn,
        user_id,
        "angelone",
        selected,
        settings,
        resolved,
        quote_price,
        quality,
        _legacy().LOT_SIZES.get(underlying, 1),
        lambda r, a, q, p: _place_angel(obj, r, a, q, p),
        lambda: _angel_cash(obj),
        state,
    )


def _open_multi(conn, user_id, broker_name, obj, selected, settings, state):
    underlying = selected["underlying"]
    signal = selected["signal_data"]
    market = selected["market_data"]

    resolved = {}
    contract_errors = []
    for expiry_request in ("current_week", "nearest", None):
        resolved = obj.search_option(
            underlying,
            expiry_request,
            _legacy()._dynamic_atm_strike(underlying, market["price"]),
            signal["signal"],
        )
        if resolved.get("success"):
            break
        contract_errors.append(
            resolved.get("message")
            or resolved.get("reason")
            or "OPTION_CONTRACT_NOT_RESOLVED"
        )
        time.sleep(0.5)
    if not resolved.get("success"):
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            resolved.get("message") or "OPTION_CONTRACT_NOT_RESOLVED",
            "OPTION_CONTRACT",
            {
                "reason": resolved.get("reason"),
                "errors": (resolved.get("errors") or []) + contract_errors[-3:],
                "requested_expiry": "nearest/current_week",
                "retry_count": 3,
            },
        )

    quote = {}
    quote_errors = []
    for _quote_attempt in range(3):
        if broker_name == "upstox":
            quote = obj.get_ltp(
                resolved.get("token") or resolved["symbol"],
                exchange=resolved.get("exchange", "NSE_FO"),
            )
        else:
            quote = obj.get_ltp(
                resolved["symbol"],
                exchange=resolved.get("exchange", "NFO"),
            )
        if quote.get("success") and _f(quote.get("ltp"), 0) > 0:
            break
        quote_errors.append(quote.get("message") or "OPTION_LTP_FAILED")
        time.sleep(1)
    if not quote.get("success"):
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            quote.get("message") or "OPTION_LTP_FAILED",
            "OPTION_QUOTE",
            {
                "symbol": resolved.get("symbol"),
                "token": resolved.get("token"),
                "exchange": resolved.get("exchange"),
                "retry_count": 3,
                "errors": quote_errors[-3:],
            },
        )

    quote_price = _f(quote.get("ltp"), 0)
    if quote_price <= 0:
        return _record_preopen_failure(
            state,
            broker_name,
            selected,
            "INVALID_OPTION_LTP",
            "OPTION_QUOTE",
            {"ltp": quote.get("ltp")},
        )

    quality = _legacy()._option_entry_quality_multi(
        broker_name, obj, resolved, quote_price
    )

    return _open_common(
        conn,
        user_id,
        broker_name,
        selected,
        settings,
        resolved,
        quote_price,
        quality,
        resolved.get("lot_size") or _legacy().LOT_SIZES.get(underlying, 1),
        lambda r, a, q, p: _place_multi(obj, r, a, q, p),
        lambda: _multi_cash(obj),
        state,
    )


def _state_update(state, scans, selected, settings, rows):
    display = _display_scan(scans, selected, settings)
    if display:
        signal = dict(display.get("signal_data", {}))
        market = display.get("market_data", {})
        state.update(signal)
        state.update(
            {
                "price": market.get("price", 0),
                "underlying": display.get("underlying"),
                "chart_instrument": display.get("underlying"),
                "chart_interval": "ONE_MINUTE",
                "chart_candles": display.get("chart_candles", []),
            }
        )
    else:
        primary = settings.get("primary_instrument", "NIFTY")
        state.update(
            {
                "signal": "WAIT",
                "score": 0,
                "price": 0,
                "underlying": primary,
                "chart_instrument": primary,
                "chart_interval": "ONE_MINUTE",
                "chart_candles": [],
            }
        )

    state.update(
        {
            "engine_mode": AUTO_ENGINE_MODE,
            "auto_scan": True,
            "enabled_instruments": _enabled(settings),
            "scan_results": [_summary(scan) for scan in scans],
            "selected_for_entry": _summary(selected) if selected else None,
            "open_trade_count": len(rows),
            "open_positions": [
                {
                    "id": row["id"],
                    "underlying": _underlying(row),
                    "side": row["side"],
                    "symbol": row["symbol"],
                    "qty": row["qty"],
                    "capital_slot": _v(row, "capital_slot"),
                    "allocation_pct": _v(row, "allocation_pct"),
                    "trading_mode": _mode(row),
                }
                for row in rows
            ],
            "capital_plan": {
                "slot_1_percent": 50,
                "slot_2_percent": 40,
                "reserve_percent": 10,
                "max_open_positions": MAX_OPEN_POSITIONS,
                "different_index_required": True,
                "paper_hard_position_cap_removed": True,
                "lot_aware_flex": True,
                "allocation_note": "50/40 target; complete lot can flex while 10% reserve stays protected",
            },
            "open_trade_monitor_seconds": 5,
            "hero": _legacy().is_hero_window_active(),
            "status": "RUNNING",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    if rows and selected is None:
        state["signal"] = "HOLD_MULTI" if len(rows) > 1 else "HOLD"


def _setting_truthy(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {
        "1", "true", "yes", "on", "enabled", "unlimited"
    }


def _reconcile_mode_change_state(rows, current_mode, state):
    """Keep the mode guard only while conflicting open positions exist."""
    modes = {_mode(row) for row in (rows or [])}
    blocked = bool(modes and current_mode not in modes)
    if not blocked:
        state.pop("mode_change_blocked", None)
    return blocked


def _can_enter(conn, user_id, settings, rows, state):
    current_mode = (
        "live"
        if str(settings.get("trading_mode", "paper")).lower() == "live"
        else "paper"
    )
    _reconcile_mode_change_state(rows, current_mode, state)
    today_count = _today_count(conn, user_id)
    raw_daily = _i(settings.get("max_trades_per_day", 5), 5)

    if len(rows) >= MAX_OPEN_POSITIONS:
        state["entry_permission"] = {
            "allowed": False,
            "reason": "MAX_OPEN_POSITIONS_REACHED",
            "open_positions": len(rows),
            "max_open_positions": MAX_OPEN_POSITIONS,
            "today_count": today_count,
            "mode": current_mode,
        }
        return False

    # A pending live-order lock must never starve paper trading after a mode
    # change or Railway restart. It remains enforced only in actual live mode.
    if current_mode == "live" and state.get("live_order_lock"):
        state["entry_permission"] = {
            "allowed": False,
            "reason": "LIVE_ORDER_PENDING_LOCK",
            "today_count": today_count,
            "mode": current_mode,
        }
        return False
    if current_mode == "paper":
        state.pop("live_order_lock", None)

    # Paper is the SaaS testing mode. Unless explicitly disabled, do not stop
    # qualified entries at the old five-trade default. Live keeps its limit.
    explicit_unlimited = _setting_truthy(
        settings.get("unlimited_trades"),
        default=_setting_truthy(
            settings.get("paper_unlimited_trades"),
            default=(current_mode == "paper"),
        ),
    )
    unlimited = raw_daily <= 0 or explicit_unlimited
    max_daily = max(1, raw_daily) if raw_daily > 0 else None

    if not unlimited and today_count >= max_daily:
        state["entry_permission"] = {
            "allowed": False,
            "reason": "DAILY_TRADE_LIMIT_REACHED",
            "today_count": today_count,
            "max_trades_per_day": max_daily,
            "mode": current_mode,
        }
        return False

    state["entry_permission"] = {
        "allowed": True,
        "reason": "ENTRY_PERMISSION_OK",
        "today_count": today_count,
        "max_trades_per_day": None if unlimited else max_daily,
        "unlimited": unlimited,
        "mode": current_mode,
    }
    return True


def run_user_bot_auto(user_id, creds, state):
    obj = None
    while state.get("running"):
        try:
            if obj is None:
                obj = _legacy().angel_login(creds)
                state["status"] = "LOGGED_IN"

            settings = _legacy()._read_settings(user_id)
            profile = _legacy().get_active_profile_config(user_id)
            streak = _legacy()._get_consecutive_losses_today(user_id)
            scans = _scan_angel(user_id, obj, settings, profile, streak)

            conn = get_db()
            _ensure_schema(conn)
            rows = _open_rows(conn, user_id)
            _manage_rows(
                conn,
                user_id,
                rows,
                scans,
                lambda trade: _ltp_angel(obj, trade),
                lambda r, a, q, p: _place_angel(obj, r, a, q, p),
                state,
            )

            rows = _open_rows(conn, user_id)
            blocked = {_underlying(row) for row in rows}
            selected = None

            if _can_enter(conn, user_id, settings, rows, state):
                candidates = _eligible_candidates(scans, blocked)
                opened_candidate = _attempt_entry_candidates(
                    candidates,
                    lambda candidate: _open_angel(
                        conn, user_id, obj, candidate, settings, state
                    ),
                    state,
                )
                selected = opened_candidate or (
                    candidates[0] if candidates else None
                )
            else:
                state["entry_candidate_attempts"] = []

            rows = _open_rows(conn, user_id)
            _state_update(state, scans, selected, settings, rows)
            conn.close()

            for _ in range(12):
                time.sleep(5)
                if not state.get("running"):
                    break
                conn = get_db()
                _ensure_schema(conn)
                rows = _open_rows(conn, user_id)
                _manage_rows(
                    conn,
                    user_id,
                    rows,
                    scans,
                    lambda trade: _ltp_angel(obj, trade),
                    lambda r, a, q, p: _place_angel(obj, r, a, q, p),
                    state,
                )
                conn.close()
        except Exception as exc:
            obj = None
            state["status"] = "ERROR: " + str(exc)[:140]
            time.sleep(30)


def run_user_bot_multi_auto(user_id, broker_name, creds, state):
    obj = None
    while state.get("running"):
        try:
            if obj is None:
                obj = _legacy().create_broker(
                    broker_name,
                    creds["client_id"],
                    creds["api_key"],
                    creds["password"],
                    creds.get("totp_secret"),
                )
                login = obj.login()
                if not login.get("success"):
                    raise RuntimeError(login.get("message", "Login failed"))
                state["status"] = "LOGGED_IN"

            settings = _legacy()._read_settings(user_id)
            profile = _legacy().get_active_profile_config(user_id)
            streak = _legacy()._get_consecutive_losses_today(user_id)
            scans = _scan_multi(
                user_id,
                broker_name,
                obj,
                settings,
                profile,
                streak,
            )

            conn = get_db()
            _ensure_schema(conn)
            rows = _open_rows(conn, user_id)
            _manage_rows(
                conn,
                user_id,
                rows,
                scans,
                lambda trade: _ltp_multi(broker_name, obj, trade),
                lambda r, a, q, p: _place_multi(obj, r, a, q, p),
                state,
            )

            rows = _open_rows(conn, user_id)
            blocked = {_underlying(row) for row in rows}
            selected = None

            if _can_enter(conn, user_id, settings, rows, state):
                candidates = _eligible_candidates(scans, blocked)
                opened_candidate = _attempt_entry_candidates(
                    candidates,
                    lambda candidate: _open_multi(
                        conn,
                        user_id,
                        broker_name,
                        obj,
                        candidate,
                        settings,
                        state,
                    ),
                    state,
                )
                selected = opened_candidate or (
                    candidates[0] if candidates else None
                )
            else:
                state["entry_candidate_attempts"] = []

            rows = _open_rows(conn, user_id)
            _state_update(state, scans, selected, settings, rows)
            conn.close()

            for _ in range(12):
                time.sleep(5)
                if not state.get("running"):
                    break
                conn = get_db()
                _ensure_schema(conn)
                rows = _open_rows(conn, user_id)
                _manage_rows(
                    conn,
                    user_id,
                    rows,
                    scans,
                    lambda trade: _ltp_multi(broker_name, obj, trade),
                    lambda r, a, q, p: _place_multi(obj, r, a, q, p),
                    state,
                )
                conn.close()
        except Exception as exc:
            obj = None
            state["status"] = "ERROR: " + str(exc)[:140]
            time.sleep(30)

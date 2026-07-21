"""Align backtest sampling and AUTO portfolio capacity with the live bot.

The live engine scores ONE_MINUTE candles after 28 candles and allows up to two
simultaneous positions in different indices using 50% / 40% capital slots. The
legacy backtest fetched FIVE_MINUTE Angel candles and merged AUTO results with
one open trade only, which suppressed most morning and overlapping setups.

V2 also serializes Angel historical requests with retry/backoff. The previous
0.45-second spacing was below the practical historical API limit, so NIFTY and
BANKNIFTY could fail while the later SENSEX request succeeded.
"""

from copy import deepcopy
from datetime import datetime
import threading
import time

from backtest import routes


ANGEL_HISTORY_MIN_GAP_SECONDS = 1.25
ANGEL_HISTORY_RETRY_DELAYS = (0.0, 1.5, 3.0)
_ANGEL_HISTORY_LOCK = threading.Lock()
_ANGEL_LAST_HISTORY_REQUEST = 0.0
_FETCH_DIAGNOSTICS = {}


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def _diag_key(broker_name, instrument, date_str):
    return (
        str(broker_name or "").lower(),
        str(instrument or "").upper(),
        str(date_str or ""),
    )


def _save_fetch_diagnostic(
    broker_name,
    instrument,
    date_str,
    status,
    candle_count=0,
    attempts=0,
    error=None,
    source="BROKER",
):
    _FETCH_DIAGNOSTICS[_diag_key(broker_name, instrument, date_str)] = {
        "status": str(status or "UNKNOWN"),
        "candle_count": int(candle_count or 0),
        "attempts": int(attempts or 0),
        "error": str(error)[:240] if error else None,
        "source": str(source or "BROKER"),
    }


def _get_fetch_diagnostic(broker_name, instrument, date_str):
    return dict(
        _FETCH_DIAGNOSTICS.get(
            _diag_key(broker_name, instrument, date_str),
            {
                "status": "NOT_RECORDED",
                "candle_count": 0,
                "attempts": 0,
                "error": None,
                "source": None,
            },
        )
    )


def _angel_historical_rows(obj, params):
    """Serialize and retry Angel historical requests to avoid burst limiting."""
    global _ANGEL_LAST_HISTORY_REQUEST

    last_error = "Angel historical data unavailable"
    for attempt, delay in enumerate(ANGEL_HISTORY_RETRY_DELAYS, start=1):
        if delay > 0:
            time.sleep(delay)

        with _ANGEL_HISTORY_LOCK:
            elapsed = time.monotonic() - _ANGEL_LAST_HISTORY_REQUEST
            wait_for = ANGEL_HISTORY_MIN_GAP_SECONDS - elapsed
            if wait_for > 0:
                time.sleep(wait_for)

            try:
                response = obj.getCandleData(params)
            except Exception as exc:
                response = None
                last_error = f"{exc.__class__.__name__}: {str(exc)[:180]}"
            finally:
                _ANGEL_LAST_HISTORY_REQUEST = time.monotonic()

        if isinstance(response, dict):
            rows = response.get("data") or []
            if response.get("status") is not False and rows:
                return rows, attempt
            last_error = str(
                response.get("message")
                or response.get("errorcode")
                or response
            )[:240]
        elif response is not None:
            last_error = str(response)[:240]

    raise RuntimeError(
        "ANGEL_HISTORY_RETRY_EXHAUSTED: " + str(last_error)[:200]
    )


def _fetch_backtest_candles_one_minute(
    broker_name,
    obj,
    instrument,
    date_str,
):
    """Fetch the same one-minute index candles used by the live engine."""
    import pandas as pd

    cached = routes._okai_get_cached_candles(
        broker_name,
        instrument,
        date_str,
    )
    if cached is not None:
        _save_fetch_diagnostic(
            broker_name,
            instrument,
            date_str,
            status="OK",
            candle_count=len(cached),
            attempts=0,
            source="CACHE",
        )
        return cached

    day = datetime.strptime(date_str, "%Y-%m-%d")
    from_dt = day.replace(hour=9, minute=15, second=0, microsecond=0)
    to_dt = day.replace(hour=15, minute=30, second=0, microsecond=0)
    attempts = 1

    try:
        if broker_name == "angelone":
            token = routes.INDEX_TOKENS.get(instrument, "26000")
            exchange = routes.INDEX_EXCHANGE.get(instrument, "NSE")
            rows, attempts = _angel_historical_rows(
                obj,
                {
                    "exchange": exchange,
                    "symboltoken": token,
                    "interval": "ONE_MINUTE",
                    "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
                    "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
                },
            )
            df = pd.DataFrame(
                rows,
                columns=["time", "open", "high", "low", "close", "volume"],
            )

        elif broker_name == "zerodha":
            token = routes.ZERODHA_INDEX_TOKENS.get(instrument)
            response = obj.get_candles(
                symbol=token,
                interval="minute",
                from_date=from_dt.strftime("%Y-%m-%d %H:%M:%S"),
                to_date=to_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )
            if not response.get("success"):
                raise RuntimeError(
                    response.get("message") or "Zerodha historical data failed"
                )
            rows = response.get("candles", [])
            df = pd.DataFrame(rows)
            if not df.empty:
                df = df.rename(columns={"date": "time"})[
                    ["time", "open", "high", "low", "close", "volume"]
                ]

        elif broker_name == "upstox":
            key = routes.UPSTOX_INDEX_KEYS.get(instrument)
            response = obj.get_candles(
                symbol=key,
                interval="1m",
                from_date=date_str,
                to_date=date_str,
            )
            if not response.get("success"):
                raise RuntimeError(
                    response.get("message") or "Upstox historical data failed"
                )
            rows = response.get("candles", [])
            df = (
                pd.DataFrame(
                    rows,
                    columns=[
                        "time", "open", "high", "low", "close", "volume", "oi"
                    ],
                )
                if rows
                else pd.DataFrame()
            )
            if not df.empty:
                df = df[["time", "open", "high", "low", "close", "volume"]]

        else:
            raise RuntimeError(f"Unsupported historical broker: {broker_name}")

        if df.empty:
            raise RuntimeError("Broker returned zero historical candles")

        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")

        df["_sort_time"] = pd.to_datetime(df["time"], errors="coerce", utc=True)
        clean = (
            df.dropna(subset=["open", "high", "low", "close", "_sort_time"])
            .sort_values("_sort_time")
            .drop(columns=["_sort_time"])
            .drop_duplicates(subset=["time"], keep="last")
            .reset_index(drop=True)
        )

        if clean.empty:
            raise RuntimeError("Historical candles became empty after validation")

        routes._okai_store_cached_candles(
            broker_name,
            instrument,
            date_str,
            clean,
        )
        _save_fetch_diagnostic(
            broker_name,
            instrument,
            date_str,
            status="OK",
            candle_count=len(clean),
            attempts=attempts,
            source="BROKER",
        )
        return clean.copy(deep=True)

    except Exception as exc:
        _save_fetch_diagnostic(
            broker_name,
            instrument,
            date_str,
            status="ERROR",
            candle_count=0,
            attempts=attempts,
            error=f"{exc.__class__.__name__}: {str(exc)[:200]}",
            source="BROKER",
        )
        raise


def _recalculate_auto_result(raw_auto, starting_capital, selected):
    total_pnl = round(sum(float(t.get("pnl") or 0) for t in selected), 2)
    wins = sum(1 for t in selected if float(t.get("pnl") or 0) >= 0)
    losses = len(selected) - wins
    win_rate = round(wins / len(selected) * 100, 2) if selected else 0
    ending_capital = round(float(starting_capital) + total_pnl, 2)

    raw_auto.update({
        "trades": selected,
        "total_trades": len(selected),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "ending_capital": ending_capital,
    })
    summary = dict(raw_auto.get("summary") or {})
    summary.update({
        "trades": len(selected),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "capital": float(starting_capital),
        "net_pnl": total_pnl,
        "ending_capital": ending_capital,
        "capital_use_percent": 90,
    })
    raw_auto["summary"] = summary
    return raw_auto


def _run_auto_two_slot(
    broker_name,
    obj,
    date_str,
    capital,
    entry_threshold,
    sl_percent,
    target_percent,
):
    """AUTO merge matching live MAX2 / 50%-40% / different-index rules."""
    single_results = {}
    candidates = []
    debug_candidates = []

    for instrument in routes._OKAI_AUTO_INSTRUMENTS:
        result = routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST(
            broker_name,
            obj,
            instrument,
            date_str,
            capital,
            entry_threshold,
            sl_percent,
            target_percent,
        )
        single_results[instrument] = result
        if not isinstance(result, dict) or not result.get("success"):
            continue

        for trade in result.get("trades", []):
            row = dict(trade)
            row["instrument"] = instrument
            candidates.append(row)
        for candidate in result.get("debug_top_candidates", []):
            row = dict(candidate)
            row["instrument"] = instrument
            debug_candidates.append(row)

    candidates.sort(
        key=lambda trade: (
            _parse_time(trade.get("entry_time")),
            -int(trade.get("score") or 0),
            routes._OKAI_INSTRUMENT_PRIORITY.get(
                str(trade.get("instrument") or ""), 99
            ),
        )
    )

    current_equity = float(capital)
    active = []
    selected = []

    for candidate in candidates:
        entry_time = _parse_time(candidate.get("entry_time"))

        still_active = []
        for position in active:
            if _parse_time(position.get("exit_time")) <= entry_time:
                current_equity += float(position.get("pnl") or 0)
            else:
                still_active.append(position)
        active = still_active

        instrument = str(candidate.get("instrument") or "NIFTY").upper()
        if len(active) >= 2:
            continue
        if any(
            str(position.get("instrument") or "").upper() == instrument
            for position in active
        ):
            continue

        slot_number = 1 if not active else 2
        allocation = 0.50 if slot_number == 1 else 0.40
        lot_size = int(routes.LOT_SIZES.get(instrument, 1))
        entry_price = float(candidate.get("entry_price") or 0)
        exit_price = float(candidate.get("exit_price") or 0)
        sizing = routes._okai_calculate_lot_sizing(
            current_equity,
            entry_price,
            lot_size,
            allocation,
        )
        if not sizing.get("affordable"):
            continue

        trade = deepcopy(candidate)
        trade["trade_no"] = len(selected) + 1
        trade["slot"] = slot_number
        trade["slot_allocation_percent"] = int(allocation * 100)
        trade["lot_size"] = lot_size
        trade["lots"] = sizing["lots"]
        trade["qty"] = sizing["quantity"]
        trade["capital_before_trade"] = round(current_equity, 2)
        trade["usable_capital"] = sizing["usable_capital"]
        trade["capital_used"] = sizing["capital_used"]
        trade["capital_utilization_percent"] = sizing[
            "capital_utilization_percent"
        ]
        trade["pnl"] = round(
            (exit_price - entry_price) * sizing["quantity"], 2
        )
        selected.append(trade)
        active.append(trade)

    for position in sorted(active, key=lambda row: _parse_time(row.get("exit_time"))):
        current_equity += float(position.get("pnl") or 0)

    per_instrument = {}
    data_warnings = []
    for instrument, result in single_results.items():
        diagnostic = _get_fetch_diagnostic(
            broker_name,
            instrument,
            date_str,
        )
        success = bool(isinstance(result, dict) and result.get("success"))
        per_instrument[instrument] = {
            "success": success,
            "message": result.get("message") if isinstance(result, dict) else None,
            "trades": result.get("total_trades", 0)
            if isinstance(result, dict)
            else 0,
            "one_lot_pnl": result.get("total_pnl", 0)
            if isinstance(result, dict)
            else 0,
            "max_score": result.get("debug_max_score")
            if isinstance(result, dict)
            else None,
            "fetch_status": diagnostic["status"],
            "candle_count": diagnostic["candle_count"],
            "fetch_attempts": diagnostic["attempts"],
            "fetch_source": diagnostic["source"],
            "fetch_error": diagnostic["error"],
        }
        if diagnostic["status"] != "OK":
            data_warnings.append(
                f"{instrument}: {diagnostic['error'] or diagnostic['status']}"
            )

    raw_auto = {
        "success": True,
        "instrument": "AUTO",
        "date": date_str,
        "capital": float(capital),
        "trades": selected,
        "debug_top_candidates": sorted(
            debug_candidates,
            key=lambda row: (
                int(row.get("score") or 0),
                -routes._OKAI_INSTRUMENT_PRIORITY.get(
                    row.get("instrument"), 99
                ),
            ),
            reverse=True,
        )[:20],
        "debug_max_score": max(
            (
                result.get("debug_max_score")
                for result in single_results.values()
                if isinstance(result, dict)
                and result.get("debug_max_score") is not None
            ),
            default=None,
        ),
        "auto_scan": {
            "enabled": True,
            "instruments": list(routes._OKAI_AUTO_INSTRUMENTS),
            "selection": "EARLIEST_VALID_THEN_HIGHEST_SCORE",
            "simultaneous_trades": True,
            "max_concurrent_trades": 2,
            "different_index_required": True,
            "slot_1_percent": 50,
            "slot_2_percent": 40,
            "reserve_percent": 10,
            "model": "LIVE_MAX2_PORTFOLIO_V2_RATE_SAFE",
        },
        "position_sizing": {
            "mode": "LIVE_SLOT_50_40_RESERVE_10",
            "max_concurrent_trades": 2,
            "different_index_required": True,
            "equity_compounding": True,
        },
        "backtest_sampling": {
            "interval": "ONE_MINUTE",
            "warmup_candles": 28,
            "first_possible_check": "09:43 IST",
            "matches_live_signal_feed": True,
            "angel_historical_min_gap_seconds": ANGEL_HISTORY_MIN_GAP_SECONDS,
            "angel_retry_delays_seconds": list(ANGEL_HISTORY_RETRY_DELAYS),
        },
        "per_instrument": per_instrument,
        "all_indices_data_ready": not data_warnings,
        "data_warnings": data_warnings,
        "summary": {
            "capital": float(capital),
            "note": "AUTO live-parity MAX2 portfolio with one-minute candles.",
        },
        "note": (
            "Signal timing uses one-minute historical index candles. AUTO allows "
            "two simultaneous positions only in different indices using 50% and "
            "40% slots. Angel historical requests are rate-safe and retried."
        ),
    }

    first_success = next(
        (
            result
            for result in single_results.values()
            if isinstance(result, dict) and result.get("success")
        ),
        None,
    )
    if first_success:
        raw_auto["exit_model"] = first_success.get("exit_model")

    return _recalculate_auto_result(raw_auto, capital, selected)


def apply_live_frequency_portfolio_patch():
    if getattr(routes, "_okai_live_frequency_portfolio_v2", False):
        return

    # Old in-memory cache could contain five-minute or incomplete data.
    routes._OKAI_BACKTEST_CANDLE_CACHE.clear()
    routes.fetch_backtest_candles = _fetch_backtest_candles_one_minute
    routes._okai_run_auto_index_backtest = _run_auto_two_slot
    routes._okai_live_frequency_portfolio_v1 = True
    routes._okai_live_frequency_portfolio_v2 = True

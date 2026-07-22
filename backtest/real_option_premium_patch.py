"""Real historical option-premium backtesting for OKAI.

This patch replaces the synthetic option path inside the captured single-index
backtest before Structural Exit V5 compiles it. Signals continue to come from
real one-minute index candles, while entries, ATR stops, profit locks and exits
use the selected ATM option contract's real one-minute OHLC candles.

Historical expired contracts use Upstox Expired Instruments APIs. Those APIs
require an Upstox Plus subscription. Missing/unsupported data never falls back
silently to an estimated premium: the affected day is returned as SKIPPED with
an explicit diagnostic.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
import inspect
import threading
import time
from urllib.parse import quote

import requests

from backtest import routes as backtest_routes
from bot.brokers.upstox import UpstoxBroker
from bot import structural_exit_v2_patch as structural_patch


REAL_PREMIUM_MODEL = "REAL_UPSTOX_OPTION_OHLC_1M_V1"
UPSTOX_PLUS_ERROR = "UPSTOX_PLUS_REQUIRED_FOR_REAL_PREMIUM"
UNDERLYING_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
}
MAX_EXPIRY_GAP_DAYS = {
    "NIFTY": 10,
    "SENSEX": 10,
    "BANKNIFTY": 40,
}
TICK_SIZE = 0.05
REQUEST_RETRY_DELAYS = (0.0, 0.8, 2.0)

_CACHE_LOCK = threading.RLock()
_EXPIRY_CACHE = {}
_CONTRACT_CACHE = {}
_CANDLE_CACHE = {}
_MAX_CANDLE_CACHE = 256


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


def _day(value):
    return str(value or "")[:10]


def _today_ist():
    return (
        datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    ).date()


def _error_code(payload):
    if not isinstance(payload, dict):
        return ""
    errors = payload.get("errors") or []
    if isinstance(errors, list) and errors:
        row = errors[0] if isinstance(errors[0], dict) else {}
        return str(row.get("errorCode") or row.get("error_code") or "")
    return str(payload.get("errorCode") or payload.get("error_code") or "")


def _error_message(payload):
    if not isinstance(payload, dict):
        return str(payload)[:300]
    return str(
        payload.get("errors")
        or payload.get("message")
        or payload
    )[:300]


def _request_json(url, headers, params=None, timeout=25):
    last_status = 0
    last_payload = {}
    for delay in REQUEST_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
            )
            last_status = response.status_code
            try:
                payload = response.json()
            except Exception:
                payload = {"message": response.text[:300]}
            last_payload = payload

            if response.status_code == 200 and payload.get("status") == "success":
                return {
                    "success": True,
                    "status_code": response.status_code,
                    "payload": payload,
                }

            code = _error_code(payload)
            if code == "UDAPI1149":
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "error_code": code,
                    "message": UPSTOX_PLUS_ERROR,
                    "raw_message": _error_message(payload),
                }

            if response.status_code not in (429, 500, 502, 503, 504):
                break
        except Exception as exc:
            last_payload = {"message": f"{exc.__class__.__name__}: {exc}"}

    return {
        "success": False,
        "status_code": last_status,
        "error_code": _error_code(last_payload),
        "message": _error_message(last_payload),
    }


def _cache_get(cache, key):
    with _CACHE_LOCK:
        value = cache.get(key)
        if value is None:
            return None
        return value


def _cache_put(cache, key, value, max_items=None):
    with _CACHE_LOCK:
        cache[key] = value
        if max_items:
            while len(cache) > max_items:
                cache.pop(next(iter(cache)), None)


def _extract_expiries(payload):
    raw = payload.get("data") if isinstance(payload, dict) else []
    if isinstance(raw, dict):
        raw = (
            raw.get("expiries")
            or raw.get("expiry_dates")
            or raw.get("data")
            or []
        )
    output = []
    for item in raw or []:
        if isinstance(item, dict):
            value = (
                item.get("expiry")
                or item.get("expiry_date")
                or item.get("date")
            )
        else:
            value = item
        text = _day(value)
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except Exception:
            continue
        if text not in output:
            output.append(text)
    return sorted(output)


def _normalise_contract_rows(payload):
    rows = payload.get("data") if isinstance(payload, dict) else []
    if isinstance(rows, dict):
        rows = rows.get("contracts") or rows.get("data") or []
    output = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        key = str(
            raw.get("instrument_key")
            or raw.get("expired_instrument_key")
            or ""
        ).strip()
        if not key:
            continue
        option_type = str(
            raw.get("instrument_type")
            or raw.get("option_type")
            or ""
        ).upper()
        if option_type not in ("CE", "PE"):
            continue
        strike = _f(
            raw.get("strike_price")
            if raw.get("strike_price") is not None
            else raw.get("strike"),
            0,
        )
        output.append({
            "instrument_key": key,
            "symbol": str(
                raw.get("trading_symbol")
                or raw.get("tradingsymbol")
                or key
            ),
            "expiry": _day(raw.get("expiry") or raw.get("expiry_date")),
            "strike": strike,
            "option_type": option_type,
            "lot_size": max(
                1,
                _i(
                    raw.get("lot_size")
                    or raw.get("minimum_lot")
                    or 1,
                    1,
                ),
            ),
            "segment": str(raw.get("segment") or ""),
        })
    return output


def _upstox_expired_expiries(self, underlying_key):
    cache_key = (id(self), str(underlying_key))
    cached = _cache_get(_EXPIRY_CACHE, cache_key)
    if cached is not None:
        return dict(cached)

    result = _request_json(
        f"{self.BASE_URL}/expired-instruments/expiries",
        self._h(),
        params={"instrument_key": underlying_key},
    )
    if not result.get("success"):
        _cache_put(_EXPIRY_CACHE, cache_key, result)
        return dict(result)

    expiries = _extract_expiries(result["payload"])
    output = {"success": True, "expiries": expiries}
    _cache_put(_EXPIRY_CACHE, cache_key, output)
    return dict(output)


def _upstox_option_contracts_for_expiry(
    self,
    underlying_key,
    expiry_date,
    expired,
):
    cache_key = (
        id(self),
        str(underlying_key),
        str(expiry_date),
        bool(expired),
    )
    cached = _cache_get(_CONTRACT_CACHE, cache_key)
    if cached is not None:
        return {
            **cached,
            "contracts": list(cached.get("contracts") or []),
        }

    endpoint = (
        "/expired-instruments/option/contract"
        if expired
        else "/option/contract"
    )
    result = _request_json(
        f"{self.BASE_URL}{endpoint}",
        self._h(),
        params={
            "instrument_key": underlying_key,
            "expiry_date": expiry_date,
        },
    )
    if not result.get("success"):
        _cache_put(_CONTRACT_CACHE, cache_key, result)
        return dict(result)

    contracts = _normalise_contract_rows(result["payload"])
    output = {
        "success": True,
        "contracts": contracts,
        "expired": bool(expired),
    }
    _cache_put(_CONTRACT_CACHE, cache_key, output)
    return {
        **output,
        "contracts": list(contracts),
    }


def _upstox_active_option_contracts(self, underlying_key):
    cache_key = (id(self), str(underlying_key), "ACTIVE_ALL")
    cached = _cache_get(_CONTRACT_CACHE, cache_key)
    if cached is not None:
        return {
            **cached,
            "contracts": list(cached.get("contracts") or []),
        }

    result = _request_json(
        f"{self.BASE_URL}/option/contract",
        self._h(),
        params={"instrument_key": underlying_key},
    )
    if not result.get("success"):
        _cache_put(_CONTRACT_CACHE, cache_key, result)
        return dict(result)

    contracts = _normalise_contract_rows(result["payload"])
    output = {"success": True, "contracts": contracts}
    _cache_put(_CONTRACT_CACHE, cache_key, output)
    return {**output, "contracts": list(contracts)}


def _upstox_resolve_historical_option(
    self,
    underlying,
    trade_date,
    spot_price,
    option_type,
):
    name = str(underlying or "").upper()
    side = str(option_type or "").upper()
    underlying_key = UNDERLYING_KEYS.get(name)
    if not underlying_key or side not in ("CE", "PE"):
        return {
            "success": False,
            "message": "REAL_PREMIUM_UNSUPPORTED_INSTRUMENT",
        }

    date_text = _day(trade_date)
    try:
        trade_day = datetime.strptime(date_text, "%Y-%m-%d").date()
    except Exception:
        return {"success": False, "message": "REAL_PREMIUM_INVALID_DATE"}

    expired_result = self.get_expired_expiries(underlying_key)
    if not expired_result.get("success"):
        return expired_result

    active_result = self.get_active_option_contracts(underlying_key)
    active_contracts = (
        active_result.get("contracts") or []
        if active_result.get("success")
        else []
    )

    expiry_values = set(expired_result.get("expiries") or [])
    expiry_values.update(
        row.get("expiry")
        for row in active_contracts
        if row.get("expiry")
    )

    candidates = []
    for expiry_text in expiry_values:
        try:
            expiry_day = datetime.strptime(expiry_text, "%Y-%m-%d").date()
        except Exception:
            continue
        if expiry_day >= trade_day:
            candidates.append((expiry_day, expiry_text))

    if not candidates:
        return {
            "success": False,
            "message": "REAL_PREMIUM_EXPIRY_NOT_AVAILABLE_FOR_DATE",
            "trade_date": date_text,
        }

    expiry_day, expiry_text = min(candidates, key=lambda row: row[0])
    gap_days = (expiry_day - trade_day).days
    if gap_days > MAX_EXPIRY_GAP_DAYS.get(name, 10):
        return {
            "success": False,
            "message": "REAL_PREMIUM_EXPIRY_OUTSIDE_NEAREST_WINDOW",
            "trade_date": date_text,
            "nearest_expiry": expiry_text,
            "gap_days": gap_days,
        }

    expired = expiry_day < _today_ist()
    if expired:
        contract_result = self.get_option_contracts_for_expiry(
            underlying_key,
            expiry_text,
            True,
        )
    else:
        contracts = [
            row
            for row in active_contracts
            if row.get("expiry") == expiry_text
        ]
        if contracts:
            contract_result = {
                "success": True,
                "contracts": contracts,
                "expired": False,
            }
        else:
            contract_result = self.get_option_contracts_for_expiry(
                underlying_key,
                expiry_text,
                False,
            )

    if not contract_result.get("success"):
        return contract_result

    matching = [
        row
        for row in contract_result.get("contracts") or []
        if row.get("option_type") == side
        and row.get("expiry") == expiry_text
    ]
    if not matching:
        return {
            "success": False,
            "message": "REAL_PREMIUM_OPTION_CONTRACT_NOT_FOUND",
            "expiry": expiry_text,
            "side": side,
        }

    spot = _f(spot_price, 0)
    selected = min(
        matching,
        key=lambda row: (
            abs(_f(row.get("strike"), 0) - spot),
            _f(row.get("strike"), 0),
        ),
    )
    return {
        "success": True,
        **selected,
        "underlying": name,
        "underlying_key": underlying_key,
        "expired": bool(expired),
        "selection": "NEAREST_ATM_STRIKE_NEAREST_VALID_EXPIRY",
        "expiry_gap_days": gap_days,
    }


def _upstox_historical_option_candles(
    self,
    contract,
    from_date,
    to_date,
    interval="1minute",
):
    instrument_key = str(contract.get("instrument_key") or "").strip()
    if not instrument_key:
        return {
            "success": False,
            "message": "REAL_PREMIUM_OPTION_KEY_MISSING",
        }

    from_day = _day(from_date)
    to_day = _day(to_date)
    expired = bool(contract.get("expired"))
    cache_key = (
        id(self),
        instrument_key,
        from_day,
        to_day,
        interval,
        expired,
    )
    cached = _cache_get(_CANDLE_CACHE, cache_key)
    if cached is not None:
        return {**cached, "candles": list(cached.get("candles") or [])}

    if expired:
        encoded = quote(instrument_key, safe="")
        url = (
            f"{self.BASE_URL}/expired-instruments/historical-candle/"
            f"{encoded}/{interval}/{to_day}/{from_day}"
        )
        result = _request_json(url, self._h())
        if not result.get("success"):
            _cache_put(_CANDLE_CACHE, cache_key, result, _MAX_CANDLE_CACHE)
            return dict(result)
        candles = (
            result.get("payload", {})
            .get("data", {})
            .get("candles", [])
            or []
        )
        request_mode = "UPSTOX_EXPIRED_INSTRUMENTS"
    else:
        interval_alias = {
            "1minute": "1m",
            "3minute": "3m",
            "5minute": "5m",
            "15minute": "15m",
            "30minute": "30m",
        }.get(interval, "1m")
        result = self.get_candles(
            symbol=instrument_key,
            interval=interval_alias,
            from_date=from_day,
            to_date=to_day,
        )
        if not result.get("success"):
            return result
        candles = result.get("candles") or []
        request_mode = result.get("request_mode") or "UPSTOX_ACTIVE_CONTRACT"

    candles = sorted(
        [row for row in candles if isinstance(row, (list, tuple)) and len(row) >= 5],
        key=lambda row: str(row[0]),
    )
    output = {
        "success": bool(candles),
        "candles": candles,
        "instrument_key": instrument_key,
        "request_mode": request_mode,
        "message": None if candles else "REAL_PREMIUM_ZERO_OPTION_CANDLES",
    }
    _cache_put(_CANDLE_CACHE, cache_key, output, _MAX_CANDLE_CACHE)
    return {**output, "candles": list(candles)}


def _install_upstox_methods():
    UpstoxBroker.get_expired_expiries = _upstox_expired_expiries
    UpstoxBroker.get_option_contracts_for_expiry = (
        _upstox_option_contracts_for_expiry
    )
    UpstoxBroker.get_active_option_contracts = _upstox_active_option_contracts
    UpstoxBroker.resolve_historical_option = (
        _upstox_resolve_historical_option
    )
    UpstoxBroker.get_historical_option_candles = (
        _upstox_historical_option_candles
    )


def _epoch_minute(value):
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(
            tzinfo=timezone(timedelta(hours=5, minutes=30))
        )
    return int(dt.timestamp() // 60)


def _build_option_bars(candles):
    bars = {}
    for row in candles or []:
        if len(row) < 5:
            continue
        minute = _epoch_minute(row[0])
        if minute is None:
            continue
        bars[minute] = {
            "time": str(row[0]),
            "open": max(TICK_SIZE, _f(row[1], TICK_SIZE)),
            "high": max(TICK_SIZE, _f(row[2], TICK_SIZE)),
            "low": max(TICK_SIZE, _f(row[3], TICK_SIZE)),
            "close": max(TICK_SIZE, _f(row[4], TICK_SIZE)),
            "volume": _f(row[5], 0) if len(row) > 5 else 0,
            "oi": _f(row[6], 0) if len(row) > 6 else 0,
        }
    keys = sorted(bars)
    return bars, keys


def _real_premium_prepare_entry(
    broker_name,
    obj,
    instrument,
    date_str,
    entry_time,
    spot_price,
    side,
):
    if str(broker_name or "").lower() != "upstox":
        return {
            "success": False,
            "message": "REAL_PREMIUM_REQUIRES_UPSTOX_BROKER",
        }
    if not hasattr(obj, "resolve_historical_option"):
        return {
            "success": False,
            "message": "REAL_PREMIUM_UPSTOX_PATCH_NOT_INSTALLED",
        }

    contract = obj.resolve_historical_option(
        instrument,
        date_str,
        spot_price,
        side,
    )
    if not contract.get("success"):
        return contract

    candle_result = obj.get_historical_option_candles(
        contract,
        date_str,
        date_str,
        interval="1minute",
    )
    if not candle_result.get("success"):
        return {
            **candle_result,
            "contract": contract,
        }

    bars, keys = _build_option_bars(candle_result.get("candles") or [])
    signal_minute = _epoch_minute(entry_time)
    if signal_minute is None or not keys:
        return {
            "success": False,
            "message": "REAL_PREMIUM_ENTRY_TIME_NOT_FOUND",
        }

    # Signal is known only after its completed index candle. Use the next
    # available option candle's OPEN, never the signal candle's already-known OHLC.
    target = signal_minute + 1
    position = bisect_left(keys, target)
    if position >= len(keys) or keys[position] > target + 3:
        return {
            "success": False,
            "message": "REAL_PREMIUM_NEXT_CANDLE_NOT_AVAILABLE",
            "signal_time": str(entry_time),
        }

    entry_minute = keys[position]
    entry_bar = bars[entry_minute]
    lot_size = max(1, _i(contract.get("lot_size"), 1))

    # Keep the exchange lot mapping aligned with the actual historical contract.
    backtest_routes.LOT_SIZES[str(instrument).upper()] = lot_size

    return {
        "success": True,
        "entry_price": round(entry_bar["open"], 2),
        "entry_time": entry_bar["time"],
        "entry_minute": entry_minute,
        "bars": bars,
        "bar_keys": keys,
        "symbol": contract.get("symbol"),
        "instrument_key": contract.get("instrument_key"),
        "expiry": contract.get("expiry"),
        "strike": contract.get("strike"),
        "option_type": contract.get("option_type"),
        "lot_size": lot_size,
        "expired_contract": bool(contract.get("expired")),
        "request_mode": candle_result.get("request_mode"),
        "premium_source": REAL_PREMIUM_MODEL,
        "execution_model": "NEXT_OPTION_CANDLE_OPEN",
    }


def _real_premium_bar(open_trade, candle_time):
    minute = _epoch_minute(candle_time)
    bars = open_trade.get("_real_option_bars") or {}
    keys = open_trade.get("_real_option_bar_keys") or []
    entry_minute = _i(open_trade.get("_real_option_entry_minute"), 0)
    if minute is None or not bars or not keys or minute < entry_minute:
        return None

    exact = bars.get(minute)
    if exact is not None:
        return exact

    # Liquid ATM index options are normally continuous. For a rare no-trade
    # minute, use only a recent prior candle; never look ahead.
    position = bisect_right(keys, minute) - 1
    if position < 0:
        return None
    selected_minute = keys[position]
    if minute - selected_minute > 3:
        return None
    return bars.get(selected_minute)


def _real_error_row(real_option, candle_time, side):
    return {
        "time": str(candle_time),
        "side": str(side),
        "message": str(real_option.get("message") or "REAL_PREMIUM_FAILED")[:220],
        "error_code": str(real_option.get("error_code") or "")[:80],
        "raw_message": str(real_option.get("raw_message") or "")[:220],
        "trade_date": real_option.get("trade_date"),
        "nearest_expiry": real_option.get("nearest_expiry"),
    }


def _patch_captured_single_index_backtest_real():
    target = getattr(
        backtest_routes,
        "_OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST",
        None,
    )
    if target is None:
        return False, "CAPTURED_SINGLE_INDEX_FUNCTION_MISSING"

    try:
        source = inspect.getsource(target)
        changed = source
        changed = changed.replace(
            "def run_realistic_day_backtest(",
            "def _okai_single_index_real_premium_v1(",
            1,
        )

        # Structural Exit V5 parity.
        changed = changed.replace(
            "reversal_required_candles = 2",
            "reversal_required_candles = (\n"
            "                1 if current_r <= -0.35 else 2\n"
            "            )",
        )
        changed = changed.replace(
            '"TWO_CANDLE_REVERSAL_EXIT"',
            '"VWAP_ST_EMA_STRUCTURAL_EXIT"',
        )
        changed = changed.replace(
            '"mode": "CONFIRMED_TWO_CANDLE"',
            '"mode": "VWAP_ST_EMA_ADAPTIVE_BALANCED_V5"',
        )
        changed = changed.replace(
            '"confirmation_candles": 2',
            '"confirmation_candles": "1_AT_MINUS_0.35R_ELSE_2"',
        )

        if "real_premium_errors = []" not in changed:
            changed = changed.replace(
                "    _core_quality_block_count = 0\n",
                "    _core_quality_block_count = 0\n"
                "    real_premium_errors = []\n",
                1,
            )

        old_premium_block = '''            close_pct = (spot_close-entry_spot)/entry_spot*100
            if side == "CE":
                good_pct = (spot_high-entry_spot)/entry_spot*100
                bad_pct = (spot_low-entry_spot)/entry_spot*100
            else:
                close_pct = -close_pct
                good_pct = (entry_spot-spot_low)/entry_spot*100
                bad_pct = (entry_spot-spot_high)/entry_spot*100

            response = 8.0
            current_premium = max(0.5, entry*(1+close_pct*response/100))
            premium_high = max(0.5, entry*(1+good_pct*response/100))
            premium_low = max(0.5, entry*(1+bad_pct*response/100))'''
        real_premium_block = '''            option_bar = _okai_real_premium_bar(
                open_trade,
                last["time"],
            )
            if option_bar is None:
                open_trade["missing_option_candle_count"] = (
                    int(open_trade.get("missing_option_candle_count", 0)) + 1
                )
                continue

            response = None
            current_premium = max(0.05, float(option_bar["close"]))
            premium_high = max(0.05, float(option_bar["high"]))
            premium_low = max(0.05, float(option_bar["low"]))'''
        changed = changed.replace(old_premium_block, real_premium_block)

        if "last_loss_exit_index = None" not in changed:
            changed = changed.replace(
                "    consecutive_losses = 0\n",
                "    consecutive_losses = 0\n"
                "    last_loss_exit_index = None\n",
                1,
            )
        if "last_loss_exit_index = i if pnl < 0 else None" not in changed:
            changed = changed.replace(
                "                consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0\n",
                "                consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0\n"
                "                last_loss_exit_index = i if pnl < 0 else None\n",
            )

        entry_gate = '''        if (
            signal_data["trade_allowed"]
            and signal_data["signal"] in ("CE", "PE")
            and signal_data["score"] >= entry_threshold
            and core_quality_ok
        ):'''
        balanced_entry_gate = '''        loss_cooldown_active = (
            last_loss_exit_index is not None
            and i - last_loss_exit_index < 10
        )

        if (
            signal_data["trade_allowed"]
            and signal_data["signal"] in ("CE", "PE")
            and signal_data["score"] >= entry_threshold
            and core_quality_ok
            and not loss_cooldown_active
        ):'''
        if "loss_cooldown_active" not in changed:
            changed = changed.replace(entry_gate, balanced_entry_gate)

        old_entry_premium = '''            est_entry_premium = round(
                max(20, atr * 6),
                2,
            )'''
        real_entry_premium = '''            real_option = _okai_real_premium_prepare_entry(
                broker_name=broker_name,
                obj=obj,
                instrument=instrument,
                date_str=date_str,
                entry_time=last["time"],
                spot_price=price,
                side=signal_data["signal"],
            )
            if not real_option.get("success"):
                real_premium_errors.append(
                    _okai_real_premium_error_row(
                        real_option,
                        last["time"],
                        signal_data["signal"],
                    )
                )
                continue
            est_entry_premium = float(real_option["entry_price"])'''
        changed = changed.replace(old_entry_premium, real_entry_premium)

        # Store the real contract/candle path only inside the open trade. The
        # large candle map is deliberately not copied into result JSON.
        changed = changed.replace(
            '''                "side": signal_data["signal"],
                "entry_price": est_entry_premium,''',
            '''                "side": signal_data["signal"],
                "option_symbol": real_option["symbol"],
                "option_instrument_key": real_option["instrument_key"],
                "option_expiry": real_option["expiry"],
                "option_strike": real_option["strike"],
                "lot_size": real_option["lot_size"],
                "premium_source": real_option["premium_source"],
                "premium_request_mode": real_option["request_mode"],
                "premium_execution_model": real_option["execution_model"],
                "_real_option_bars": real_option["bars"],
                "_real_option_bar_keys": real_option["bar_keys"],
                "_real_option_entry_minute": real_option["entry_minute"],
                "entry_price": est_entry_premium,''',
        )

        if '"entry_index": i' not in changed:
            changed = changed.replace(
                '                "entry_time": str(last["time"]),\n',
                '                "entry_time": str(last["time"]),\n'
                '                "entry_index": i,\n',
            )
        changed = changed.replace(
            '                "entry_time": str(last["time"]),',
            '                "entry_time": str(real_option.get("entry_time") or last["time"]),',
        )
        changed = changed.replace(
            '                "entry_index": i,',
            '                "entry_index": i + 1,',
        )

        structural_block = '''            structural_exit = (
                open_trade["reversal_count"]
                >= reversal_required_candles
            )

            if ('''
        balanced_structural_block = '''            structural_exit = (
                open_trade["reversal_count"]
                >= reversal_required_candles
            )
            bars_held = max(
                0,
                i - int(open_trade.get("entry_index", i)),
            )
            peak_r_seen = max(
                float(open_trade.get("peak_r", 0) or 0),
                (premium_high - entry) / risk_points,
            )
            no_follow_through_exit = (
                bars_held >= 3
                and peak_r_seen < 0.30
                and int(reversal.get("opposite_count", 0) or 0) >= 2
            )

            if ('''
        if "no_follow_through_exit" not in changed:
            changed = changed.replace(
                structural_block,
                balanced_structural_block,
            )
            changed = changed.replace(
                "                hit_sl\n                or force_combined_reserve_exit",
                "                hit_sl\n                or no_follow_through_exit\n                or force_combined_reserve_exit",
            )
            changed = changed.replace(
                '''                elif force_combined_reserve_exit:
                    exit_price = round(''',
                '''                elif no_follow_through_exit:
                    exit_price = round(
                        current_premium,
                        2,
                    )
                    reason = "NO_FOLLOW_THROUGH_EXIT_3C_2OF3"
                elif force_combined_reserve_exit:
                    exit_price = round(''',
            )
            changed = changed.replace(
                '                    "fixed_target_enabled": False,',
                '                    "no_follow_through_exit": bool(no_follow_through_exit),\n'
                '                    "bars_held_at_exit": bars_held,\n'
                '                    "peak_r_seen": round(peak_r_seen, 2),\n'
                '                    "fixed_target_enabled": False,',
            )

        changed = changed.replace(
            "                pnl = round((exit_price-entry)*qty,2)",
            "                trade_qty = int(open_trade.get(\"lot_size\") or qty)\n"
            "                pnl = round((exit_price-entry)*trade_qty,2)",
        )
        changed = changed.replace(
            '                    "symbol": f"{instrument} {side}",',
            '                    "symbol": open_trade.get("option_symbol") or f"{instrument} {side}",',
        )
        changed = changed.replace(
            '                    "qty": qty,',
            '                    "qty": trade_qty,',
        )
        changed = changed.replace(
            '                    "premium_response_factor": response,',
            '                    "premium_response_factor": None,\n'
            '                    "premium_model": "REAL_UPSTOX_OPTION_OHLC_1M_V1",\n'
            '                    "option_instrument_key": open_trade.get("option_instrument_key"),\n'
            '                    "option_expiry": open_trade.get("option_expiry"),\n'
            '                    "option_strike": open_trade.get("option_strike"),\n'
            '                    "lot_size": int(open_trade.get("lot_size") or trade_qty),\n'
            '                    "premium_source": open_trade.get("premium_source"),\n'
            '                    "premium_request_mode": open_trade.get("premium_request_mode"),\n'
            '                    "premium_execution_model": open_trade.get("premium_execution_model"),',
        )

        # Do not report a zero-trade tested day when a real-premium candidate was
        # found but Upstox Plus/expired data was unavailable.
        changed = changed.replace(
            '    total_pnl = round(sum(t["pnl"] for t in trades), 2)',
            '''    if real_premium_errors and not trades:
        first_real_error = real_premium_errors[0]
        return {
            "success": False,
            "message": first_real_error.get("message") or "REAL_PREMIUM_DATA_UNAVAILABLE",
            "premium_mode": "REAL",
            "premium_model": "REAL_UPSTOX_OPTION_OHLC_1M_V1",
            "real_premium_errors": real_premium_errors[:20],
            "instrument": instrument,
            "date": date_str,
        }

    total_pnl = round(sum(t["pnl"] for t in trades), 2)''',
            1,
        )

        changed = changed.replace(
            '''    return {
        "success": True,''',
            '''    return {
        "success": True,
        "premium_mode": "REAL",
        "premium_model": "REAL_UPSTOX_OPTION_OHLC_1M_V1",
        "real_premium_errors": real_premium_errors[:20],''',
            1,
        )
        changed = changed.replace(
            '"normal_option_response": 0.50,',
            '"normal_option_response": None,\n'
            '            "premium_data": "REAL_OPTION_OHLC_1M",',
        )
        changed = changed.replace(
            '"expiry_option_response": 1.00,',
            '"expiry_option_response": None,',
        )
        changed = changed.replace(
            '"note": "Signal timing/score based on REAL historical index candles. Option premium is an ATR-based estimate since real historical option premiums aren\'t available from the broker\'s live scrip master.",',
            '"note": "Signal timing uses real historical index candles. Entry, stop, profit lock and exit use the exact selected option contract\'s real 1-minute OHLC candles.",',
        )
        changed = changed.replace(
            '"note": "Real signal timing (historical candles). Premium P&L is an estimate.",',
            '"note": "Real index signals and real option-premium OHLC P&L.",',
        )

        required = (
            "_okai_single_index_real_premium_v1",
            "_okai_real_premium_prepare_entry",
            "_okai_real_premium_bar",
            "REAL_UPSTOX_OPTION_OHLC_1M_V1",
            "NO_FOLLOW_THROUGH_EXIT_3C_2OF3",
            "loss_cooldown_active",
        )
        missing = [marker for marker in required if marker not in changed]
        if missing:
            return False, "REAL_PREMIUM_SOURCE_TRANSFORM_MISSING:" + ",".join(missing)
        if "response = 8.0" in changed or "max(20, atr * 6)" in changed:
            return False, "SYNTHETIC_PREMIUM_SOURCE_STILL_PRESENT"

        exec(
            compile(changed, backtest_routes.__file__, "exec"),
            backtest_routes.__dict__,
        )
        patched = backtest_routes.__dict__.get(
            "_okai_single_index_real_premium_v1"
        )
        if not callable(patched):
            return False, "REAL_PREMIUM_FUNCTION_NOT_CREATED"

        backtest_routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = patched
        try:
            backtest_routes._OKAI_BACKTEST_CANDLE_CACHE.clear()
        except Exception:
            pass
        return True, "OK_REAL_OPTION_PREMIUM_OHLC_V1"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}:{str(exc)[:220]}"


def prepare_real_option_premium_patch():
    """Install before apply_structural_exit_v2_patch()."""
    if getattr(backtest_routes, "_okai_real_option_premium_prepared_v1", False):
        return

    _install_upstox_methods()

    # Helpers are referenced by the dynamically compiled backtest function.
    backtest_routes._okai_real_premium_prepare_entry = (
        _real_premium_prepare_entry
    )
    backtest_routes._okai_real_premium_bar = _real_premium_bar
    backtest_routes._okai_real_premium_error_row = _real_error_row

    # Structural Exit V5 owns the one-time source compilation. Replace only its
    # compiler hook so all existing runtime structural logic and flags remain.
    structural_patch._patch_captured_single_index_backtest = (
        _patch_captured_single_index_backtest_real
    )

    backtest_routes._okai_real_option_premium_prepared_v1 = True
    backtest_routes._okai_real_option_premium_model = REAL_PREMIUM_MODEL

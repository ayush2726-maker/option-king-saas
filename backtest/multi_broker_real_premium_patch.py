"""Real option-premium entry data through the user's selected broker.

Upstox keeps its active/expired instrument implementation. Angel One and
Zerodha use their active contract masters and exact one-minute option candles.
If the contract that was nearest on a historical date has already expired and
is no longer exposed by that broker, the day is explicitly SKIPPED; a later
expiry or synthetic premium is never substituted.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date, datetime, timedelta

from backtest import real_option_premium_patch as core
from backtest import routes
from bot.option_chain import (
    expected_expiry_for_trade_date,
    get_atm_strike,
    resolve_option_for_date,
)


SUPPORTED_REAL_BROKERS = {"angelone", "upstox", "zerodha"}
GENERIC_REAL_MODEL = "REAL_SELECTED_BROKER_OPTION_OHLC_1M_V2"


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value or "")[:10], "%Y-%m-%d").date()


def _float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _int(value, default=0):
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _normalise_candles(raw_rows):
    output = []
    for row in raw_rows or []:
        if isinstance(row, dict):
            stamp = row.get("date") or row.get("time") or row.get("timestamp")
            open_price = row.get("open")
            high = row.get("high")
            low = row.get("low")
            close = row.get("close")
            volume = row.get("volume") or 0
            oi = row.get("oi") or 0
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            stamp = row[0]
            open_price = row[1]
            high = row[2]
            low = row[3]
            close = row[4]
            volume = row[5] if len(row) > 5 else 0
            oi = row[6] if len(row) > 6 else 0
        else:
            continue

        if not stamp:
            continue
        values = (
            _float(open_price, -1),
            _float(high, -1),
            _float(low, -1),
            _float(close, -1),
        )
        if min(values) <= 0:
            continue
        output.append([
            str(stamp),
            values[0],
            values[1],
            values[2],
            values[3],
            _float(volume, 0),
            _float(oi, 0),
        ])

    output.sort(key=lambda row: str(row[0]))
    return output


def _angel_contract(instrument, trade_date, spot_price, side):
    contract = resolve_option_for_date(
        underlying=instrument,
        trade_date=trade_date,
        spot_price=spot_price,
        option_type=side,
    )
    if not contract:
        return {
            "success": False,
            "message": "REAL_PREMIUM_ANGEL_ACTIVE_CONTRACT_NOT_AVAILABLE",
            "trade_date": str(trade_date)[:10],
            "detail": (
                "Requested date ka nearest-expiry contract Angel One active "
                "scrip master me nahi hai; expired contract substitute nahi kiya."
            ),
        }
    return {"success": True, **contract}


def _zerodha_contract(obj, instrument, trade_date, spot_price, side):
    if not getattr(obj, "kite", None):
        return {
            "success": False,
            "message": "REAL_PREMIUM_ZERODHA_SESSION_NOT_READY",
        }

    name = str(instrument or "").upper()
    exchange = "BFO" if name == "SENSEX" else "NFO"
    trade_day = _date(trade_date)
    expected = expected_expiry_for_trade_date(name, trade_day)
    strike_target = get_atm_strike(name, spot_price)
    candidates = []

    try:
        rows = obj.kite.instruments(exchange)
    except Exception as exc:
        return {
            "success": False,
            "message": "REAL_PREMIUM_ZERODHA_INSTRUMENT_MASTER_FAILED",
            "raw_message": str(exc)[:180],
        }

    for row in rows or []:
        if str(row.get("name") or "").upper() != name:
            continue
        if str(row.get("instrument_type") or "").upper() != str(side).upper():
            continue

        expiry = row.get("expiry")
        try:
            expiry_day = _date(expiry)
        except Exception:
            continue
        if expiry_day < trade_day or abs((expiry_day - expected).days) > 3:
            continue

        strike = _float(row.get("strike"), -1)
        if strike <= 0:
            continue
        candidates.append(
            (
                abs((expiry_day - expected).days),
                expiry_day,
                abs(strike - strike_target),
                strike,
                row,
            )
        )

    if not candidates:
        return {
            "success": False,
            "message": "REAL_PREMIUM_ZERODHA_ACTIVE_CONTRACT_NOT_AVAILABLE",
            "trade_date": str(trade_date)[:10],
            "detail": (
                "Requested date ka nearest-expiry contract Zerodha active "
                "instrument master me nahi hai; expired contract substitute nahi kiya."
            ),
        }

    _, expiry_day, _, strike, selected = min(
        candidates,
        key=lambda item: (item[0], item[1], item[2], item[3]),
    )
    return {
        "success": True,
        "token": str(selected.get("instrument_token") or ""),
        "instrument_key": str(selected.get("instrument_token") or ""),
        "symbol": str(selected.get("tradingsymbol") or ""),
        "exchange": exchange,
        "exch_seg": exchange,
        "expiry": expiry_day.isoformat(),
        "strike": strike,
        "option_type": str(side).upper(),
        "lot_size": max(1, _int(selected.get("lot_size"), 1)),
        "selection": "EXPECTED_NEAREST_EXPIRY_ATM_ACTIVE_MASTER",
    }


def _fetch_angel_candles(obj, contract, trade_date):
    start = f"{str(trade_date)[:10]} 09:15"
    end = f"{str(trade_date)[:10]} 15:30"
    params = {
        "exchange": contract.get("exch_seg") or contract.get("exchange") or "NFO",
        "symboltoken": str(contract.get("token") or ""),
        "interval": "ONE_MINUTE",
        "fromdate": start,
        "todate": end,
    }

    try:
        if hasattr(obj, "getCandleData"):
            response = obj.getCandleData(params)
        elif getattr(obj, "smart_api", None) is not None:
            response = obj.smart_api.getCandleData(params)
        else:
            response = obj.get_candles(
                symbol=str(contract.get("token") or ""),
                interval="1m",
                from_date=start,
                to_date=end,
                exchange=params["exchange"],
            )
            if isinstance(response, dict) and response.get("success"):
                return {
                    "success": True,
                    "candles": _normalise_candles(response.get("candles") or []),
                    "request_mode": "ANGELONE_ACTIVE_CONTRACT_HISTORICAL",
                }
    except Exception as exc:
        return {
            "success": False,
            "message": "REAL_PREMIUM_ANGEL_OPTION_CANDLES_FAILED",
            "raw_message": str(exc)[:180],
        }

    rows = response.get("data") if isinstance(response, dict) else None
    candles = _normalise_candles(rows or [])
    if not candles:
        return {
            "success": False,
            "message": "REAL_PREMIUM_ZERO_OPTION_CANDLES",
            "raw_message": str(response)[:180],
        }
    return {
        "success": True,
        "candles": candles,
        "request_mode": "ANGELONE_ACTIVE_CONTRACT_HISTORICAL",
    }


def _fetch_zerodha_candles(obj, contract, trade_date):
    day = str(trade_date)[:10]
    try:
        result = obj.get_candles(
            symbol=str(contract.get("token") or contract.get("instrument_key") or ""),
            interval="1m",
            from_date=f"{day} 09:15:00",
            to_date=f"{day} 15:30:00",
            exchange=contract.get("exchange") or "NFO",
        )
    except Exception as exc:
        return {
            "success": False,
            "message": "REAL_PREMIUM_ZERODHA_OPTION_CANDLES_FAILED",
            "raw_message": str(exc)[:180],
        }

    if not isinstance(result, dict) or not result.get("success"):
        return {
            "success": False,
            "message": "REAL_PREMIUM_ZERODHA_OPTION_CANDLES_FAILED",
            "raw_message": str((result or {}).get("message") or result)[:180],
        }
    candles = _normalise_candles(result.get("candles") or [])
    if not candles:
        return {"success": False, "message": "REAL_PREMIUM_ZERO_OPTION_CANDLES"}
    return {
        "success": True,
        "candles": candles,
        "request_mode": "ZERODHA_ACTIVE_CONTRACT_HISTORICAL",
    }


def _entry_from_contract(
    broker_name,
    instrument,
    entry_time,
    contract,
    candle_result,
):
    bars, keys = core._build_option_bars(candle_result.get("candles") or [])
    signal_minute = core._epoch_minute(entry_time)
    if signal_minute is None or not keys:
        return {"success": False, "message": "REAL_PREMIUM_ENTRY_TIME_NOT_FOUND"}

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
    lot_size = max(1, _int(contract.get("lot_size"), 1))
    routes.LOT_SIZES[str(instrument).upper()] = lot_size
    broker = str(broker_name or "").lower()

    return {
        "success": True,
        "entry_price": round(float(entry_bar["open"]), 2),
        "entry_time": entry_bar["time"],
        "entry_minute": entry_minute,
        "bars": bars,
        "bar_keys": keys,
        "symbol": contract.get("symbol"),
        "instrument_key": str(
            contract.get("instrument_key")
            or contract.get("token")
            or ""
        ),
        "expiry": contract.get("expiry"),
        "strike": contract.get("strike"),
        "option_type": contract.get("option_type"),
        "lot_size": lot_size,
        "expired_contract": False,
        "request_mode": candle_result.get("request_mode"),
        "premium_source": f"REAL_{broker.upper()}_OPTION_OHLC_1M_V2",
        "premium_model": GENERIC_REAL_MODEL,
        "selected_broker": broker,
        "execution_model": "NEXT_OPTION_CANDLE_OPEN",
    }


def _selected_broker_prepare_entry(
    broker_name,
    obj,
    instrument,
    date_str,
    entry_time,
    spot_price,
    side,
):
    broker = str(broker_name or "").lower().strip()
    if broker == "upstox":
        result = core._real_premium_prepare_entry(
            broker_name=broker,
            obj=obj,
            instrument=instrument,
            date_str=date_str,
            entry_time=entry_time,
            spot_price=spot_price,
            side=side,
        )
        if isinstance(result, dict):
            result["selected_broker"] = broker
            result["premium_model"] = GENERIC_REAL_MODEL
        return result

    if broker == "angelone":
        contract = _angel_contract(
            instrument,
            date_str,
            spot_price,
            side,
        )
        if not contract.get("success"):
            return contract
        candle_result = _fetch_angel_candles(obj, contract, date_str)
    elif broker == "zerodha":
        contract = _zerodha_contract(
            obj,
            instrument,
            date_str,
            spot_price,
            side,
        )
        if not contract.get("success"):
            return contract
        candle_result = _fetch_zerodha_candles(obj, contract, date_str)
    else:
        return {
            "success": False,
            "message": "REAL_PREMIUM_SELECTED_BROKER_UNSUPPORTED",
            "selected_broker": broker,
        }

    if not candle_result.get("success"):
        return {**candle_result, "contract": contract, "selected_broker": broker}

    return _entry_from_contract(
        broker_name=broker,
        instrument=instrument,
        entry_time=entry_time,
        contract=contract,
        candle_result=candle_result,
    )


def apply_multi_broker_real_premium_patch() -> None:
    if getattr(routes, "_okai_multi_broker_real_premium_v2", False):
        return

    # prepare_real_option_premium_patch() first installs the Upstox helper. Keep
    # that helper as the Upstox branch and replace only the runtime dispatcher.
    routes._okai_real_premium_prepare_entry = _selected_broker_prepare_entry
    routes._okai_real_option_premium_model = GENERIC_REAL_MODEL
    routes._okai_multi_broker_real_premium_v2 = True

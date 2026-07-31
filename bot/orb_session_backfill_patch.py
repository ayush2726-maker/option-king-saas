"""Late-start ORB session backfill for Angel One, Zerodha and Upstox.

The live engine can be started after the opening-range window.  The strategy still
needs the current session's 09:15-09:30 IST candles, otherwise ORB remains 0/0.
This patch validates the returned dataframe, performs one dedicated broker
backfill when the opening range is absent, caches the recovered opening candles,
and merges them into the live dataframe before indicators and scoring run.

The patch does not manufacture ORB levels.  If the broker cannot provide the
morning candles, the existing unavailable/neutral ORB handling remains active.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))
ORB_START_MINUTE = 9 * 60 + 15
ORB_END_MINUTE = 9 * 60 + 30
ORB_FETCH_END_MINUTE = 9 * 60 + 31
MIN_RETRY_SECONDS = 120.0

_ORB_CACHE: Dict[Tuple[str, str, str], Any] = {}
_LAST_ATTEMPT: Dict[Tuple[str, str, str], float] = {}
_LOCK = threading.RLock()


def _now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


def _safe_timestamp(value: Any):
    import pandas as pd

    if value is None:
        return pd.NaT

    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            absolute = abs(float(value))
            if absolute >= 1e14:
                stamp = pd.to_datetime(value, unit="us", errors="coerce", utc=True)
            elif absolute >= 1e11:
                stamp = pd.to_datetime(value, unit="ms", errors="coerce", utc=True)
            elif absolute >= 1e9:
                stamp = pd.to_datetime(value, unit="s", errors="coerce", utc=True)
            else:
                stamp = pd.to_datetime(value, errors="coerce")
        else:
            stamp = pd.to_datetime(value, errors="coerce")
    except Exception:
        return pd.NaT

    if pd.isna(stamp):
        return pd.NaT

    try:
        if getattr(stamp, "tzinfo", None) is None:
            return stamp.tz_localize("Asia/Kolkata")
        return stamp.tz_convert("Asia/Kolkata")
    except Exception:
        try:
            stamp = pd.to_datetime(value, errors="coerce", utc=True)
            if pd.isna(stamp):
                return pd.NaT
            return stamp.tz_convert("Asia/Kolkata")
        except Exception:
            return pd.NaT


def _normalise_frame(df):
    import pandas as pd

    if df is None:
        return None
    try:
        if df.empty or "time" not in df.columns:
            return None
    except Exception:
        return None

    out = df.copy()
    out["time"] = out["time"].map(_safe_timestamp)
    out = out.dropna(subset=["time"])

    for column in ("open", "high", "low", "close", "volume"):
        if column not in out.columns:
            out[column] = 0.0
        out[column] = pd.to_numeric(out[column], errors="coerce")

    out = out.dropna(subset=["open", "high", "low", "close"])
    if out.empty:
        return None

    out = (
        out.sort_values("time")
        .drop_duplicates(subset=["time"], keep="last")
        .reset_index(drop=True)
    )
    return out


def _latest_session_frame(df):
    normalised = _normalise_frame(df)
    if normalised is None or normalised.empty:
        return None

    dates = normalised["time"].map(lambda value: value.date())
    latest_day = dates.max()
    return normalised.loc[dates == latest_day].reset_index(drop=True)


def _orb_rows(df, start_minute=ORB_START_MINUTE, end_minute=ORB_END_MINUTE):
    session = _latest_session_frame(df)
    if session is None or session.empty:
        return None

    minutes = session["time"].map(lambda value: value.hour * 60 + value.minute)
    if int(minutes.max()) < int(end_minute):
        return None

    mask = minutes.ge(int(start_minute)) & minutes.le(int(end_minute))
    rows = session.loc[mask].copy()
    return rows if not rows.empty else None


def calculate_orb_levels_resilient(
    df,
    start_minute: int = ORB_START_MINUTE,
    end_minute: int = ORB_END_MINUTE,
):
    """Return the latest session's completed ORB high/low using robust IST parsing."""
    try:
        rows = _orb_rows(df, start_minute=start_minute, end_minute=end_minute)
        if rows is None or rows.empty:
            return 0.0, 0.0

        orb_high = float(rows["high"].max())
        orb_low = float(rows["low"].min())
        if orb_high <= 0 or orb_low <= 0 or orb_high < orb_low:
            return 0.0, 0.0
        return orb_high, orb_low
    except Exception:
        return 0.0, 0.0


def _has_orb(df) -> bool:
    high, low = calculate_orb_levels_resilient(df)
    return bool(high > 0 and low > 0 and high >= low)


def _merge_frames(primary, extra):
    import pandas as pd

    first = _normalise_frame(primary)
    second = _normalise_frame(extra)

    if first is None:
        return second
    if second is None:
        return first

    merged = pd.concat([second, first], ignore_index=True, sort=False)
    merged = (
        merged.sort_values("time")
        .drop_duplicates(subset=["time"], keep="last")
        .reset_index(drop=True)
    )
    return merged


def _cache_key(broker_name: str, instrument: Any, now_ist: datetime) -> Tuple[str, str, str]:
    return (
        str(broker_name or "").strip().lower(),
        str(instrument or "").strip(),
        now_ist.strftime("%Y-%m-%d"),
    )


def _cache_orb(key, df) -> None:
    rows = _orb_rows(df)
    if rows is None or rows.empty:
        return
    with _LOCK:
        _ORB_CACHE[key] = rows.copy()


def _cached_orb(key):
    with _LOCK:
        cached = _ORB_CACHE.get(key)
        return cached.copy() if cached is not None else None


def _may_attempt(key, now_monotonic: Optional[float] = None) -> bool:
    current = time.monotonic() if now_monotonic is None else float(now_monotonic)
    with _LOCK:
        previous = float(_LAST_ATTEMPT.get(key, 0.0))
        if previous and current - previous < MIN_RETRY_SECONDS:
            return False
        _LAST_ATTEMPT[key] = current
        return True


def _frame_from_rows(rows: Iterable[Any]):
    import pandas as pd

    rows = list(rows or [])
    if not rows:
        return None

    first = rows[0]
    if isinstance(first, dict):
        frame = pd.DataFrame(rows)
        if "time" not in frame.columns and "date" in frame.columns:
            frame = frame.rename(columns={"date": "time"})
        if "time" not in frame.columns and "timestamp" in frame.columns:
            frame = frame.rename(columns={"timestamp": "time"})
    else:
        width = max(len(row) for row in rows if isinstance(row, (list, tuple)))
        if width < 5:
            return None
        columns = ["time", "open", "high", "low", "close", "volume", "oi"]
        frame = pd.DataFrame(rows, columns=columns[:width])

    if "volume" not in frame.columns:
        frame["volume"] = 0.0

    required = ["time", "open", "high", "low", "close", "volume"]
    if any(column not in frame.columns for column in required):
        return None
    return _normalise_frame(frame[required])


def _opening_window_bounds(now_ist: datetime) -> Tuple[datetime, datetime]:
    start = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    end_hour, end_minute = divmod(ORB_FETCH_END_MINUTE, 60)
    end = now_ist.replace(
        hour=end_hour,
        minute=end_minute,
        second=0,
        microsecond=0,
    )
    return start, min(now_ist, end)


def _market_is_past_orb(now_ist: datetime) -> bool:
    return now_ist.hour * 60 + now_ist.minute >= ORB_END_MINUTE


def _fetch_angel_opening_frame(obj, token: str, exchange: str, now_ist: datetime):
    start, end = _opening_window_bounds(now_ist)
    response = obj.getCandleData(
        {
            "exchange": exchange,
            "symboltoken": str(token),
            "interval": "ONE_MINUTE",
            "fromdate": start.strftime("%Y-%m-%d %H:%M"),
            "todate": end.strftime("%Y-%m-%d %H:%M"),
        }
    )
    if not isinstance(response, dict) or response.get("status") is False:
        return None
    return _frame_from_rows(response.get("data", []) or [])


def ensure_angel_orb(
    df,
    obj,
    token: str,
    exchange: str = "NSE",
    *,
    now_ist: Optional[datetime] = None,
):
    """Merge Angel's opening candles when a late-start dataframe lacks them."""
    current = now_ist or _now_ist()
    key = _cache_key("angelone", f"{exchange}:{token}", current)

    if _has_orb(df):
        _cache_orb(key, df)
        return _normalise_frame(df)

    cached = _cached_orb(key)
    merged = _merge_frames(df, cached)
    if _has_orb(merged):
        return merged

    if not _market_is_past_orb(current) or not _may_attempt(key):
        return merged if merged is not None else df

    try:
        opening = _fetch_angel_opening_frame(obj, token, exchange, current)
    except Exception:
        opening = None

    merged = _merge_frames(merged, opening)
    if _has_orb(merged):
        _cache_orb(key, merged)
    return merged if merged is not None else df


def _fetch_multi_opening_frame(
    broker_name: str,
    broker_obj,
    underlying: str,
    now_ist: datetime,
    *,
    upstox_keys: Dict[str, Any],
    zerodha_tokens: Dict[str, Any],
):
    broker = str(broker_name or "").strip().lower()
    today = now_ist.strftime("%Y-%m-%d")
    start, end = _opening_window_bounds(now_ist)

    if broker == "upstox":
        key = upstox_keys[underlying]
        result = broker_obj.get_candles(
            symbol=key,
            interval="1m",
            from_date=today,
            to_date=today,
            exchange="BSE_INDEX" if underlying == "SENSEX" else "NSE_INDEX",
        )
    elif broker == "zerodha":
        token = zerodha_tokens[underlying]
        result = broker_obj.get_candles(
            symbol=token,
            interval="minute",
            from_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            to_date=end.strftime("%Y-%m-%d %H:%M:%S"),
            exchange="BSE" if underlying == "SENSEX" else "NSE",
        )
    else:
        return None

    if not isinstance(result, dict) or not result.get("success"):
        return None
    return _frame_from_rows(result.get("candles", []) or [])


def ensure_multi_orb(
    df,
    broker_name: str,
    broker_obj,
    underlying: str,
    *,
    upstox_keys: Dict[str, Any],
    zerodha_tokens: Dict[str, Any],
    now_ist: Optional[datetime] = None,
):
    """Merge opening candles for Zerodha/Upstox when the bot starts late."""
    current = now_ist or _now_ist()
    broker = str(broker_name or "").strip().lower()
    instrument = (
        upstox_keys.get(underlying)
        if broker == "upstox"
        else zerodha_tokens.get(underlying)
    )
    key = _cache_key(broker, instrument or underlying, current)

    if _has_orb(df):
        _cache_orb(key, df)
        return _normalise_frame(df)

    cached = _cached_orb(key)
    merged = _merge_frames(df, cached)
    if _has_orb(merged):
        return merged

    if not _market_is_past_orb(current) or not _may_attempt(key):
        return merged if merged is not None else df

    try:
        opening = _fetch_multi_opening_frame(
            broker,
            broker_obj,
            underlying,
            current,
            upstox_keys=upstox_keys,
            zerodha_tokens=zerodha_tokens,
        )
    except Exception:
        opening = None

    merged = _merge_frames(merged, opening)
    if _has_orb(merged):
        _cache_orb(key, merged)
    return merged if merged is not None else df


def apply_orb_session_backfill_patch() -> None:
    from bot import angel_fetcher as fetcher

    if getattr(fetcher, "_okai_orb_session_backfill_v1", False):
        return

    original_get_candles = fetcher.get_candles
    original_get_candles_multi = fetcher.get_candles_multi

    def get_candles_with_orb_backfill(
        obj,
        token: str,
        interval: str = "ONE_MINUTE",
        exchange: str = "NSE",
    ):
        df = original_get_candles(
            obj,
            token,
            interval=interval,
            exchange=exchange,
        )
        if str(interval or "").upper() != "ONE_MINUTE":
            return df
        return ensure_angel_orb(
            df,
            obj,
            token,
            exchange,
        )

    def get_candles_multi_with_orb_backfill(
        broker_name,
        broker_obj,
        underlying,
    ):
        df = original_get_candles_multi(
            broker_name,
            broker_obj,
            underlying,
        )
        return ensure_multi_orb(
            df,
            broker_name,
            broker_obj,
            underlying,
            upstox_keys=fetcher.UPSTOX_INDEX_KEYS,
            zerodha_tokens=fetcher.ZERODHA_INDEX_TOKENS,
        )

    fetcher.calculate_orb_levels = calculate_orb_levels_resilient
    fetcher.get_candles = get_candles_with_orb_backfill
    fetcher.get_candles_multi = get_candles_multi_with_orb_backfill
    fetcher._okai_orb_session_backfill_v1 = True

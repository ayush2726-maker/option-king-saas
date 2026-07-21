"""Angel index historical-token and empty-success compatibility patch.

Angel uses different index tokens for historical candles than the short tokens
commonly used by the real-time feed. NIFTY/BANKNIFTY historical requests must
prefer 99926000/99926009; SENSEX uses 99919000. This module intentionally
changes only the backtest routes snapshot, not the live websocket/LTP tokens.
"""

import time

from backtest import live_frequency_portfolio_patch as live_patch
from backtest import routes


ANGEL_HISTORICAL_INDEX_TOKENS = {
    "NIFTY": "99926000",
    "BANKNIFTY": "99926009",
    "SENSEX": "99919000",
}

_ANGEL_LEGACY_INDEX_TOKENS = {
    "99926000": "26000",
    "99926009": "26009",
}

_APPLIED = False


def _normalise_rows(response):
    if not isinstance(response, dict):
        return []
    data = response.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        nested = data.get("candles") or data.get("data") or []
        return nested if isinstance(nested, list) else []
    return []


def _token_candidates(primary_token):
    primary = str(primary_token or "").strip()
    candidates = [primary] if primary else []
    legacy = _ANGEL_LEGACY_INDEX_TOKENS.get(primary)
    if legacy and legacy not in candidates:
        candidates.append(legacy)
    return candidates


def _historical_rows_with_index_tokens(obj, params):
    """Serialize requests, prefer historical tokens and explain empty success."""
    base_params = dict(params or {})
    candidates = _token_candidates(base_params.get("symboltoken"))
    if not candidates:
        candidates = [str(base_params.get("symboltoken") or "")]

    last_error = "Angel historical data unavailable"
    total_attempts = 0

    for candidate_number, token in enumerate(candidates):
        retry_delays = (
            live_patch.ANGEL_HISTORY_RETRY_DELAYS
            if candidate_number == 0
            else (0.0,)
        )

        for delay in retry_delays:
            if delay > 0:
                time.sleep(delay)

            request_params = dict(base_params)
            request_params["symboltoken"] = token
            total_attempts += 1

            with live_patch._ANGEL_HISTORY_LOCK:
                elapsed = time.monotonic() - live_patch._ANGEL_LAST_HISTORY_REQUEST
                wait_for = live_patch.ANGEL_HISTORY_MIN_GAP_SECONDS - elapsed
                if wait_for > 0:
                    time.sleep(wait_for)

                try:
                    response = obj.getCandleData(request_params)
                except Exception as exc:
                    response = None
                    last_error = f"{exc.__class__.__name__}: {str(exc)[:180]}"
                finally:
                    live_patch._ANGEL_LAST_HISTORY_REQUEST = time.monotonic()

            rows = _normalise_rows(response)
            if (
                isinstance(response, dict)
                and response.get("status") is not False
                and rows
            ):
                return rows, total_attempts

            if isinstance(response, dict):
                message = str(
                    response.get("message")
                    or response.get("errorcode")
                    or "NO_MESSAGE"
                )[:120]
                if response.get("status") is not False and not rows:
                    last_error = (
                        "EMPTY_DATA"
                        f" exchange={request_params.get('exchange')}"
                        f" token={token}"
                        f" interval={request_params.get('interval')}"
                        f" message={message}"
                    )
                else:
                    last_error = f"API_ERROR token={token} message={message}"
            elif response is not None:
                last_error = str(response)[:240]

    raise RuntimeError(
        "ANGEL_HISTORY_RETRY_EXHAUSTED: " + str(last_error)[:200]
    )


def apply_angel_historical_index_patch():
    global _APPLIED
    if _APPLIED:
        return

    routes.INDEX_TOKENS = {
        **dict(getattr(routes, "INDEX_TOKENS", {}) or {}),
        **ANGEL_HISTORICAL_INDEX_TOKENS,
    }
    live_patch._angel_historical_rows = _historical_rows_with_index_tokens
    _APPLIED = True

"""Keep AUTO real-premium backtests honest on zero-trade days.

The single-index real-premium engine correctly returns ``success=False`` when a
valid strategy candidate exists but the exact option contract/OHLC cannot be
loaded.  The AUTO combiner historically swallowed those failures, returned
``success=True`` with zero trades, and Monthly/Range then displayed the day as
FLAT.  That makes a data failure look like a genuine no-signal day.

This final wrapper preserves genuine zero-trade days, but converts hidden AUTO
data failures into an explicit failed day so Monthly/Range mark it SKIPPED.  It
also attaches a readable reason to genuine flat days.
"""

from __future__ import annotations

import copy

from backtest import routes


_DATA_FAILURE_TOKENS = (
    "REAL_PREMIUM",
    "UPSTOX_PLUS",
    "OPTION_CONTRACT",
    "OPTION_CANDLES",
    "HISTORICAL_DATA",
    "NO DATA",
    "NO_DATA",
    "INSUFFICIENT CANDLE",
    "BROKER LOGIN",
    "INVALID INSTRUMENT",
    "UDAPI",
)


def _is_data_failure(message: object) -> bool:
    text = str(message or "").upper()
    return bool(text) and any(token in text for token in _DATA_FAILURE_TOKENS)


def _failure_rows(result: dict) -> list[dict]:
    per_instrument = result.get("per_instrument")
    if not isinstance(per_instrument, dict):
        return []

    failures: list[dict] = []
    for instrument, info in per_instrument.items():
        if not isinstance(info, dict) or info.get("success") is not False:
            continue
        message = str(info.get("message") or "BACKTEST_DATA_UNAVAILABLE")
        if not _is_data_failure(message):
            continue
        failures.append({
            "instrument": str(instrument or "").upper(),
            "message": message[:240],
        })
    return failures


def _score_text(result: dict) -> str:
    maximum = result.get("debug_max_score")
    if maximum is None:
        return "max score unavailable"
    try:
        return f"max score {float(maximum):.0f}/82"
    except Exception:
        return f"max score {maximum}/82"


def apply_auto_real_premium_integrity_patch() -> None:
    if getattr(routes, "_okai_auto_real_premium_integrity_v1", False):
        return

    original_auto = routes._okai_run_auto_index_backtest

    def honest_auto_result(*args, **kwargs):
        raw = original_auto(*args, **kwargs)
        if not isinstance(raw, dict) or not raw.get("success"):
            return raw

        output = copy.deepcopy(raw)
        trades = int(
            output.get("total_trades")
            or len(output.get("trades") or [])
            or 0
        )
        failures = _failure_rows(output)

        if trades == 0 and failures:
            short = "; ".join(
                f"{row['instrument']}: {row['message']}"
                for row in failures[:3]
            )
            output.update({
                "success": False,
                "message": "AUTO_REAL_PREMIUM_DATA_INCOMPLETE: " + short,
                "no_trade_reason": "DATA_INCOMPLETE_NOT_A_GENUINE_FLAT_DAY",
                "auto_data_failures": failures,
                "auto_integrity_model": "AUTO_REAL_PREMIUM_INTEGRITY_V1",
            })
            summary = dict(output.get("summary") or {})
            summary.update({
                "no_trade_reason": output["no_trade_reason"],
                "auto_data_failures": failures,
            })
            output["summary"] = summary
            return output

        if trades == 0:
            reason = (
                "NO_ELIGIBLE_82_PLUS_SETUP: "
                + _score_text(output)
                + "; VWAP + Supertrend + EMA direction must also agree."
            )
            output["no_trade_reason"] = reason
            summary = dict(output.get("summary") or {})
            summary["no_trade_reason"] = reason
            output["summary"] = summary

        if failures:
            output["partial_data_warning"] = failures

        output["auto_integrity_model"] = "AUTO_REAL_PREMIUM_INTEGRITY_V1"
        return output

    routes._okai_run_auto_index_backtest = honest_auto_result
    routes._okai_auto_real_premium_integrity_v1 = True

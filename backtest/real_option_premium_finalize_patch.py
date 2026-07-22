"""Final result routing and metadata for real option-premium backtests.

Install this after all other backtest wrappers. It keeps Daily, Monthly and Date
Range results honest: REAL means every Normal/AUTO trade used an exact option
contract and real one-minute option OHLC. There is no synthetic fallback.

Hero Zero/Combined remain explicitly unavailable in REAL mode until their
low-premium contract-selection path is migrated; returning an error is safer
than mixing estimated expiry-gamma P&L into a result labelled REAL.
"""

from __future__ import annotations

import copy

from backtest import range_routes
from backtest import routes
from backtest.real_option_contract_resolution_patch import (
    apply_real_option_contract_resolution_patch,
)
from backtest.real_option_premium_patch import REAL_PREMIUM_MODEL


REAL_NOTE = (
    "Signals use real one-minute index candles. Entry, SL, profit lock and exit "
    "use the exact selected option contract's real one-minute OHLC candles. "
    "No estimated-premium fallback is allowed."
)


def _annotate(result):
    if not isinstance(result, dict):
        return result

    output = result
    output["premium_mode"] = "REAL"
    output["premium_model"] = REAL_PREMIUM_MODEL
    output["premium_data"] = "EXACT_OPTION_CONTRACT_OHLC_1M"
    output["estimated_premium_fallback"] = False
    output["note"] = REAL_NOTE

    summary = dict(output.get("summary") or {})
    summary.update({
        "premium_mode": "REAL",
        "premium_model": REAL_PREMIUM_MODEL,
        "premium_data": "EXACT_OPTION_CONTRACT_OHLC_1M",
        "estimated_premium_fallback": False,
        "note": REAL_NOTE,
    })
    output["summary"] = summary

    auto_scan = dict(output.get("auto_scan") or {})
    if auto_scan:
        auto_scan.update({
            "premium_mode": "REAL",
            "premium_model": REAL_PREMIUM_MODEL,
        })
        output["auto_scan"] = auto_scan

    return output


def _unsupported_strategy_result(strategy_mode):
    selected = str(strategy_mode or "NORMAL").upper()
    return {
        "success": False,
        "message": (
            "REAL_PREMIUM_HERO_ZERO_NOT_ENABLED: "
            "Abhi REAL premium mode NORMAL/AUTO ke liye active hai. "
            "Hero Zero/Combined ko estimated gamma ke saath mix nahi kiya gaya."
        ),
        "strategy_mode": selected,
        "premium_mode": "REAL",
        "premium_model": REAL_PREMIUM_MODEL,
        "estimated_premium_fallback": False,
    }


def finalize_real_option_premium_patch():
    if getattr(routes, "_okai_real_option_premium_final_v1", False):
        return

    # Recent completed dates may still use a currently active option contract and
    # therefore do not need the Plus-only expired endpoints. Older contracts do.
    apply_real_option_contract_resolution_patch()

    original_run_mode = routes._okai_run_backtest_mode

    def real_run_mode(*args, **kwargs):
        broker_name = kwargs.get("broker_name")
        strategy_mode = kwargs.get("strategy_mode", "NORMAL")
        if broker_name is None and len(args) >= 1:
            broker_name = args[0]
        if "strategy_mode" not in kwargs and len(args) >= 9:
            strategy_mode = args[8]

        if str(broker_name or "").lower() != "upstox":
            return {
                "success": False,
                "message": "REAL_PREMIUM_REQUIRES_UPSTOX_BROKER",
                "premium_mode": "REAL",
                "premium_model": REAL_PREMIUM_MODEL,
                "estimated_premium_fallback": False,
            }

        if str(strategy_mode or "NORMAL").upper() != "NORMAL":
            return _unsupported_strategy_result(strategy_mode)

        return _annotate(original_run_mode(*args, **kwargs))

    routes._okai_run_backtest_mode = real_run_mode

    original_monthly_sync = routes._okai_run_monthly_backtest_sync

    def real_monthly_sync(*args, **kwargs):
        return _annotate(original_monthly_sync(*args, **kwargs))

    routes._okai_run_monthly_backtest_sync = real_monthly_sync

    original_range_update = range_routes._update_job

    def real_range_update(job_id, **updates):
        if isinstance(updates.get("result"), dict):
            updates = dict(updates)
            updates["result"] = _annotate(copy.deepcopy(updates["result"]))
        original_range_update(job_id, **updates)

    range_routes._update_job = real_range_update

    routes._okai_real_option_premium_final_v1 = True
    range_routes._okai_real_option_premium_final_v1 = True

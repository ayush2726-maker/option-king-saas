"""Final routing and metadata for selected-broker real-premium backtests.

Daily, Monthly and Date Range use the same broker selected for PAPER/LIVE.
Upstox can use active and expired-instrument APIs. Angel One and Zerodha use
exact active-contract one-minute OHLC; dates whose required contract is already
expired are explicitly SKIPPED rather than estimated or silently rerouted.
"""

from __future__ import annotations

import copy

from backtest import range_routes
from backtest import routes
from backtest.auto_real_premium_integrity_patch import (
    apply_auto_real_premium_integrity_patch,
)
from backtest.multi_broker_real_premium_patch import (
    GENERIC_REAL_MODEL,
    SUPPORTED_REAL_BROKERS,
    apply_multi_broker_real_premium_patch,
)
from backtest.real_option_atr_patch import apply_real_option_atr_patch
from backtest.real_option_contract_resolution_patch import (
    apply_real_option_contract_resolution_patch,
)


REAL_NOTE = (
    "Signals use the selected broker's real index candles. Entry, option ATR "
    "stop, profit lock and exit use the exact selected option contract's real "
    "one-minute OHLC candles. No estimated-premium or alternate-broker fallback."
)


def _annotate(result, broker_name=None):
    if not isinstance(result, dict):
        return result

    output = result
    broker = str(
        broker_name
        or output.get("selected_broker")
        or output.get("broker_name")
        or ""
    ).lower().strip()

    output["premium_mode"] = "REAL"
    output["premium_model"] = GENERIC_REAL_MODEL
    output["premium_data"] = "EXACT_OPTION_CONTRACT_OHLC_1M"
    output["option_atr_model"] = "CAUSAL_REAL_OPTION_ATR14"
    output["estimated_premium_fallback"] = False
    output["alternate_broker_fallback"] = False
    output["note"] = REAL_NOTE
    if broker:
        output["selected_broker"] = broker
        output["broker_name"] = broker

    summary = dict(output.get("summary") or {})
    summary.update({
        "premium_mode": "REAL",
        "premium_model": GENERIC_REAL_MODEL,
        "premium_data": "EXACT_OPTION_CONTRACT_OHLC_1M",
        "option_atr_model": "CAUSAL_REAL_OPTION_ATR14",
        "estimated_premium_fallback": False,
        "alternate_broker_fallback": False,
        "note": REAL_NOTE,
    })
    if broker:
        summary["selected_broker"] = broker
    output["summary"] = summary

    auto_scan = dict(output.get("auto_scan") or {})
    if auto_scan:
        auto_scan.update({
            "premium_mode": "REAL",
            "premium_model": GENERIC_REAL_MODEL,
            "selected_broker": broker or auto_scan.get("selected_broker"),
        })
        output["auto_scan"] = auto_scan

    return output


def _unsupported_strategy_result(strategy_mode, broker_name=None):
    selected = str(strategy_mode or "NORMAL").upper()
    broker = str(broker_name or "").lower().strip()
    return {
        "success": False,
        "message": (
            "REAL_PREMIUM_HERO_ZERO_NOT_ENABLED: "
            "Abhi REAL premium mode NORMAL/AUTO ke liye active hai. "
            "Hero Zero/Combined ko estimated gamma ke saath mix nahi kiya gaya."
        ),
        "strategy_mode": selected,
        "selected_broker": broker or None,
        "premium_mode": "REAL",
        "premium_model": GENERIC_REAL_MODEL,
        "estimated_premium_fallback": False,
        "alternate_broker_fallback": False,
    }


def finalize_real_option_premium_patch():
    if getattr(routes, "_okai_real_option_premium_final_v2", False):
        return

    # prepare_real_option_premium_patch() installed the Upstox helper earlier.
    # Replace its runtime dispatcher now so the compiled strategy asks the exact
    # broker selected by the user for option OHLC.
    apply_multi_broker_real_premium_patch()

    # Upstox-specific resolver still provides expired-instrument support. The
    # runtime dispatcher handles Angel One/Zerodha active contract data.
    apply_real_option_contract_resolution_patch()
    apply_real_option_atr_patch()
    apply_auto_real_premium_integrity_patch()

    original_run_mode = routes._okai_run_backtest_mode

    def real_run_mode(*args, **kwargs):
        broker_name = kwargs.get("broker_name")
        strategy_mode = kwargs.get("strategy_mode", "NORMAL")
        if broker_name is None and len(args) >= 1:
            broker_name = args[0]
        if "strategy_mode" not in kwargs and len(args) >= 9:
            strategy_mode = args[8]

        broker = str(broker_name or "").lower().strip()
        if broker not in SUPPORTED_REAL_BROKERS:
            return {
                "success": False,
                "message": "REAL_PREMIUM_SELECTED_BROKER_UNSUPPORTED",
                "selected_broker": broker or None,
                "premium_mode": "REAL",
                "premium_model": GENERIC_REAL_MODEL,
                "estimated_premium_fallback": False,
                "alternate_broker_fallback": False,
            }

        if str(strategy_mode or "NORMAL").upper() != "NORMAL":
            return _unsupported_strategy_result(strategy_mode, broker)

        return _annotate(
            original_run_mode(*args, **kwargs),
            broker,
        )

    routes._okai_run_backtest_mode = real_run_mode

    original_monthly_sync = routes._okai_run_monthly_backtest_sync

    def real_monthly_sync(*args, **kwargs):
        result = original_monthly_sync(*args, **kwargs)
        broker = None
        if isinstance(result, dict):
            broker = result.get("selected_broker") or result.get("broker_name")
            if not broker:
                for trade in result.get("trades") or []:
                    broker = trade.get("selected_broker") or trade.get("broker_name")
                    if broker:
                        break
        return _annotate(result, broker)

    routes._okai_run_monthly_backtest_sync = real_monthly_sync

    original_range_update = range_routes._update_job

    def real_range_update(job_id, **updates):
        if isinstance(updates.get("result"), dict):
            updates = dict(updates)
            updates["result"] = _annotate(copy.deepcopy(updates["result"]))
        original_range_update(job_id, **updates)

    range_routes._update_job = real_range_update

    routes._okai_real_option_premium_final_v1 = True
    routes._okai_real_option_premium_final_v2 = True
    range_routes._okai_real_option_premium_final_v1 = True
    range_routes._okai_real_option_premium_final_v2 = True

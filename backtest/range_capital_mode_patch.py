"""Add selectable compounding/fixed sizing to the date-range backtest.

The original range worker already owns job progress, day/month/year aggregation,
notifications and drawdown reporting.  This patch keeps that implementation and
changes only how capital is supplied to the final daily strategy engine:

* COMPOUNDING: each day sizes from the previous day's closing equity.
* FIXED: every day sizes from the user-entered base capital, while the reported
  equity curve still accumulates all daily P&L normally.

A thread-local context keeps concurrent users and ordinary daily/monthly
backtests isolated from one another.
"""

from __future__ import annotations

import copy
import threading

from backtest import range_routes
from backtest import routes


_ALLOWED_CAPITAL_MODES = {"COMPOUNDING", "FIXED"}
_CONTEXT = threading.local()


def _normalise_capital_mode(value: object) -> str:
    text = str(value or "COMPOUNDING").upper().strip().replace("-", "_")
    aliases = {
        "CONTINUOUS": "COMPOUNDING",
        "CONTINUOUS_COMPOUNDING": "COMPOUNDING",
        "COMPOUND": "COMPOUNDING",
        "EQUITY_COMPOUNDING": "COMPOUNDING",
        "FIXED_CAPITAL": "FIXED",
        "FIXED_DAILY_CAPITAL": "FIXED",
        "NO_COMPOUNDING": "FIXED",
    }
    text = aliases.get(text, text)
    return text if text in _ALLOWED_CAPITAL_MODES else "COMPOUNDING"


def _annotate_result(result: dict, context: dict) -> dict:
    mode = context["capital_mode"]
    fixed = mode == "FIXED"
    base_capital = round(float(context["base_capital"]), 2)

    result["capital_mode"] = mode
    result["sizing_capital"] = base_capital if fixed else "CURRENT_EQUITY"

    position_sizing = dict(result.get("position_sizing") or {})
    position_sizing.update(
        {
            "mode": (
                "FIXED_BASE_CAPITAL_ALLOCATION"
                if fixed
                else "CONTINUOUS_CAPITAL_BASED_ALLOCATION"
            ),
            "capital_mode": mode,
            "equity_compounding": not fixed,
            "fixed_daily_sizing_capital": base_capital if fixed else None,
            "whole_lots_only": True,
            "auto_slot_1_percent": 50,
            "auto_slot_2_percent": 40,
            "reserve_percent": 10,
        }
    )
    result["position_sizing"] = position_sizing

    summary = dict(result.get("summary") or {})
    summary["capital_mode"] = mode
    summary["sizing_capital"] = base_capital if fixed else "CURRENT_EQUITY"
    result["summary"] = summary

    if fixed:
        result["note"] = (
            "Fixed Capital mode sizes every trading day from the same entered "
            f"base capital Rs {base_capital}. Day, month and year P&L still build "
            "one cumulative equity curve, so ending capital and drawdown remain "
            "continuous and directly comparable."
        )
    else:
        result["note"] = (
            "Continuous Compounding mode sizes each day from the previous day's "
            "closing equity. Day, month and year breakdowns are views of the same run."
        )
    return result


def apply_range_capital_mode_patch() -> None:
    if getattr(range_routes, "_okai_range_capital_mode_v1", False):
        return

    original_normalize = range_routes._normalize_request
    original_worker = range_routes._range_worker
    original_update_job = range_routes._update_job
    original_run_mode = routes._okai_run_backtest_mode

    def normalize_with_capital_mode(body: dict | None) -> dict:
        payload = original_normalize(body)
        source = dict(body or {})
        payload["capital_mode"] = _normalise_capital_mode(
            source.get("capital_mode") or source.get("sizing_mode")
        )
        return payload

    def run_mode_with_range_capital(*args, **kwargs):
        context = getattr(_CONTEXT, "value", None)
        if not context:
            return original_run_mode(*args, **kwargs)

        if context["capital_mode"] != "FIXED":
            return original_run_mode(*args, **kwargs)

        call_kwargs = dict(kwargs)
        call_args = list(args)
        base_capital = float(context["base_capital"])

        if "capital" in call_kwargs:
            call_kwargs["capital"] = base_capital
        elif len(call_args) >= 5:
            call_args[4] = base_capital
        else:
            call_kwargs["capital"] = base_capital

        raw = original_run_mode(*call_args, **call_kwargs)
        if not isinstance(raw, dict) or not raw.get("success"):
            return raw

        output = dict(raw)
        day_pnl = float(output.get("total_pnl") or 0.0)
        context["equity"] = float(context["equity"]) + day_pnl

        # The old range worker reads ending_capital to advance its continuous
        # equity curve.  Return accumulated equity here while keeping daily lots
        # based only on base_capital above.
        output["ending_capital"] = round(float(context["equity"]), 2)
        output["range_sizing_capital"] = round(base_capital, 2)
        output["capital_mode"] = "FIXED"
        return output

    def update_job_with_capital_mode(job_id: str, **updates) -> None:
        context = getattr(_CONTEXT, "value", None)
        if context and isinstance(updates.get("result"), dict):
            updates = dict(updates)
            updates["result"] = _annotate_result(
                copy.deepcopy(updates["result"]),
                context,
            )
        original_update_job(job_id, **updates)

    def worker_with_capital_mode(
        job_id: str,
        payload: dict,
        authorization: str | None,
    ) -> None:
        mode = _normalise_capital_mode(payload.get("capital_mode"))
        context = {
            "capital_mode": mode,
            "base_capital": float(payload["capital"]),
            "equity": float(payload["capital"]),
        }
        previous = getattr(_CONTEXT, "value", None)
        _CONTEXT.value = context
        try:
            original_worker(job_id, payload, authorization)
        finally:
            if previous is None:
                try:
                    delattr(_CONTEXT, "value")
                except AttributeError:
                    pass
            else:
                _CONTEXT.value = previous

    range_routes._normalize_request = normalize_with_capital_mode
    range_routes._range_worker = worker_with_capital_mode
    range_routes._update_job = update_job_with_capital_mode
    routes._okai_run_backtest_mode = run_mode_with_range_capital
    range_routes._okai_range_capital_mode_v1 = True

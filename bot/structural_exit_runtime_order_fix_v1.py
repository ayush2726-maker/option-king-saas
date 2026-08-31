"""Ensure structural-exit loss cap and fast opposite exit survive runtime patch order.

This module is imported from bot.__init__ before main imports
apply_structural_exit_v2_patch. It wraps that installer so PAPER and LIVE share
these exit rules after the structural runtime is installed:
- immediate confirmed structural exit at -0.25R or worse;
- normal structural reversal still needs two completed candles;
- re-apply the fast opposite-trend exit wrapper after structural installation so
  a qualified opposite signal can exit without waiting for ATR SL.

Profit-lock/trailing parameters are intentionally unchanged.
"""
from __future__ import annotations

from bot import structural_exit_v2_patch as structural

VERSION = "STRUCTURAL_EXIT_MINUS_025R_ORDER_FIX_V1"
FAST_LOSS_EXIT_R = -0.25


def install_structural_exit_runtime_order_fix_v1() -> None:
    if getattr(structural, "_okai_minus_025r_order_fix_v1", False):
        return

    original_apply = structural.apply_structural_exit_v2_patch

    def apply_with_loss_cap_and_fast_opposite(*args, **kwargs):
        # The structural evaluator reads this module-level value on every exit
        # evaluation, so both PAPER and LIVE use the same -0.25R threshold.
        structural.STRUCTURAL_LOSS_FAST_EXIT_R = FAST_LOSS_EXIT_R
        result = original_apply(*args, **kwargs)

        # structural_exit_v2 installs/replaces runtime._evaluate_exit. Re-apply
        # the opposite-trend wrapper afterwards so a qualified 82+ opposite
        # signal or strong VWAP+EMA+Supertrend flip can still close the trade.
        try:
            from bot import fast_opposite_trend_exit_patch as fast_exit
            # The old applied flag may refer to the evaluator that structural
            # just replaced, so clear it before wrapping the final evaluator.
            from bot import auto_portfolio_runtime as runtime
            runtime._okai_fast_opposite_trend_exit_v1 = False
            fast_exit.apply_fast_opposite_trend_exit_patch()
        except Exception:
            pass
        return result

    structural.apply_structural_exit_v2_patch = apply_with_loss_cap_and_fast_opposite
    structural.STRUCTURAL_LOSS_FAST_EXIT_R = FAST_LOSS_EXIT_R
    structural._okai_minus_025r_order_fix_v1 = True

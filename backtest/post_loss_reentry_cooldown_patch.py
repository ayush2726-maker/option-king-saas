"""Backtest parity for post-ATR-stop re-entry cooldown.

AUTO live/paper now blocks only the stopped index+side for 15 minutes after a
PURE ATR SL. Historical single-index results are filtered with the same rule
before AUTO MAX2 allocation and compounding are calculated.

This is intentionally applied to the raw one-lot trade stream. The AUTO merge
then performs its normal 50%/40% sizing on the filtered candidates, so removed
rapid re-entries do not contribute P&L or consume a portfolio slot.
"""

from copy import deepcopy
from datetime import datetime, timedelta

from backtest import routes


COOLDOWN_MINUTES = 15
COOLDOWN_REASON = "POST_ATR_SL_SAME_SIDE_COOLDOWN_15M"


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def _is_atr_stop(trade):
    return "PURE_ATR_SL" in str(trade.get("reason") or "").upper()


def _side(trade):
    return str(trade.get("side") or "").upper()


def _recalculate(result, trades):
    output = deepcopy(result)
    total_pnl = round(sum(float(row.get("pnl") or 0) for row in trades), 2)
    wins = sum(1 for row in trades if float(row.get("pnl") or 0) >= 0)
    losses = len(trades) - wins
    win_rate = round(wins / len(trades) * 100, 2) if trades else 0.0
    capital = float(output.get("capital") or 0)

    output.update({
        "trades": trades,
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "ending_capital": round(capital + total_pnl, 2),
        "post_loss_reentry_cooldown_minutes": COOLDOWN_MINUTES,
        "post_loss_reentry_model": COOLDOWN_REASON,
    })

    summary = dict(output.get("summary") or {})
    summary.update({
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "net_pnl": total_pnl,
        "ending_capital": output["ending_capital"],
        "post_loss_reentry_cooldown_minutes": COOLDOWN_MINUTES,
    })
    output["summary"] = summary
    return output


def _filter_result(result):
    if not isinstance(result, dict) or not result.get("success"):
        return result

    source = list(result.get("trades") or [])
    if not source:
        output = deepcopy(result)
        output["post_loss_reentry_cooldown_minutes"] = COOLDOWN_MINUTES
        return output

    ordered = sorted(source, key=lambda row: _parse_time(row.get("entry_time")))
    blocked_until = {}
    kept = []
    blocked = []

    for row in ordered:
        trade = dict(row)
        side = _side(trade)
        entry_time = _parse_time(trade.get("entry_time"))
        until = blocked_until.get(side)

        if side in ("CE", "PE") and until is not None and entry_time < until:
            trade["backtest_block_reason"] = COOLDOWN_REASON
            trade["backtest_blocked_until"] = until.isoformat()
            blocked.append(trade)
            continue

        trade["trade_no"] = len(kept) + 1
        kept.append(trade)

        if _is_atr_stop(trade) and side in ("CE", "PE"):
            exit_time = _parse_time(trade.get("exit_time"))
            if exit_time != datetime.min:
                blocked_until[side] = exit_time + timedelta(minutes=COOLDOWN_MINUTES)

    output = _recalculate(result, kept)
    output["post_loss_reentries_blocked"] = len(blocked)
    output["blocked_post_loss_reentries"] = blocked
    return output


def apply_backtest_post_loss_reentry_cooldown_patch():
    if getattr(routes, "_okai_backtest_post_loss_cooldown_v1", False):
        return

    original_single = routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST

    def single_with_cooldown(*args, **kwargs):
        return _filter_result(original_single(*args, **kwargs))

    # AUTO MAX2 reads this module global dynamically for each instrument.
    routes._OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST = single_with_cooldown
    # Direct NIFTY/BANKNIFTY/SENSEX Daily/Monthly routes use this name.
    routes.run_realistic_day_backtest = single_with_cooldown
    routes._okai_backtest_post_loss_cooldown_v1 = True

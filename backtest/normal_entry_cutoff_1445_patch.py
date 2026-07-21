"""Backtest-only 14:45 Normal entry cutoff experiment.

New NORMAL entries at or after 14:45 IST are removed from Daily/AUTO/Monthly
backtest results. Existing positions may continue until their normal exit.
Hero Zero trades remain untouched, including the 14:30-15:00 expiry window.
Paper and Live runtimes are not changed.
"""

from copy import deepcopy
from datetime import datetime, timezone, timedelta

from backtest import routes


NORMAL_ENTRY_CUTOFF_MINUTES = 14 * 60 + 45


def _entry_minutes(value):
    if value is None:
        return None

    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(
                timezone(timedelta(hours=5, minutes=30))
            )
        return parsed.hour * 60 + parsed.minute
    except Exception:
        pass

    # Handles plain HH:MM / HH:MM:SS values.
    try:
        clock = text.split("T")[-1].split(" ")[-1]
        hour_text, minute_text = clock.split(":", 2)[:2]
        return int(hour_text) * 60 + int(minute_text)
    except Exception:
        return None


def _is_hero_zero_trade(trade):
    fields = " ".join(
        str(trade.get(key) or "")
        for key in (
            "strategy_mode",
            "trade_type",
            "mode",
            "reason",
            "entry_reason",
            "exit_reason",
        )
    ).upper()
    return "HERO_ZERO" in fields or "HERO ZERO" in fields


def _trade_pnl(trade):
    try:
        return float(trade.get("pnl") or 0.0)
    except Exception:
        return 0.0


def _apply_cutoff(result, starting_capital):
    if not isinstance(result, dict) or not result.get("success"):
        return result

    output = deepcopy(result)
    original_trades = list(output.get("trades") or [])
    kept = []
    blocked = []

    for trade in original_trades:
        row = dict(trade)
        minutes = _entry_minutes(row.get("entry_time"))
        should_block = (
            not _is_hero_zero_trade(row)
            and minutes is not None
            and minutes >= NORMAL_ENTRY_CUTOFF_MINUTES
        )
        if should_block:
            row["backtest_block_reason"] = "NORMAL_ENTRY_AT_OR_AFTER_1445"
            blocked.append(row)
        else:
            kept.append(row)

    # Preserve results that contain no detailed trades.
    if not original_trades:
        return output

    total_pnl = round(sum(_trade_pnl(row) for row in kept), 2)
    wins = sum(1 for row in kept if _trade_pnl(row) >= 0)
    losses = len(kept) - wins
    win_rate = round(wins / len(kept) * 100, 2) if kept else 0.0

    hero_pnl = round(
        sum(_trade_pnl(row) for row in kept if _is_hero_zero_trade(row)),
        2,
    )
    normal_pnl = round(total_pnl - hero_pnl, 2)

    output.update({
        "trades": kept,
        "total_trades": len(kept),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "normal_pnl": normal_pnl,
        "hero_zero_pnl": hero_pnl,
        "ending_capital": round(float(starting_capital) + total_pnl, 2),
        "normal_entry_cutoff": "14:45 IST",
        "normal_entries_blocked_after_cutoff": len(blocked),
        "blocked_after_1445_trades": blocked,
        "backtest_cutoff_experiment": "NORMAL_NEW_ENTRY_BLOCK_AT_OR_AFTER_14_45",
    })

    summary = dict(output.get("summary") or {})
    summary.update({
        "trades": len(kept),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "net_pnl": total_pnl,
        "normal_pnl": normal_pnl,
        "hero_zero_pnl": hero_pnl,
        "capital": float(starting_capital),
        "ending_capital": output["ending_capital"],
        "normal_entry_cutoff": "14:45 IST",
        "normal_entries_blocked_after_cutoff": len(blocked),
    })
    output["summary"] = summary
    return output


def apply_normal_entry_cutoff_1445_patch():
    if getattr(routes, "_okai_normal_entry_cutoff_1445_v1", False):
        return

    original = routes._okai_run_backtest_mode

    def run_with_1445_cutoff(*args, **kwargs):
        result = original(*args, **kwargs)

        capital = kwargs.get("capital")
        if capital is None and len(args) >= 5:
            capital = args[4]
        try:
            starting_capital = float(capital or 0.0)
        except Exception:
            starting_capital = 0.0

        return _apply_cutoff(result, starting_capital)

    routes._okai_run_backtest_mode = run_with_1445_cutoff
    routes._okai_normal_entry_cutoff_1445_v1 = True

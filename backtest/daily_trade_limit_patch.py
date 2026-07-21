"""Enforce the live default maximum of five entries per trading day.

The limit is applied to every final Daily/AUTO result before Monthly aggregation,
so a monthly run may contain at most five trades for each tested date rather
than five trades for the entire month.
"""

from copy import deepcopy
from datetime import datetime

from backtest import routes


MAX_TRADES_PER_DAY = 5


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def _trade_is_hero(trade):
    text = " ".join(
        str(trade.get(key) or "")
        for key in ("strategy", "strategy_mode", "mode", "symbol", "reason")
    ).upper()
    return "HERO" in text


def _limit_day_result(result):
    if not isinstance(result, dict) or not result.get("success"):
        return result

    output = deepcopy(result)
    original = list(output.get("trades") or [])
    ordered = sorted(
        original,
        key=lambda trade: (
            _parse_time(trade.get("entry_time")),
            int(trade.get("trade_no") or 0),
        ),
    )
    selected = ordered[:MAX_TRADES_PER_DAY]
    dropped = max(0, len(ordered) - len(selected))

    for index, trade in enumerate(selected, start=1):
        trade["trade_no"] = index
        trade["daily_trade_number"] = index
        trade["daily_trade_limit"] = MAX_TRADES_PER_DAY

    total_pnl = round(sum(_f(trade.get("pnl")) for trade in selected), 2)
    wins = sum(1 for trade in selected if _f(trade.get("pnl")) > 0)
    losses = sum(1 for trade in selected if _f(trade.get("pnl")) < 0)
    flats = len(selected) - wins - losses
    capital = _f(
        output.get("capital", (output.get("summary") or {}).get("capital", 0)),
        0,
    )

    gross = round(
        sum(
            _f(
                trade.get(
                    "gross_pnl",
                    (trade.get("charges") or {}).get("market_gross_pnl", trade.get("pnl")),
                )
            )
            for trade in selected
        ),
        2,
    )
    slippage = round(
        sum(
            _f(
                trade.get(
                    "slippage_cost",
                    (trade.get("charges") or {}).get("slippage_cost", 0),
                )
            )
            for trade in selected
        ),
        2,
    )
    total_charges = round(
        sum(
            _f(
                trade.get(
                    "total_charges",
                    (trade.get("charges") or {}).get("total_charges", 0),
                )
            )
            for trade in selected
        ),
        2,
    )
    brokerage = round(
        sum(_f((trade.get("charges") or {}).get("brokerage", 0)) for trade in selected),
        2,
    )
    statutory = round(max(0.0, total_charges - brokerage), 2)
    normal_pnl = round(
        sum(_f(trade.get("pnl")) for trade in selected if not _trade_is_hero(trade)),
        2,
    )
    hero_pnl = round(
        sum(_f(trade.get("pnl")) for trade in selected if _trade_is_hero(trade)),
        2,
    )

    output.update({
        "trades": selected,
        "total_trades": len(selected),
        "wins": wins,
        "losses": losses,
        "flat_trades": flats,
        "win_rate": round(wins / len(selected) * 100.0, 2) if selected else 0.0,
        "total_pnl": total_pnl,
        "net_pnl": total_pnl,
        "ending_capital": round(capital + total_pnl, 2),
        "normal_pnl": normal_pnl,
        "hero_zero_pnl": hero_pnl,
        "gross_pnl_before_costs": gross,
        "total_slippage_cost": slippage,
        "total_brokerage": brokerage,
        "total_statutory_charges": statutory,
        "total_charges": total_charges,
        "daily_trade_limit": MAX_TRADES_PER_DAY,
        "daily_trade_limit_applied": True,
        "trades_before_daily_limit": len(original),
        "trades_dropped_by_daily_limit": dropped,
        "trade_limit_scope": "PER_TRADING_DAY",
    })

    summary = dict(output.get("summary") or {})
    summary.update({
        "trades": len(selected),
        "wins": wins,
        "losses": losses,
        "flat_trades": flats,
        "win_rate": output["win_rate"],
        "capital": capital,
        "gross_pnl": gross,
        "slippage_cost": slippage,
        "charges": total_charges,
        "net_pnl": total_pnl,
        "ending_capital": output["ending_capital"],
        "normal_pnl": normal_pnl,
        "hero_zero_pnl": hero_pnl,
        "daily_trade_limit": MAX_TRADES_PER_DAY,
        "daily_trade_limit_applied": True,
        "trades_before_daily_limit": len(original),
        "trades_dropped_by_daily_limit": dropped,
        "trade_limit_scope": "PER_TRADING_DAY",
    })
    output["summary"] = summary
    return output


def _wrap(name):
    original = getattr(routes, name, None)
    if not callable(original):
        return False
    marker = f"_okai_daily_limit_wrapped_{name}"
    if getattr(routes, marker, False):
        return False

    def limited(*args, **kwargs):
        return _limit_day_result(original(*args, **kwargs))

    limited.__name__ = getattr(original, "__name__", name)
    limited.__doc__ = getattr(original, "__doc__", None)
    setattr(routes, name, limited)
    setattr(routes, marker, True)
    return True


def apply_daily_trade_limit_patch():
    if getattr(routes, "_okai_backtest_daily_trade_limit_v1", False):
        return

    wrapped = []
    for name in (
        "run_realistic_day_backtest",
        "_okai_run_auto_index_backtest",
        "_okai_run_strategy_day",
        "_okai_run_combined_day",
    ):
        if _wrap(name):
            wrapped.append(name)

    routes._okai_backtest_daily_trade_limit_v1 = True
    routes._okai_backtest_daily_trade_limit_value = MAX_TRADES_PER_DAY
    routes._okai_backtest_daily_trade_limit_wrapped = tuple(wrapped)

"""Shared capital-based lot sizing for paper mode and backtests."""

from __future__ import annotations

import math
from typing import Any, Dict

CAPITAL_USE_FRACTION = 0.90


def calculate_lot_sizing(
    capital: float,
    entry_price: float,
    lot_size: int,
    capital_use_fraction: float = CAPITAL_USE_FRACTION,
) -> Dict[str, Any]:
    """Use whole lots without exceeding the configured capital fraction."""
    try:
        equity = max(0.0, float(capital or 0.0))
        premium = max(0.0, float(entry_price or 0.0))
        lot = max(1, int(lot_size or 1))
        fraction = min(
            1.0,
            max(0.0, float(capital_use_fraction)),
        )
    except (TypeError, ValueError):
        equity = 0.0
        premium = 0.0
        lot = 1
        fraction = CAPITAL_USE_FRACTION

    usable_capital = equity * fraction
    one_lot_cost = premium * lot

    if usable_capital <= 0 or one_lot_cost <= 0:
        lots = 0
    else:
        lots = int(
            math.floor(
                usable_capital / one_lot_cost
            )
        )

    quantity = lots * lot
    capital_used = quantity * premium
    utilization = (
        capital_used / equity * 100.0
        if equity > 0
        else 0.0
    )

    return {
        "capital": round(equity, 2),
        "capital_use_fraction": round(
            fraction,
            4,
        ),
        "usable_capital": round(
            usable_capital,
            2,
        ),
        "entry_price": round(premium, 2),
        "lot_size": lot,
        "one_lot_cost": round(
            one_lot_cost,
            2,
        ),
        "lots": lots,
        "quantity": quantity,
        "capital_used": round(
            capital_used,
            2,
        ),
        "capital_utilization_percent": round(
            utilization,
            2,
        ),
        "affordable": lots >= 1,
    }

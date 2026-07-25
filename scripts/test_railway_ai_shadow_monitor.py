from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone


root = tempfile.mkdtemp(prefix="okai-railway-shadow-")
os.environ["DB_PATH"] = os.path.join(root, "shadow_test.db")

from bot.ai_shadow_monitor import (  # noqa: E402
    ensure_shadow_schema,
    get_shadow_summary,
    observe_shadow_outcomes,
    register_shadow_decision,
)


ensure_shadow_schema()
base = datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc)
snapshot = {
    "symbol": "NIFTY",
    "price": 25000,
    "signal": "CE",
    "signal_direction": "CE",
    "strategy_score": 86,
    "min_strategy_score": 82,
    "server_trade_allowed": True,
    "market_regime": "TRENDING",
    "adx": 29,
    "volume_ratio": 1.3,
    "market_open": True,
    "feed_connected": True,
}
result = {
    "success": True,
    "model_version": "test-v1",
    "decision": "NO_TRADE",
    "confidence": 100,
    "probabilities": {"CE": 10, "PE": 10, "NO_TRADE": 80},
    "reasons": ["DIRECTION_CONFLICT"],
}

decision_id = register_shadow_decision(
    1,
    snapshot,
    result,
    now=base,
)
assert decision_id

loss_snapshot = {
    **snapshot,
    "price": 24970,
}
assert observe_shadow_outcomes(
    1,
    loss_snapshot,
    now=base + timedelta(minutes=15),
) == 2
assert observe_shadow_outcomes(
    1,
    loss_snapshot,
    now=base + timedelta(minutes=30),
) == 1

summary = get_shadow_summary(1)
assert summary["location"] == "RAILWAY"
assert summary["trade_blocking"] is False
assert summary["order_execution"] is False
assert summary["summary"]["ai_blocks_that_would_help"] == 1
assert (
    summary["summary"]["estimated_net_benefit_spot_points_15m"]
    == 30.0
)

print("PASS OKAI-RAILWAY-AI-SHADOW-MONITOR-V1")

from backtest import routes
from backtest.post_loss_reentry_cooldown_patch import (
    apply_backtest_post_loss_reentry_cooldown_patch,
)


def test_cooldown_patch_keeps_auto_out_of_single_index_fetch(monkeypatch):
    calls = []

    def fake_single(*args, **kwargs):
        calls.append(("single", args[2]))
        return {
            "success": True,
            "instrument": args[2],
            "capital": args[4],
            "trades": [],
            "total_trades": 0,
            "total_pnl": 0,
            "summary": {},
        }

    def fake_auto(*args, **kwargs):
        calls.append(("auto", args[2]))
        return {
            "success": True,
            "instrument": "AUTO",
            "total_trades": 0,
            "total_pnl": 0,
        }

    def fake_scale(result, capital):
        calls.append(("scale", result.get("instrument")))
        return result

    monkeypatch.setattr(
        routes,
        "_OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST",
        fake_single,
    )
    monkeypatch.setattr(
        routes,
        "_okai_run_auto_index_backtest",
        fake_auto,
    )
    monkeypatch.setattr(
        routes,
        "_okai_scale_trades_to_capital",
        fake_scale,
    )
    monkeypatch.delattr(
        routes,
        "_okai_backtest_post_loss_cooldown_v2",
        raising=False,
    )

    apply_backtest_post_loss_reentry_cooldown_patch()

    result = routes.run_realistic_day_backtest(
        "upstox",
        object(),
        "AUTO",
        "2026-07-01",
        100000,
        82,
        0,
        0,
    )

    assert result["instrument"] == "AUTO"
    assert calls == [("auto", "2026-07-01")]


def test_direct_index_keeps_cooldown_then_capital_scaling(monkeypatch):
    calls = []

    def fake_single(*args, **kwargs):
        calls.append(("single", args[2]))
        return {
            "success": True,
            "instrument": args[2],
            "capital": args[4],
            "trades": [],
            "total_trades": 0,
            "total_pnl": 0,
            "summary": {},
        }

    def fake_scale(result, capital):
        calls.append(("scale", result.get("instrument")))
        result["scaled"] = True
        return result

    monkeypatch.setattr(
        routes,
        "_OKAI_ORIGINAL_SINGLE_INDEX_BACKTEST",
        fake_single,
    )
    monkeypatch.setattr(
        routes,
        "_okai_scale_trades_to_capital",
        fake_scale,
    )
    monkeypatch.delattr(
        routes,
        "_okai_backtest_post_loss_cooldown_v2",
        raising=False,
    )

    apply_backtest_post_loss_reentry_cooldown_patch()

    result = routes.run_realistic_day_backtest(
        "upstox",
        object(),
        "NIFTY",
        "2026-07-01",
        100000,
        82,
        0,
        0,
    )

    assert result["instrument"] == "NIFTY"
    assert result["scaled"] is True
    assert calls == [("single", "NIFTY"), ("scale", "NIFTY")]

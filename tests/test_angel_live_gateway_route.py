from pathlib import Path


def test_angel_auto_live_uses_static_ip_gateway_not_direct_order():
    source = Path("bot/auto_portfolio_runtime.py").read_text(encoding="utf-8")

    assert "def _angel_gateway_live_cycle" in source
    assert '"OWNER_STATIC_IP_LOCAL_GATEWAY"' in source
    assert "_legacy()._manage_live_gateway_entry" in source
    assert (
        'if str(settings.get("trading_mode", "paper")).lower() == "live":'
        in source
    )


def test_gateway_live_sizing_reuses_shared_paper_size_function():
    source = Path("bot/angel_fetcher.py").read_text(encoding="utf-8")

    assert "from bot.auto_portfolio_runtime import _size" in source
    assert "sizing = _size(" in source
    assert 'live_lots = int(sizing["lots"])' in source
    assert 'quantity = int(sizing["qty"])' in source


def test_gateway_does_not_block_live_entries_by_daily_trade_count():
    source = Path("local_gateway/service.py").read_text(encoding="utf-8")

    assert '"reason": "MAX_DAILY_LIVE_TRADES"' not in source
    assert 'payload["daily_trade_limit"] = None' in source


def test_live_and_backtest_daily_trade_count_are_unlimited():
    live_source = Path("bot/paper_unlimited_observation_patch.py").read_text(
        encoding="utf-8"
    )
    backtest_source = Path("backtest/daily_trade_limit_patch.py").read_text(
        encoding="utf-8"
    )
    strategy_source = Path("strategy/routes.py").read_text(encoding="utf-8")

    assert "LIVE_MAX_TRADES_PER_DAY = None" in live_source
    assert 'state["trade_limit_status"] = "DAILY_TRADE_COUNT_UNLIMITED"' in live_source
    assert "MAX_TRADES_PER_DAY = None" in backtest_source
    assert "selected = ordered" in backtest_source
    assert '"daily_trade_limit_applied": False' in backtest_source
    assert '"max_trades_per_day": 0' in strategy_source
    assert '"unlimited_trades": True' in strategy_source

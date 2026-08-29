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

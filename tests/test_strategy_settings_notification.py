from strategy.notifications import settings_saved_message


def _settings(trading_mode):
    return {
        "mode": "default",
        "trading_mode": trading_mode,
        "entry_threshold": 82,
        "sl_percent": 12.0,
        "target_percent": 24.0,
    }


def test_live_confirmation_uses_trading_mode_not_strategy_profile():
    message = settings_saved_message(_settings("live"))

    assert "Trading Mode: <b>LIVE</b>" in message
    assert "Strategy Profile: DEFAULT" in message
    assert "Mode: default" not in message


def test_paper_confirmation_uses_trading_mode_not_strategy_profile():
    message = settings_saved_message(_settings("paper"))

    assert "Trading Mode: <b>PAPER</b>" in message
    assert "Strategy Profile: DEFAULT" in message
    assert "Mode: default" not in message


if __name__ == "__main__":
    test_live_confirmation_uses_trading_mode_not_strategy_profile()
    test_paper_confirmation_uses_trading_mode_not_strategy_profile()
    print("Strategy settings Telegram notifications verified")

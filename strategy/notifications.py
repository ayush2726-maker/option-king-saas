def settings_saved_message(settings: dict) -> str:
    """Build an unambiguous Telegram confirmation for strategy changes."""
    values = dict(settings or {})
    trading_mode = (
        "LIVE"
        if str(values.get("trading_mode", "paper")).strip().lower() == "live"
        else "PAPER"
    )
    strategy_profile = str(values.get("mode", "default") or "default").upper()
    return (
        "⚙️ <b>Strategy Settings Saved</b>\n"
        f"Trading Mode: <b>{trading_mode}</b>\n"
        f"Strategy Profile: {strategy_profile}\n"
        f"Entry Score: {values.get('entry_threshold')}\n"
        f"SL: {values.get('sl_percent')}%\n"
        f"Target: {values.get('target_percent')}%"
    )

    # Dynamic signal - REAL ENGINE (TQU strategy.py) when broker connected
    if is_running:
        engine_state = get_user_bot_state(user["id"])
        engine_ready = engine_state.get("strategy") == "TQU_ENHANCED"

        if engine_ready:
            score = int(engine_state.get("score", 0))
            adx = float(engine_state.get("adx", 0))
            volume_ratio = float(engine_state.get("volume_ratio", 0))
            mtf_ok = bool(engine_state.get("mtf_confirmed", False))
            base_score = int(engine_state.get("base_score", 0))
            adx_score = int(engine_state.get("adx_bonus", 0))
            volume_score = int(engine_state.get("volume_bonus", 0))
            mtf_score = int(engine_state.get("mtf_bonus", 0))
            regime_score = int(engine_state.get("regime_score", 0))
            side = engine_state.get("signal", "WAIT")
            if side not in ("CE", "PE"):
                side = "CE"
            display_symbol, broker_symbol, strike, expiry = make_paper_option_symbol(primary, side)
            symbol = display_symbol

            if open_trade:
                signal = "HOLD_" + str(open_trade["side"])
            else:
                signal = "READY_" + side if score >= entry_threshold else "WAITING"

            status = "PAPER_RUNNING" if trading_mode == "paper" else "LIVE_RUNNING"
            mtf = "OK" if mtf_ok else "WEAK"

            open_trade = conn.execute(
                """SELECT id FROM paper_trades
                   WHERE user_id=? AND status='OPEN'
                   ORDER BY id DESC LIMIT 1""",
                (user["id"],)
            ).fetchone()
        else:
            score = 0
            signal = "NO_DATA"
            status = "CONNECT_BROKER_FOR_REAL_SIGNAL"
            adx = 0
            volume_ratio = 0
            mtf = "WAITING"
            base_score = 0
            adx_score = 0
            volume_score = 0
            mtf_score = 0
            regime_score = 0
            symbol = None
    else:
        score = 0
        signal = "WAITING"
        status = "PAPER_STOPPED" if trading_mode == "paper" else "LIVE_WAITING"
        adx = 0
        volume_ratio = 0
        mtf = "WAITING"
        base_score = 0
        adx_score = 0
        volume_score = 0
        mtf_score = 0
        regime_score = 0
        symbol = None

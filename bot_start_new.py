@router.post("/start")
def bot_start(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    ensure_tables(conn)

    settings = get_strategy_settings(conn, user["id"])
    trading_mode = settings.get("trading_mode", "paper")

    if trading_mode != "live":
        save_bot_status(conn, user["id"], 1, "PAPER_MODE")

        broker = conn.execute(
            "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
            (user["id"],)
        ).fetchone()
        conn.close()

        engine_note = "Broker connect karein real TQU signal ke liye."
        if broker:
            try:
                creds = {
                    "api_key": decrypt_credential(broker["api_key"]),
                    "client_id": broker["client_id"],
                    "password": decrypt_credential(broker["api_secret"]),
                    "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
                }
                start_user_bot(user["id"], creds)
                engine_note = "Real TQU signal engine started (paper mode - real orders OFF)."
            except Exception:
                pass

        try:
            msg = "\n".join([
                "📝 <b>Paper Bot Started</b>",
                "Mode: PAPER",
                f"Paper Capital: ₹{settings.get('paper_capital', 100000)}",
                f"Instruments: {', '.join(settings.get('enabled_instruments', ['NIFTY']))}",
                f"Primary: {settings.get('primary_instrument', 'NIFTY')}",
                "Real orders OFF.",
            ])
            notify_user(user["id"], msg)
        except Exception:
            pass

        return {
            "success": True,
            "message": f"Paper mode bot started. Real orders OFF. {engine_note}",
            "mode": "paper",
            "paper_capital": settings.get("paper_capital", 100000),
            "primary_instrument": settings.get("primary_instrument", "NIFTY"),
            "enabled_instruments": settings.get("enabled_instruments", ["NIFTY"]),
        }

    broker = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
        (user["id"],)
    ).fetchone()

    conn.close()

    if not broker:
        return {"success": False, "message": "Live mode ke liye pehle broker credentials save karo"}

    creds = {
        "api_key": decrypt_credential(broker["api_key"]),
        "client_id": broker["client_id"],
        "password": decrypt_credential(broker["api_secret"]),
        "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
    }

    res = start_user_bot(user["id"], creds)
    if isinstance(res, dict) and res.get("success"):
        try:
            notify_user(user["id"], "▶️ <b>LIVE Bot Started</b>\nReal orders enabled.")
        except Exception:
            pass

    return res


def market_open() -> bool:
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return False
    start = ist.replace(hour=9, minute=15, second=0, microsecond=0)
    end = ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return start <= ist <= end


@router.post("/hero-zero/start")
def hero_zero_start(body: dict = None, authorization: str = Header(None)):
    user = get_current_user(authorization)

    if not market_open():
        return {"success": False, "message": "Market closed. Hero Zero available only during market hours (Mon-Fri 09:15-15:30 IST)."}

    conn = get_db()
    ensure_tables(conn)

    settings = get_strategy_settings(conn, user["id"])
    primary = settings.get("primary_instrument", "NIFTY")
    qty = LOT_SIZES.get(primary, 65)

    open_trade = conn.execute(
        """SELECT * FROM paper_trades
           WHERE user_id=? AND status='OPEN'
           ORDER BY id DESC LIMIT 1""",
        (user["id"],)
    ).fetchone()

    if open_trade:
        conn.close()
        return {
            "success": True,
            "message": "Already open trade hai. Pehle old trade close hone do.",
            "active_trade": {
                "id": open_trade["id"],
                "symbol": open_trade["symbol"],
                "side": open_trade["side"],
                "qty": open_trade["qty"],
                "entry_price": open_trade["entry_price"],
                "status": open_trade["status"],
                "reason": open_trade["reason"],
                "created_at": open_trade["created_at"],
            }
        }

    broker = conn.execute(
        "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
        (user["id"],)
    ).fetchone()

    if not broker:
        conn.close()
        return {"success": False, "message": "Broker connect karein Hero Zero ke liye (real premium chahiye)."}

    body = body or {}
    side = body.get("side")
    if side not in ("CE", "PE"):
        conn.close()
        return {"success": False, "message": "side CE ya PE hona chahiye"}

    try:
        creds = {
            "api_key": decrypt_credential(broker["api_key"]),
            "client_id": broker["client_id"],
            "password": decrypt_credential(broker["api_secret"]),
            "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
        }
        obj = angel_login(creds)

        index_token = INDEX_TOKENS.get(primary, "26000")
        index_exch = INDEX_EXCHANGE.get(primary, "NSE")
        spot_quote = obj.ltpData(index_exch, primary, index_token)
        spot_price = float(spot_quote["data"]["ltp"])

        resolved = resolve_option(primary, spot_price, side)
        if not resolved:
            conn.close()
            return {"success": False, "message": "Option contract resolve nahi hua"}

        quote = obj.ltpData(resolved["exch_seg"], resolved["symbol"], resolved["token"])
        entry_price = float(quote["data"]["ltp"])
    except Exception as e:
        conn.close()
        return {"success": False, "message": f"Real premium fetch failed: {str(e)[:150]}"}

    if entry_price <= 0:
        conn.close()
        return {"success": False, "message": "Invalid premium mila broker se"}

    symbol = resolved["symbol"]
    sl_price = round(entry_price * 0.50, 2)
    target_price = round(entry_price * 2.00, 2)

    now = datetime.utcnow().isoformat()

    conn.execute(
        """INSERT INTO paper_trades
           (user_id, symbol, side, entry_price, qty, pnl, status, reason,
            sl_price, target_price, token, exch_seg, expiry, strike, created_at)
           VALUES (?, ?, ?, ?, ?, 0, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user["id"], symbol, side, entry_price, qty,
            f"Hero Zero real entry | SL {sl_price} | Target {target_price}",
            sl_price, target_price, resolved["token"], resolved["exch_seg"],
            resolved["expiry"], resolved["strike"], now
        )
    )

    add_trade_count(conn, user["id"], "HERO_ZERO_" + side)
    save_bot_status(conn, user["id"], 1, "HERO_ZERO_" + side)

    trade_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.commit()
    conn.close()

    try:
        msg = "\n".join([
            "🚀 <b>Expiry Hero Zero Started (REAL premium)</b>",
            f"Symbol: {symbol}",
            f"Side: {side}",
            f"Qty: {qty}",
            f"Entry: Rs {entry_price}",
            f"SL: Rs {sl_price}",
            f"Target: Rs {target_price}",
            "Mode: PAPER / DEMO (real premium tracking)",
        ])
        notify_user(user["id"], msg)
    except Exception:
        pass

    return {
        "success": True,
        "message": "Expiry Hero Zero paper trade started (real premium)",
        "active_trade": {
            "id": trade_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "sl_price": sl_price,
            "target_price": target_price,
            "exit_price": None,
            "pnl": 0,
            "status": "OPEN",
            "reason": f"Hero Zero real entry | SL {sl_price} | Target {target_price}",
            "created_at": now
        }
    }


@router.post("/hero-zero/force-close")
def hero_zero_force_close(authorization: str = Header(None)):
    user = get_current_user(authorization)

    conn = get_db()
    ensure_tables(conn)

    open_trade = conn.execute(
        """SELECT * FROM paper_trades
           WHERE user_id=? AND status='OPEN'
           ORDER BY id DESC LIMIT 1""",
        (user["id"],)
    ).fetchone()

    if not open_trade:
        conn.close()
        return {"success": True, "message": "No open paper trade"}

    entry = float(open_trade["entry_price"] or 0)
    qty = int(open_trade["qty"] or 65)
    token = open_trade["token"]
    symbol = open_trade["symbol"]
    exch_seg = open_trade["exch_seg"]

    exit_price = entry
    if token and exch_seg:
        try:
            broker = conn.execute(
                "SELECT * FROM broker_credentials WHERE user_id=? AND is_active=1 ORDER BY last_connected DESC LIMIT 1",
                (user["id"],)
            ).fetchone()
            if broker:
                creds = {
                    "api_key": decrypt_credential(broker["api_key"]),
                    "client_id": broker["client_id"],
                    "password": decrypt_credential(broker["api_secret"]),
                    "totp_secret": decrypt_credential(broker["totp_secret"]) if broker["totp_secret"] else None,
                }
                obj = angel_login(creds)
                quote = obj.ltpData(exch_seg, symbol, token)
                exit_price = float(quote["data"]["ltp"])
        except Exception:
            pass

    pnl = round((exit_price - entry) * qty, 2)

    conn.execute(
        """UPDATE paper_trades
           SET exit_price=?, pnl=?, status='CLOSED', reason=?
           WHERE id=?""",
        (exit_price, pnl, "HERO ZERO FORCE EXIT (real premium)", open_trade["id"])
    )

    add_pnl(conn, user["id"], pnl)
    conn.commit()
    conn.close()

    try:
        msg = "\n".join([
            "📤 <b>Hero Zero Exit</b>",
            f"Symbol: {open_trade['symbol']}",
            f"Entry: Rs {entry}",
            f"Exit: Rs {exit_price}",
            f"Qty: {qty}",
            f"P&L: Rs {pnl}",
        ])
        notify_user(user["id"], msg)
    except Exception:
        pass

    return {
        "success": True,
        "message": "Hero Zero trade closed",
        "exit_price": exit_price,
        "pnl": pnl
    }

from pathlib import Path


def replace_once(path, old, new, label):
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{label} marker not found in {path}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


# Main API registration/startup.
replace_once(
    "main.py",
    "from auth.registration_email_middleware import SafeRegistrationEmailVerificationMiddleware\n",
    "from auth.registration_email_middleware import SafeRegistrationEmailVerificationMiddleware\n"
    "from local_gateway.routes import router as local_gateway_router\n"
    "from local_gateway.service import ensure_local_gateway_schema\n",
    "main gateway imports",
)
replace_once(
    "main.py",
    "    ensure_recovery_schema()\n\n    from database import init_bot_status_table\n",
    "    ensure_recovery_schema()\n"
    "    ensure_local_gateway_schema()\n\n"
    "    from database import init_bot_status_table\n",
    "main gateway schema startup",
)
replace_once(
    "main.py",
    "app.include_router(recovery_router)\napp.include_router(broker_router)\n",
    "app.include_router(recovery_router)\n"
    "app.include_router(local_gateway_router)\n"
    "app.include_router(broker_router)\n",
    "main gateway router",
)

# Strategy settings: safe default one lot and local gateway requirement.
replace_once(
    "strategy/routes.py",
    '    "trading_mode": "paper",\n    "paper_capital": 100000,\n',
    '    "trading_mode": "paper",\n'
    '    "paper_capital": 100000,\n'
    '    "live_lots": 1,\n'
    '    "local_gateway_required": True,\n',
    "strategy live defaults",
)
replace_once(
    "strategy/routes.py",
    '    base["paper_capital"] = clamp_num(body.get("paper_capital", base.get("paper_capital", 100000)), 1000, 10000000, base.get("paper_capital", 100000))\n',
    '    base["paper_capital"] = clamp_num(body.get("paper_capital", base.get("paper_capital", 100000)), 1000, 10000000, base.get("paper_capital", 100000))\n'
    '    base["live_lots"] = int(clamp_num(body.get("live_lots", base.get("live_lots", 1)), 1, 10, 1))\n'
    '    base["local_gateway_required"] = True\n',
    "strategy live lot normalization",
)

# Angel engine integration.
replace_once(
    "bot/angel_fetcher.py",
    "from strategy.profile_engine import get_active_profile_config\n",
    "from strategy.profile_engine import get_active_profile_config\n"
    "from local_gateway.service import gateway_ready, queue_live_entry\n",
    "angel gateway import",
)
replace_once(
    "bot/angel_fetcher.py",
    '        "entry_threshold": 82,\n    }\n',
    '        "entry_threshold": 82,\n'
    '        "live_lots": 1,\n'
    '        "max_concurrent_trades": 1,\n'
    '        "max_trades_per_day": 5,\n'
    '        "different_index_required": True,\n'
    '    }\n',
    "angel live defaults",
)

marker = "\ndef _manage_paper_trade(\n"
helper = r'''
def _manage_live_gateway_entry(
    user_id,
    underlying,
    price,
    side,
    score,
    trade_allowed,
    settings,
    obj,
    spot_atr=0.0,
    market_data=None,
    candle_id=None,
):
    """Queue one live entry for execution by the owner's static-IP phone."""
    if not trade_allowed or side not in ("CE", "PE"):
        return {"queued": False, "reason": "NO_QUALIFYING_LIVE_SIGNAL"}

    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    minute = now_ist.hour * 60 + now_ist.minute
    if minute < 9 * 60 + 15 or minute >= 15 * 60 + 25:
        return {"queued": False, "reason": "LIVE_ENTRY_TIME_BLOCK"}

    ready, reason, status = gateway_ready(user_id)
    if not ready:
        _entry_guard_state[user_id] = {
            "allowed": False,
            "reason": reason,
            "gateway": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"queued": False, "reason": reason}

    resolved = resolve_option(underlying, price, side)
    if not resolved:
        return {"queued": False, "reason": "OPTION_CONTRACT_NOT_RESOLVED"}

    try:
        quote = obj.ltpData(
            resolved["exch_seg"],
            resolved["symbol"],
            resolved["token"],
        )
        expected_entry = float(quote["data"]["ltp"])
    except Exception as exc:
        return {"queued": False, "reason": f"OPTION_LTP_FAILED: {str(exc)[:100]}"}
    if expected_entry <= 0:
        return {"queued": False, "reason": "INVALID_OPTION_LTP"}

    quality = _option_entry_quality_angel(obj, resolved, expected_entry)
    if quality.get("allowed") is False:
        _entry_guard_state[user_id] = {
            **quality,
            "allowed": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return {"queued": False, "reason": quality.get("reason")}

    sl_percent = max(3.0, float(settings.get("sl_percent", 12) or 12))
    target_percent = max(5.0, float(settings.get("target_percent", 24) or 24))
    reward_multiple = max(1.0, target_percent / max(sl_percent, 1.0))
    atr_levels = calculate_option_atr_levels(
        spot_price=price,
        option_entry_price=expected_entry,
        spot_atr=spot_atr,
        is_expiry_day=now_ist.weekday() == 1,
        sl_floor_percent=sl_percent,
        reward_multiple=reward_multiple,
    )

    live_lots = max(1, min(int(settings.get("live_lots", 1) or 1), 10))
    quantity = int(LOT_SIZES.get(underlying, 1)) * live_lots
    safe_candle_id = str(
        candle_id
        or now_ist.replace(second=0, microsecond=0).isoformat()
    )
    idempotency_key = (
        f"LIVE_ENTRY:{user_id}:{underlying}:{resolved['symbol']}:{safe_candle_id}"
    )
    payload = {
        "underlying": underlying,
        "option_type": side,
        "symbol": resolved["symbol"],
        "symboltoken": str(resolved["token"]),
        "exchange": resolved["exch_seg"],
        "quantity": quantity,
        "lots": live_lots,
        "lot_size": int(LOT_SIZES.get(underlying, 1)),
        "expected_entry_price": round(expected_entry, 2),
        "sl_percent": sl_percent,
        "target_percent": target_percent,
        "sl_price": atr_levels.get("sl_price"),
        "target_price": atr_levels.get("target_price"),
        "force_exit_at": "15:25",
        "score": int(score or 0),
        "min_score": int((market_data or {}).get("signal_min_score") or 82),
        "spot_price": round(float(price or 0), 2),
        "spot_atr": round(float(spot_atr or 0), 4),
        "expiry": resolved.get("expiry"),
        "strike": resolved.get("strike"),
        "candle_id": safe_candle_id,
        "different_index_required": bool(
            settings.get("different_index_required", True)
        ),
        "strategy_mode": settings.get("mode", "default"),
        "execution_route": "OWNER_STATIC_IP_LOCAL_GATEWAY",
    }
    result = queue_live_entry(
        user_id,
        payload,
        idempotency_key,
        max_concurrent=int(settings.get("max_concurrent_trades", 1) or 1),
        max_trades_per_day=int(settings.get("max_trades_per_day", 5) or 5),
    )
    _entry_guard_state[user_id] = {
        "allowed": bool(result.get("queued")),
        "reason": result.get("reason"),
        "trade_id": result.get("trade_id"),
        "command_id": result.get("command_id"),
        "symbol": resolved["symbol"],
        "quantity": quantity,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    return result

'''
angel_path = Path("bot/angel_fetcher.py")
angel_text = angel_path.read_text(encoding="utf-8")
if marker not in angel_text:
    raise RuntimeError("manage paper marker not found")
angel_text = angel_text.replace(marker, "\n" + helper + "def _manage_paper_trade(\n", 1)
angel_path.write_text(angel_text, encoding="utf-8")

replace_once(
    "bot/angel_fetcher.py",
    '''    obj,
    spot_atr=0.0,
):
    """
    Checks/manages the user's open paper trade using REAL option premiums
''',
    '''    obj,
    spot_atr=0.0,
    market_data=None,
    candle_id=None,
):
    """
    Checks/manages paper trades or queues LIVE orders to the local static-IP gateway.
''',
    "paper manager signature",
)
replace_once(
    "bot/angel_fetcher.py",
    '''    Live order execution is NOT done
    here (paper mode only).
    """
    conn = get_db()
''',
    '''    LIVE broker orders are never sent from Railway; they are queued to the
    owner's local gateway so Angel One sees the registered static IPv4.
    """
    if str(settings.get("trading_mode", "paper")).lower() == "live":
        return _manage_live_gateway_entry(
            user_id=user_id,
            underlying=underlying,
            price=price,
            side=side,
            score=score,
            trade_allowed=trade_allowed,
            settings=settings,
            obj=obj,
            spot_atr=spot_atr,
            market_data=market_data,
            candle_id=candle_id,
        )

    conn = get_db()
''',
    "paper manager live branch",
)

print("Personal static-IP gateway integration patch applied")

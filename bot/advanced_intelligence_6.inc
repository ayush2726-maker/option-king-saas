def get_advanced_summary(user_id: int, recent_limit: int = 20) -> Dict[str, Any]:
    ensure_schema()
    limit = max(1, min(_int(recent_limit, 20), 50))
    conn = get_db()
    try:
        decisions = conn.execute(
            """
            SELECT * FROM ai_advanced_decisions
            WHERE user_id=?
            ORDER BY datetime(created_at) DESC,rowid DESC
            LIMIT ?
            """,
            (int(user_id), limit),
        ).fetchall()
        outcomes = conn.execute(
            """
            SELECT * FROM ai_advanced_outcomes
            WHERE user_id=? AND horizon_minutes=?
            """,
            (int(user_id), PRIMARY_HORIZON),
        ).fetchall()
        recent = []
        for row in decisions:
            item = dict(row)
            for source, target, default in (
                ("probabilities_json", "probabilities", {}),
                ("features_json", "features", {}),
                ("option_snapshot_json", "option_intelligence", {}),
                ("news_snapshot_json", "news_intelligence", {}),
                ("global_snapshot_json", "global_intelligence", {}),
                ("selected_option_json", "selected_option", {}),
            ):
                item[target] = _loads(item.pop(source), default)
            item["outcomes"] = [
                dict(outcome)
                for outcome in conn.execute(
                    "SELECT * FROM ai_advanced_outcomes "
                    "WHERE decision_id=? ORDER BY horizon_minutes",
                    (item["id"],),
                ).fetchall()
            ]
            for flag in ("complete", "trade_blocking", "order_execution"):
                item[flag] = bool(item.get(flag))
            recent.append(item)
        wins = sum(row["advanced_outcome"] == "WIN" for row in outcomes)
        losses = sum(row["advanced_outcome"] == "LOSS" for row in outcomes)
        option_labeled = [
            row for row in outcomes if abs(_num(row["option_exit_ltp"])) > 0
        ]
        option_net = round(
            sum(_num(row["estimated_net_option_pnl"]) for row in option_labeled), 2
        )
        model = {
            str(horizon): _model_state(user_id, horizon)
            for horizon in HORIZONS
        }
        return {
            "success": True,
            "version": VERSION,
            "mode": "RAILWAY_ADVANCED_SHADOW_ONLY",
            "location": "RAILWAY",
            "supported_brokers": ["angelone", "upstox", "zerodha"],
            "broker_dependency": "NONE_SINGLE_BROKER",
            "trade_blocking": False,
            "order_execution": False,
            "summary": {
                "total_decisions": len(decisions),
                "advanced_15m_wins": wins,
                "advanced_15m_losses": losses,
                "advanced_15m_hit_rate_percent": (
                    round(wins / (wins + losses) * 100.0, 2)
                    if wins + losses
                    else None
                ),
                "option_premium_labeled_15m_count": len(option_labeled),
                "estimated_net_option_pnl_15m": option_net,
                "cost_model_version": COST_MODEL_VERSION,
            },
            "model_state": model,
            "storage": get_db_storage_info(),
            "recent_decisions": recent,
        }
    finally:
        conn.close()


def advanced_health() -> Dict[str, Any]:
    ensure_schema()
    broker_status = {
        str(user_id): {
            "broker": (cache.get("data") or {}).get("broker"),
            "success": bool((cache.get("data") or {}).get("success")),
            "error": (cache.get("data") or {}).get("error"),
            "contract_count": _int((cache.get("data") or {}).get("contract_count")),
        }
        for user_id, cache in _BROKER_CACHE.items()
    }
    return {
        "success": True,
        "version": VERSION,
        "started": bool(_STARTED),
        "thread_alive": bool(_THREAD and _THREAD.is_alive()),
        "last_cycle_at": _LAST_CYCLE_AT,
        "last_global_at": _LAST_GLOBAL_AT,
        "last_error": _LAST_ERROR,
        "supported_brokers": ["angelone", "upstox", "zerodha"],
        "broker_neutral": True,
        "broker_status": broker_status,
        "global_markets": {
            "available_count": len((_GLOBAL_CACHE or {}).get("items") or {}),
            "errors": (_GLOBAL_CACHE or {}).get("errors") or {},
            "gift_nifty_configured": bool(
                (_GLOBAL_CACHE or {}).get("gift_nifty_configured")
            ),
        },
        "storage": get_db_storage_info(),
        "mode": "SHADOW_ONLY",
        "trade_blocking": False,
        "order_execution": False,
    }


def _heartbeat(error: Optional[str]) -> None:
    global _LAST_CYCLE_AT, _LAST_ERROR
    _LAST_CYCLE_AT = _iso()
    _LAST_ERROR = str(error)[:500] if error else None
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO ai_advanced_runtime
            VALUES(1,?,?,?,?,?,?,0,0)
            ON CONFLICT(singleton) DO UPDATE SET
                version=excluded.version,
                instance_id=excluded.instance_id,
                heartbeat_at=excluded.heartbeat_at,
                last_global_at=excluded.last_global_at,
                last_error=excluded.last_error,
                trade_blocking=0,
                order_execution=0
            """,
            (
                VERSION,
                _INSTANCE_ID,
                _LAST_CYCLE_AT,
                _LAST_CYCLE_AT,
                _LAST_GLOBAL_AT,
                _LAST_ERROR,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _cycle() -> None:
    builder = _SNAPSHOT_BUILDER
    if not callable(builder):
        return
    global_data = _global_markets()
    news = aggregate_news()
    for user_id in _running_users():
        try:
            snapshot = dict(builder(user_id) or {})
            option_data = _chain_for_user(user_id, snapshot)
            _observe_outcomes(user_id, snapshot, option_data)
            base = predict(snapshot)
            result = build_advanced_decision(
                user_id,
                snapshot,
                base,
                news,
                option_data,
                global_data,
            )
            _register_decision(user_id, snapshot, result)
        except Exception as exc:
            print(
                "AI ADVANCED RAILWAY | cycle warning | "
                f"user={user_id} | {type(exc).__name__}: {str(exc)[:220]}"
            )


def _loop() -> None:
    while True:
        error = None
        try:
            _cycle()
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:300]}"
            print(f"AI ADVANCED RAILWAY | monitor warning | {error}")
        try:
            _heartbeat(error)
        except Exception as exc:
            print(f"AI ADVANCED RAILWAY | heartbeat warning | {str(exc)[:160]}")
        time.sleep(POLL_SECONDS)


def start_advanced_intelligence(
    snapshot_builder: Callable[[int], Dict[str, Any]],
) -> Dict[str, Any]:
    global _STARTED, _THREAD, _SNAPSHOT_BUILDER
    with _LOCK:
        _SNAPSHOT_BUILDER = snapshot_builder
        if _STARTED and _THREAD and _THREAD.is_alive():
            return advanced_health()
        ensure_schema()
        _STARTED = True
        _THREAD = threading.Thread(
            target=_loop,
            name="okai-advanced-intelligence-shadow",
            daemon=True,
        )
        _THREAD.start()
        print(
            f"AI ADVANCED RAILWAY {VERSION} active | "
            "Angel One + Upstox + Zerodha | option chain + Greeks + "
            "liquidity + global + news + online calibration | "
            "monitor only, trade blocking OFF | order execution OFF"
        )
        return advanced_health()

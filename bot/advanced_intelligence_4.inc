def _estimated_round_trip_cost(entry: float, exit_price: float, qty: int) -> float:
    turnover = max(0.0, entry + exit_price) * max(1, qty)
    flat = _num(os.getenv("OKAI_ESTIMATED_FLAT_ROUND_TRIP"), 40.0)
    variable = turnover * _num(os.getenv("OKAI_ESTIMATED_VARIABLE_COST_RATE"), 0.0008)
    return round(flat + variable, 2)


def ensure_schema() -> None:
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_advanced_decisions (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                broker_name TEXT,
                symbol TEXT NOT NULL,
                entry_spot REAL NOT NULL,
                base_decision TEXT,
                base_confidence INTEGER,
                advanced_decision TEXT NOT NULL,
                advanced_confidence REAL,
                probabilities_json TEXT NOT NULL,
                relation_to_base TEXT,
                data_quality INTEGER,
                features_json TEXT NOT NULL,
                option_snapshot_json TEXT NOT NULL,
                news_snapshot_json TEXT NOT NULL,
                global_snapshot_json TEXT NOT NULL,
                selected_option_json TEXT,
                option_symbol TEXT,
                option_token TEXT,
                option_side TEXT,
                option_entry_ltp REAL,
                option_entry_bid REAL,
                option_entry_ask REAL,
                option_lot_size INTEGER,
                model_mode TEXT,
                model_sample_count INTEGER,
                complete INTEGER DEFAULT 0,
                completed_at TEXT,
                trade_blocking INTEGER DEFAULT 0,
                order_execution INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ai_advanced_user_created
            ON ai_advanced_decisions(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_ai_advanced_pending
            ON ai_advanced_decisions(user_id, complete, created_at);

            CREATE TABLE IF NOT EXISTS ai_advanced_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                evaluated_at TEXT NOT NULL,
                exit_spot REAL,
                option_exit_ltp REAL,
                option_exit_bid REAL,
                gross_option_pnl REAL,
                estimated_cost REAL,
                estimated_net_option_pnl REAL,
                cost_model_version TEXT,
                advanced_outcome TEXT,
                base_spot_points REAL,
                advanced_spot_points REAL,
                advanced_vs_base_spot_benefit REAL,
                UNIQUE(decision_id, horizon_minutes)
            );

            CREATE TABLE IF NOT EXISTS ai_advanced_model_state (
                user_id INTEGER NOT NULL,
                horizon_minutes INTEGER NOT NULL,
                sample_count INTEGER DEFAULT 0,
                weights_json TEXT NOT NULL,
                calibration_json TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY(user_id, horizon_minutes)
            );

            CREATE TABLE IF NOT EXISTS ai_advanced_runtime (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                version TEXT,
                instance_id TEXT,
                started_at TEXT,
                heartbeat_at TEXT,
                last_global_at TEXT,
                last_error TEXT,
                trade_blocking INTEGER DEFAULT 0,
                order_execution INTEGER DEFAULT 0
            );
            """
        )
        now = _iso()
        conn.execute(
            """
            INSERT INTO ai_advanced_runtime
            VALUES(1,?,?,?,?,NULL,NULL,0,0)
            ON CONFLICT(singleton) DO UPDATE SET
                version=excluded.version,
                instance_id=excluded.instance_id,
                heartbeat_at=excluded.heartbeat_at,
                trade_blocking=0,
                order_execution=0
            """,
            (VERSION, _INSTANCE_ID, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _running_users() -> List[int]:
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id FROM (
                SELECT user_id FROM user_bot_state WHERE is_running=1
                UNION ALL
                SELECT user_id FROM bot_status WHERE is_running=1
            )
            ORDER BY user_id
            """
        ).fetchall()
        return [int(row["user_id"]) for row in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _chain_for_user(
    user_id: int,
    snapshot: Mapping[str, Any],
    force: bool = False,
) -> Dict[str, Any]:
    cached = _BROKER_CACHE.get(int(user_id))
    now_mono = time.monotonic()
    if (
        not force
        and cached
        and now_mono - _num(cached.get("at")) < BROKER_REFRESH_SECONDS
    ):
        return dict(cached.get("data") or {})
    raw = _fetch_broker_chain(user_id, snapshot)
    metrics = _option_metrics(user_id, raw, _num(snapshot.get("price")))
    _BROKER_CACHE[int(user_id)] = {"at": now_mono, "data": metrics}
    return metrics


def _register_decision(
    user_id: int,
    snapshot: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Optional[str]:
    if (
        not snapshot.get("market_open")
        or not snapshot.get("feed_connected")
        or _num(snapshot.get("price")) <= 0
    ):
        return None
    side = _direction(result.get("decision"))
    if side not in {"CE", "PE", "NO_TRADE"}:
        return None
    option = dict(result.get("selected_option") or {})
    broker_name = str((result.get("option_intelligence") or {}).get("broker") or "")
    conn = get_db()
    try:
        last = conn.execute(
            """
            SELECT created_at, advanced_decision, relation_to_base, option_symbol,
                   CAST(entry_spot / 5 AS INTEGER) AS spot_bucket
            FROM ai_advanced_decisions
            WHERE user_id=?
            ORDER BY datetime(created_at) DESC, rowid DESC
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        if last:
            created = _parse_dt(last["created_at"])
            same = (
                str(last["advanced_decision"]) == side
                and str(last["relation_to_base"]) == str(result.get("relation_to_base"))
                and str(last["option_symbol"] or "") == str(option.get("symbol") or "")
                and _int(last["spot_bucket"]) == int(_num(snapshot.get("price")) / 5.0)
            )
            if created and same and (_utc_now() - created).total_seconds() < 300:
                return None
        decision_id = uuid.uuid4().hex[:20]
        conn.execute(
            """
            INSERT INTO ai_advanced_decisions (
                id,user_id,created_at,broker_name,symbol,entry_spot,
                base_decision,base_confidence,advanced_decision,advanced_confidence,
                probabilities_json,relation_to_base,data_quality,features_json,
                option_snapshot_json,news_snapshot_json,global_snapshot_json,
                selected_option_json,option_symbol,option_token,option_side,
                option_entry_ltp,option_entry_bid,option_entry_ask,option_lot_size,
                model_mode,model_sample_count,complete,trade_blocking,order_execution
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0)
            """,
            (
                decision_id,
                int(user_id),
                _iso(),
                broker_name,
                str(snapshot.get("symbol") or "NIFTY").upper(),
                round(_num(snapshot.get("price")), 2),
                str(result.get("base_decision") or "NO_TRADE"),
                _int(result.get("base_confidence")),
                side,
                _num(result.get("confidence")),
                _json(result.get("probabilities") or {}),
                str(result.get("relation_to_base") or ""),
                _int(result.get("data_quality")),
                _json(result.get("features") or {}),
                _json(result.get("option_intelligence") or {}),
                _json(result.get("news_intelligence") or {}),
                _json(result.get("global_intelligence") or {}),
                _json(option) if option else None,
                str(option.get("symbol") or ""),
                str(option.get("token") or ""),
                str(option.get("side") or ""),
                _num(option.get("ltp")),
                _num(option.get("bid")),
                _num(option.get("ask")),
                _int(option.get("lot_size"), 1),
                str(result.get("model_mode") or ""),
                _int(result.get("model_sample_count")),
            ),
        )
        conn.commit()
        print(
            "AI ADVANCED RAILWAY | decision logged | "
            f"user={user_id} | broker={broker_name or 'none'} | "
            f"{side} {result.get('confidence')}% | "
            f"{result.get('relation_to_base')} | trade blocking OFF"
        )
        return decision_id
    finally:
        conn.close()


def _signed_spot(side: str, entry: float, exit_price: float) -> float:
    if side == "CE":
        return exit_price - entry
    if side == "PE":
        return entry - exit_price
    return 0.0


def _find_contract(
    option_data: Mapping[str, Any],
    symbol: str,
    token: str,
) -> Optional[Dict[str, Any]]:
    for item in option_data.get("contracts") or []:
        if token and str(item.get("token") or "") == token:
            return dict(item)
        if symbol and str(item.get("symbol") or "") == symbol:
            return dict(item)
    return None


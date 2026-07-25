def _update_online_model(
    user_id: int,
    horizon: int,
    features: Mapping[str, float],
    chosen_side: str,
    won: bool,
    predicted_confidence: float,
) -> None:
    if chosen_side not in {"CE", "PE"}:
        return
    state = _model_state(user_id, horizon)
    weights = dict(state["weights"])
    x = dict(features)
    target = 1.0 if (
        (chosen_side == "CE" and won)
        or (chosen_side == "PE" and not won)
    ) else 0.0
    logit = sum(_num(x.get(key)) * _num(weights.get(key)) for key in weights)
    predicted = _sigmoid(logit)
    learning_rate = 0.025 / math.sqrt(1.0 + state["sample_count"] / 100.0)
    error = target - predicted
    for key in weights:
        weights[key] = round(
            _clamp(
                _num(weights.get(key)) + learning_rate * error * _num(x.get(key)),
                -6.0,
                6.0,
            ),
            8,
        )
    calibration = dict(state.get("calibration") or {})
    bucket = str(int(_clamp(predicted_confidence, 0, 99) // 10) * 10)
    bin_data = dict(calibration.get(bucket) or {"wins": 0, "total": 0})
    bin_data["total"] = _int(bin_data.get("total")) + 1
    if won:
        bin_data["wins"] = _int(bin_data.get("wins")) + 1
    calibration[bucket] = bin_data
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO ai_advanced_model_state (
                user_id,horizon_minutes,sample_count,weights_json,
                calibration_json,updated_at
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id,horizon_minutes) DO UPDATE SET
                sample_count=excluded.sample_count,
                weights_json=excluded.weights_json,
                calibration_json=excluded.calibration_json,
                updated_at=excluded.updated_at
            """,
            (
                int(user_id),
                int(horizon),
                state["sample_count"] + 1,
                _json(weights),
                _json(calibration),
                _iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _observe_outcomes(
    user_id: int,
    snapshot: Mapping[str, Any],
    option_data: Mapping[str, Any],
) -> int:
    spot = _num(snapshot.get("price"))
    if spot <= 0 or not snapshot.get("feed_connected"):
        return 0
    conn = get_db()
    created = 0
    model_updates: List[Tuple[int, Dict[str, float], str, bool, float]] = []
    try:
        rows = conn.execute(
            """
            SELECT * FROM ai_advanced_decisions
            WHERE user_id=? AND complete=0
            ORDER BY datetime(created_at) ASC
            """,
            (int(user_id),),
        ).fetchall()
        for row in rows:
            created_at = _parse_dt(row["created_at"])
            if not created_at:
                continue
            elapsed = (_utc_now() - created_at).total_seconds()
            entry_spot = _num(row["entry_spot"])
            advanced_side = str(row["advanced_decision"] or "NO_TRADE")
            base_side = str(row["base_decision"] or "NO_TRADE")
            current_contract = _find_contract(
                option_data,
                str(row["option_symbol"] or ""),
                str(row["option_token"] or ""),
            )
            for horizon in HORIZONS:
                if elapsed < horizon * 60:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM ai_advanced_outcomes WHERE decision_id=? AND horizon_minutes=?",
                    (row["id"], horizon),
                ).fetchone()
                if exists:
                    continue
                advanced_points = _signed_spot(advanced_side, entry_spot, spot)
                base_points = _signed_spot(base_side, entry_spot, spot)
                threshold = max(4.0, entry_spot * 0.00018) * {
                    5: 0.8, 15: 1.0, 30: 1.25
                }[horizon]
                if advanced_side == "NO_TRADE":
                    outcome = "CORRECT_SKIP" if abs(spot - entry_spot) < threshold else "MISSED_MOVE"
                else:
                    outcome = (
                        "WIN" if advanced_points >= threshold
                        else "LOSS" if advanced_points <= -threshold
                        else "FLAT"
                    )
                option_exit_ltp = option_exit_bid = gross = cost = net = 0.0
                if current_contract and _num(row["option_entry_ask"]) > 0:
                    option_exit_ltp = _num(current_contract.get("ltp"))
                    option_exit_bid = _num(current_contract.get("bid"), option_exit_ltp)
                    qty = max(1, _int(row["option_lot_size"], 1))
                    gross = (
                        option_exit_bid - _num(row["option_entry_ask"])
                    ) * qty
                    cost = _estimated_round_trip_cost(
                        _num(row["option_entry_ask"]), option_exit_bid, qty
                    )
                    net = gross - cost
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ai_advanced_outcomes (
                        decision_id,user_id,horizon_minutes,evaluated_at,
                        exit_spot,option_exit_ltp,option_exit_bid,gross_option_pnl,
                        estimated_cost,estimated_net_option_pnl,cost_model_version,
                        advanced_outcome,base_spot_points,advanced_spot_points,
                        advanced_vs_base_spot_benefit
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row["id"],
                        int(user_id),
                        horizon,
                        _iso(),
                        round(spot, 2),
                        round(option_exit_ltp, 4),
                        round(option_exit_bid, 4),
                        round(gross, 2),
                        round(cost, 2),
                        round(net, 2),
                        COST_MODEL_VERSION,
                        outcome,
                        round(base_points, 2),
                        round(advanced_points, 2),
                        round(advanced_points - base_points, 2),
                    ),
                )
                created += 1
                won = net > 0 if option_exit_bid > 0 else outcome == "WIN"
                model_updates.append(
                    (
                        horizon,
                        _loads(row["features_json"], {}),
                        advanced_side,
                        won,
                        _num(row["advanced_confidence"]),
                    )
                )
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM ai_advanced_outcomes WHERE decision_id=?",
                (row["id"],),
            ).fetchone()["n"]
            if _int(count) >= len(HORIZONS):
                conn.execute(
                    "UPDATE ai_advanced_decisions SET complete=1,completed_at=? WHERE id=?",
                    (_iso(), row["id"]),
                )
        conn.execute(
            "DELETE FROM ai_advanced_outcomes WHERE decision_id IN "
            "(SELECT id FROM ai_advanced_decisions WHERE user_id=? "
            "AND datetime(created_at)<datetime('now','-180 days'))",
            (int(user_id),),
        )
        conn.execute(
            "DELETE FROM ai_advanced_decisions WHERE user_id=? "
            "AND datetime(created_at)<datetime('now','-180 days')",
            (int(user_id),),
        )
        conn.commit()
    finally:
        conn.close()
    for horizon, features, advanced_side, won, confidence in model_updates:
        try:
            _update_online_model(
                user_id,
                horizon,
                features,
                advanced_side,
                won,
                confidence,
            )
        except Exception as exc:
            print(
                "AI ADVANCED RAILWAY | model update warning | "
                f"user={user_id} | horizon={horizon} | {str(exc)[:140]}"
            )
    return created


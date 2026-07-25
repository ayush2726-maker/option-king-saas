def _latest_previous_chain(user_id: int) -> List[Dict[str, Any]]:
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT option_snapshot_json
            FROM ai_advanced_decisions
            WHERE user_id=?
            ORDER BY datetime(created_at) DESC, rowid DESC
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        if not row:
            return []
        snapshot = _loads(row["option_snapshot_json"], {})
        return list(snapshot.get("contracts") or [])
    except Exception:
        return []
    finally:
        conn.close()


def _option_metrics(
    user_id: int,
    chain_result: Mapping[str, Any],
    spot: float,
) -> Dict[str, Any]:
    contracts = [dict(item) for item in (chain_result.get("contracts") or [])]
    previous = {
        (str(item.get("symbol")), str(item.get("token"))): _num(item.get("oi"))
        for item in _latest_previous_chain(user_id)
    }
    call_oi = put_oi = call_change = put_change = 0.0
    strikes: Dict[float, Dict[str, float]] = {}
    spreads = []
    depth_signed = 0.0
    depth_total = 0.0
    iv_values = []
    for item in contracts:
        side = _direction(item.get("side"))
        oi = max(0.0, _num(item.get("oi")))
        old_oi = previous.get((str(item.get("symbol")), str(item.get("token"))), oi)
        change = oi - old_oi
        item["oi_change"] = round(change, 2)
        strike = _num(item.get("strike"))
        strikes.setdefault(strike, {"CE": 0.0, "PE": 0.0})[side] += oi
        if side == "CE":
            call_oi += oi
            call_change += change
        elif side == "PE":
            put_oi += oi
            put_change += change
        spread = _num(item.get("spread_percent"))
        if spread >= 0:
            spreads.append(spread)
        bid_qty = max(0.0, _num(item.get("bid_qty")))
        ask_qty = max(0.0, _num(item.get("ask_qty")))
        depth_signed += bid_qty - ask_qty
        depth_total += bid_qty + ask_qty
        iv = _num(item.get("iv"))
        if iv > 0:
            iv_values.append(iv)
    market_info = dict(chain_result.get("market_info") or {})
    official_oi = dict(market_info.get("oi") or {})
    if official_oi:
        call_oi = _num(official_oi.get("total_calls"), call_oi)
        put_oi = _num(official_oi.get("total_puts"), put_oi)
    official_change = dict(market_info.get("change_oi") or {})
    if official_change:
        call_change = _num(
            official_change.get("total_call_change_oi"),
            call_change,
        )
        put_change = _num(
            official_change.get("total_put_change_oi"),
            put_change,
        )
    pcr = put_oi / call_oi if call_oi > 0 else 0.0
    official_pcr = dict(market_info.get("pcr") or {})
    pcr = _num(
        official_pcr.get("pcr", official_pcr.get("put_call_ratio")),
        pcr,
    )
    change_pcr = (
        put_change / call_change
        if abs(call_change) > 1e-9
        else 0.0
    )
    call_wall = max(strikes, key=lambda strike: strikes[strike]["CE"], default=0.0)
    put_wall = max(strikes, key=lambda strike: strikes[strike]["PE"], default=0.0)
    max_pain = 0.0
    pain_best: Optional[float] = None
    for settlement in strikes:
        pain = 0.0
        for strike, pair in strikes.items():
            pain += pair["CE"] * max(0.0, settlement - strike)
            pain += pair["PE"] * max(0.0, strike - settlement)
        if pain_best is None or pain < pain_best:
            pain_best = pain
            max_pain = settlement
    depth_imbalance = depth_signed / depth_total if depth_total > 0 else 0.0
    average_spread = sum(spreads) / len(spreads) if spreads else 0.0
    option_sentiment = 0.0
    if pcr:
        option_sentiment += _clamp((pcr - 1.0) * 45.0, -35.0, 35.0)
    if put_wall and put_wall < spot:
        option_sentiment += 8.0
    if call_wall and call_wall > spot:
        option_sentiment -= 8.0
    option_sentiment += _clamp(depth_imbalance * 22.0, -18.0, 18.0)
    if change_pcr:
        option_sentiment += _clamp((change_pcr - 1.0) * 10.0, -10.0, 10.0)
    return {
        "contracts": contracts,
        "contract_count": len(contracts),
        "pcr": round(pcr, 4),
        "change_pcr": round(change_pcr, 4),
        "total_call_oi": round(call_oi, 2),
        "total_put_oi": round(put_oi, 2),
        "call_oi_change": round(call_change, 2),
        "put_oi_change": round(put_change, 2),
        "call_oi_wall": round(call_wall, 2),
        "put_oi_wall": round(put_wall, 2),
        "max_pain": round(
            _num(
                (market_info.get("max_pain") or {}).get("max_pain"),
                max_pain,
            ),
            2,
        ),
        "depth_imbalance": round(depth_imbalance, 4),
        "average_spread_percent": round(average_spread, 4),
        "average_iv": round(sum(iv_values) / len(iv_values), 4) if iv_values else 0.0,
        "sentiment_score": round(_clamp(option_sentiment, -100.0, 100.0), 2),
        "sentiment_direction": (
            "CE" if option_sentiment >= 10
            else "PE" if option_sentiment <= -10
            else "NEUTRAL"
        ),
        "market_info": market_info,
        "source": chain_result.get("source"),
        "broker": chain_result.get("broker"),
        "success": bool(chain_result.get("success")),
        "error": chain_result.get("error"),
        "fetched_at": chain_result.get("fetched_at"),
    }


def _contract_rank(item: Mapping[str, Any], side: str) -> float:
    if _direction(item.get("side")) != side:
        return -1e9
    delta = abs(_num(item.get("delta")))
    spread = _num(item.get("spread_percent"), 99.0)
    volume = max(0.0, _num(item.get("volume")))
    oi = max(0.0, _num(item.get("oi")))
    theta = abs(_num(item.get("theta")))
    iv = _num(item.get("iv"))
    liquidity = math.log10(1.0 + volume) * 4.0 + math.log10(1.0 + oi) * 3.0
    delta_fit = 30.0 - abs(delta - 0.52) * 70.0 if delta > 0 else 4.0
    spread_penalty = min(45.0, spread * 7.0)
    theta_penalty = min(12.0, theta / 8.0)
    iv_penalty = max(0.0, iv - 45.0) * 0.15
    return liquidity + delta_fit - spread_penalty - theta_penalty - iv_penalty


def _select_contract(option_data: Mapping[str, Any], side: str) -> Optional[Dict[str, Any]]:
    if side not in {"CE", "PE"}:
        return None
    contracts = list(option_data.get("contracts") or [])
    if not contracts:
        return None
    ranked = sorted(
        ((_contract_rank(item, side), dict(item)) for item in contracts),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] <= -1e8:
        return None
    selected = ranked[0][1]
    selected["selection_score"] = round(ranked[0][0], 2)
    selected["selection_model"] = "DELTA_LIQUIDITY_IV_THETA_V1"
    return selected


def _fetch_chart(symbol: str) -> Optional[Dict[str, Any]]:
    encoded = urllib.parse.quote(symbol, safe="")
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{encoded}?interval=5m&range=1d"
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "OptionKingAI-GlobalMonitor/1.0"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None
    meta = result.get("meta") or {}
    price = _num(meta.get("regularMarketPrice"))
    previous = _num(meta.get("chartPreviousClose", meta.get("previousClose")))
    if price <= 0:
        closes = (((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
        valid = [_num(value) for value in closes if _num(value) > 0]
        price = valid[-1] if valid else 0.0
    change_pct = ((price - previous) / previous * 100.0) if previous > 0 else 0.0
    return {
        "symbol": symbol,
        "price": round(price, 4),
        "previous_close": round(previous, 4),
        "change_percent": round(change_pct, 4),
        "market_time": meta.get("regularMarketTime"),
    }


def _global_markets(force: bool = False) -> Dict[str, Any]:
    global _GLOBAL_CACHE, _GLOBAL_CACHE_MONO, _LAST_GLOBAL_AT
    now_mono = time.monotonic()
    if (
        not force
        and _GLOBAL_CACHE
        and now_mono - _GLOBAL_CACHE_MONO < GLOBAL_REFRESH_SECONDS
    ):
        return dict(_GLOBAL_CACHE)
    symbols = {
        "sp500": "^GSPC",
        "nasdaq": "^IXIC",
        "dow": "^DJI",
        "nikkei": "^N225",
        "hang_seng": "^HSI",
        "crude": "CL=F",
        "usd_inr": "INR=X",
        "india_vix": "^INDIAVIX",
        "us_10y": "^TNX",
    }
    configured_gift = str(os.getenv("OKAI_GIFT_NIFTY_SYMBOL", "")).strip()
    if configured_gift:
        symbols["gift_nifty"] = configured_gift
    items: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for name, ticker in symbols.items():
        try:
            data = _fetch_chart(ticker)
            if data:
                items[name] = data
            else:
                errors[name] = "NO_DATA"
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {str(exc)[:100]}"
    risk_score = 0.0
    for name in ("sp500", "nasdaq", "dow", "nikkei", "hang_seng"):
        risk_score += _num((items.get(name) or {}).get("change_percent"))
    risk_score /= max(1, sum(name in items for name in ("sp500", "nasdaq", "dow", "nikkei", "hang_seng")))
    risk_score -= max(0.0, _num((items.get("crude") or {}).get("change_percent"))) * 0.6
    risk_score -= max(0.0, _num((items.get("usd_inr") or {}).get("change_percent"))) * 1.2
    risk_score -= max(0.0, _num((items.get("india_vix") or {}).get("change_percent"))) * 0.8
    result = {
        "fetched_at": _iso(),
        "items": items,
        "errors": errors,
        "risk_score": round(_clamp(risk_score * 8.0, -100.0, 100.0), 2),
        "risk_direction": (
            "CE" if risk_score > 0.25
            else "PE" if risk_score < -0.25
            else "NEUTRAL"
        ),
        "gift_nifty_configured": bool(configured_gift),
    }
    _GLOBAL_CACHE = dict(result)
    _GLOBAL_CACHE_MONO = now_mono
    _LAST_GLOBAL_AT = result["fetched_at"]
    return result


def _feature_vector(
    snapshot: Mapping[str, Any],
    base: Mapping[str, Any],
    news: Mapping[str, Any],
    options: Mapping[str, Any],
    global_data: Mapping[str, Any],
) -> Dict[str, float]:
    probabilities = dict(base.get("probabilities") or {})
    signal = _direction(snapshot.get("signal_direction") or snapshot.get("signal"))
    news_bias = _direction(news.get("news_bias"))
    option_direction = _direction(options.get("sentiment_direction"))
    global_direction = _direction(global_data.get("risk_direction"))
    return {
        "intercept": 1.0,
        "base_edge": (_num(probabilities.get("CE")) - _num(probabilities.get("PE"))) / 100.0,
        "strategy_direction": 1.0 if signal == "CE" else -1.0 if signal == "PE" else 0.0,
        "strategy_score": _num(snapshot.get("strategy_score")) / 100.0,
        "adx": _num(snapshot.get("adx")) / 50.0,
        "volume_ratio": _num(snapshot.get("volume_ratio")) - 1.0,
        "price_vwap": (
            (_num(snapshot.get("price")) - _num(snapshot.get("vwap")))
            / max(1.0, _num(snapshot.get("price")))
            * 100.0
        ),
        "news": (
            (1.0 if news_bias == "CE" else -1.0 if news_bias == "PE" else 0.0)
            * _num(news.get("news_strength"))
            / 100.0
        ),
        "news_risk": _num(news.get("news_risk_score")) / 100.0,
        "option": (
            (1.0 if option_direction == "CE" else -1.0 if option_direction == "PE" else 0.0)
            * abs(_num(options.get("sentiment_score")))
            / 100.0
        ),
        "pcr": _clamp(_num(options.get("pcr")) - 1.0, -1.5, 1.5),
        "depth": _num(options.get("depth_imbalance")),
        "spread": _num(options.get("average_spread_percent")) / 10.0,
        "global": (
            (1.0 if global_direction == "CE" else -1.0 if global_direction == "PE" else 0.0)
            * abs(_num(global_data.get("risk_score")))
            / 100.0
        ),
    }


DEFAULT_WEIGHTS = {
    "intercept": 0.0,
    "base_edge": 2.2,
    "strategy_direction": 0.7,
    "strategy_score": 0.3,
    "adx": 0.25,
    "volume_ratio": 0.2,
    "price_vwap": 0.25,
    "news": 0.7,
    "news_risk": -0.15,
    "option": 1.0,
    "pcr": 0.3,
    "depth": 0.45,
    "spread": -0.35,
    "global": 0.65,
}


def _model_state(user_id: int, horizon: int = PRIMARY_HORIZON) -> Dict[str, Any]:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM ai_advanced_model_state WHERE user_id=? AND horizon_minutes=?",
            (int(user_id), int(horizon)),
        ).fetchone()
        if not row:
            return {
                "sample_count": 0,
                "weights": dict(DEFAULT_WEIGHTS),
                "calibration": {},
                "ready": False,
            }
        return {
            "sample_count": _int(row["sample_count"]),
            "weights": {
                **DEFAULT_WEIGHTS,
                **_loads(row["weights_json"], {}),
            },
            "calibration": _loads(row["calibration_json"], {}),
            "ready": _int(row["sample_count"]) >= MIN_TRAIN_SAMPLES,
        }
    finally:
        conn.close()


def _sigmoid(value: float) -> float:
    if value >= 0:
        exp = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + exp)
    exp = math.exp(max(value, -60.0))
    return exp / (1.0 + exp)


def _advanced_predict(
    user_id: int,
    features: Mapping[str, float],
    data_quality: int,
) -> Dict[str, Any]:
    state = _model_state(user_id)
    weights = state["weights"]
    logit = sum(_num(features.get(key)) * _num(weight) for key, weight in weights.items())
    probability_ce = _sigmoid(logit)
    probability_pe = 1.0 - probability_ce
    directional_conf = max(probability_ce, probability_pe) * 100.0
    no_trade = max(5.0, 42.0 - directional_conf * 0.35)
    no_trade += max(0.0, 55.0 - data_quality) * 0.8
    no_trade += max(0.0, _num(features.get("spread")) * 30.0)
    if abs(probability_ce - probability_pe) < 0.12:
        no_trade += 20.0
    direction_scale = max(0.0, 100.0 - no_trade)
    ce = probability_ce * direction_scale
    pe = probability_pe * direction_scale
    total = ce + pe + no_trade or 1.0
    probabilities = {
        "CE": int(round(ce / total * 100.0)),
        "PE": int(round(pe / total * 100.0)),
        "NO_TRADE": int(round(no_trade / total * 100.0)),
    }
    probabilities["NO_TRADE"] += 100 - sum(probabilities.values())
    decision, raw_conf = max(probabilities.items(), key=lambda item: item[1])
    bucket = str(int(raw_conf // 10) * 10)
    calibration = state.get("calibration") or {}
    bin_data = calibration.get(bucket) or {}
    calibrated = (
        (_num(bin_data.get("wins")) + 2.0)
        / (_num(bin_data.get("total")) + 4.0)
        * 100.0
        if bin_data
        else float(raw_conf)
    )
    return {
        "decision": decision,
        "probabilities": probabilities,
        "raw_confidence": int(raw_conf),
        "calibrated_confidence": round(calibrated, 2),
        "model_sample_count": state["sample_count"],
        "model_ready": state["ready"],
        "model_mode": "ONLINE_LOGISTIC" if state["ready"] else "HEURISTIC_WARMUP",
        "minimum_training_samples": MIN_TRAIN_SAMPLES,
    }


def _data_quality(
    snapshot: Mapping[str, Any],
    news: Mapping[str, Any],
    options: Mapping[str, Any],
    global_data: Mapping[str, Any],
) -> Tuple[int, List[str]]:
    score = 0
    reasons = []
    if snapshot.get("feed_connected"):
        score += 30
    else:
        reasons.append("MARKET_FEED_MISSING")
    if _num(snapshot.get("price")) > 0:
        score += 10
    if options.get("success") and _int(options.get("contract_count")) >= 4:
        score += 30
    else:
        reasons.append("BROKER_OPTION_DATA_PARTIAL")
    if news.get("fresh"):
        score += 15
    else:
        reasons.append("NEWS_DATA_NOT_FRESH")
    if len(global_data.get("items") or {}) >= 4:
        score += 15
    else:
        reasons.append("GLOBAL_MARKET_DATA_PARTIAL")
    return int(_clamp(score, 0, 100)), reasons


def build_advanced_decision(
    user_id: int,
    snapshot: Mapping[str, Any],
    base: Mapping[str, Any],
    news: Mapping[str, Any],
    options: Mapping[str, Any],
    global_data: Mapping[str, Any],
) -> Dict[str, Any]:
    quality, quality_reasons = _data_quality(snapshot, news, options, global_data)
    features = _feature_vector(snapshot, base, news, options, global_data)
    model = _advanced_predict(user_id, features, quality)
    side = model["decision"]
    selected = _select_contract(options, side)
    relation = (
        "SAME_AS_BASE"
        if side == _direction(base.get("decision"))
        else "ADVANCED_WOULD_BLOCK_BASE"
        if side == "NO_TRADE" and _direction(base.get("decision")) in {"CE", "PE"}
        else "ADVANCED_OPPOSITE_BASE"
        if side in {"CE", "PE"} and _direction(base.get("decision")) in {"CE", "PE"}
        else "ADVANCED_DIRECTIONAL_WHEN_BASE_WAITED"
        if side in {"CE", "PE"} and _direction(base.get("decision")) == "NO_TRADE"
        else "DIFFERENT"
    )
    reasons = list(quality_reasons)
    reasons.extend(
        [
            f"BROKER={options.get('broker') or 'NONE'}",
            f"OPTION_SENTIMENT={options.get('sentiment_direction')}",
            f"NEWS_BIAS={news.get('news_bias')}",
            f"GLOBAL_RISK={global_data.get('risk_direction')}",
            f"MODEL={model['model_mode']}",
        ]
    )
    return {
        "success": True,
        "version": VERSION,
        "mode": "ADVANCED_FUSION_SHADOW_ONLY",
        "decision": side,
        "confidence": model["calibrated_confidence"],
        "raw_confidence": model["raw_confidence"],
        "probabilities": model["probabilities"],
        "base_decision": _direction(base.get("decision")),
        "base_confidence": _int(base.get("confidence")),
        "relation_to_base": relation,
        "data_quality": quality,
        "features": dict(features),
        "option_intelligence": dict(options),
        "news_intelligence": dict(news),
        "global_intelligence": dict(global_data),
        "selected_option": selected,
        "model_sample_count": model["model_sample_count"],
        "model_ready": model["model_ready"],
        "model_mode": model["model_mode"],
        "minimum_training_samples": model["minimum_training_samples"],
        "reasons": reasons,
        "trade_blocking": False,
        "order_execution": False,
    }


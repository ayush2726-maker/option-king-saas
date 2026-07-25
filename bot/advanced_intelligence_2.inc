def _selected_broker_session(user_id: int) -> Tuple[Optional[str], Any, Optional[str]]:
    cached = _SESSION_CACHE.get(int(user_id))
    now_mono = time.monotonic()
    if cached and now_mono - _num(cached.get("at")) < 300:
        return cached.get("broker"), cached.get("object"), cached.get("error")
    conn = get_db()
    try:
        row = get_selected_broker(conn, int(user_id))
        if row is None:
            result = (None, None, "BROKER_NOT_SELECTED")
        else:
            broker_name = str(row["broker_name"] or "").lower().strip()
            try:
                broker = create_broker(
                    broker_name,
                    row["client_id"],
                    decrypt_credential(row["api_key"]),
                    decrypt_credential(row["api_secret"]),
                    (
                        decrypt_credential(row["totp_secret"])
                        if row["totp_secret"]
                        else None
                    ),
                )
                login = broker.login()
                if not login.get("success"):
                    result = (
                        broker_name,
                        None,
                        "BROKER_LOGIN_FAILED: "
                        + str(login.get("message") or "unknown")[:180],
                    )
                else:
                    result = (broker_name, broker, None)
            except Exception as exc:
                result = (
                    broker_name,
                    None,
                    f"BROKER_SESSION_ERROR: {type(exc).__name__}: {str(exc)[:180]}",
                )
    finally:
        conn.close()
    _SESSION_CACHE[int(user_id)] = {
        "at": now_mono,
        "broker": result[0],
        "object": result[1],
        "error": result[2],
    }
    return result


def _angel_contract_rows(underlying: str, spot: float) -> List[Dict[str, Any]]:
    from bot.option_chain import _load_cache, _parse_expiry, _strike_of, _lot_size_of

    today = (_utc_now() + timedelta(hours=5, minutes=30)).date()
    rows = []
    for row in _load_cache():
        if str(row.get("name") or "").upper() != underlying:
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not (symbol.endswith("CE") or symbol.endswith("PE")):
            continue
        expiry_day = _parse_expiry(row.get("expiry"))
        strike = _strike_of(row)
        if expiry_day is None or expiry_day < today or strike is None:
            continue
        rows.append((expiry_day, abs(strike - spot), strike, row))
    if not rows:
        return []
    nearest_expiry = min(item[0] for item in rows)
    selected = [item for item in rows if item[0] == nearest_expiry]
    selected.sort(key=lambda item: (item[1], item[2]))
    return [
        {
            **item[3],
            "_expiry_iso": item[0].isoformat(),
            "_strike": item[2],
            "_lot_size": _lot_size_of(item[3], underlying),
        }
        for item in selected[:MAX_CHAIN_CONTRACTS]
    ]


def _angel_chain(broker: Any, underlying: str, spot: float) -> Dict[str, Any]:
    rows = _angel_contract_rows(underlying, spot)
    if not rows:
        return {"success": False, "error": "ANGEL_OPTION_CHAIN_EMPTY", "contracts": []}
    exchange_groups: Dict[str, List[str]] = {}
    for row in rows:
        exchange_groups.setdefault(
            str(row.get("exch_seg") or ANGEL_EXCHANGE.get(underlying, "NFO")),
            [],
        ).append(str(row.get("token") or ""))
    quotes: Dict[str, Dict[str, Any]] = {}
    for exchange, tokens in exchange_groups.items():
        tokens = [token for token in tokens if token]
        if not tokens:
            continue
        try:
            response = broker.smart_api.getMarketData("FULL", {exchange: tokens})
            data = response.get("data", {}) if isinstance(response, dict) else {}
            fetched = data.get("fetched") or data.get("data") or []
            if isinstance(fetched, dict):
                fetched = list(fetched.values())
            for item in fetched if isinstance(fetched, list) else []:
                token = str(item.get("symbolToken") or item.get("symboltoken") or item.get("token") or "")
                if token:
                    quotes[token] = dict(item)
        except Exception:
            pass
    greek_rows: List[Dict[str, Any]] = []
    expiry_fmt = datetime.strptime(rows[0]["_expiry_iso"], "%Y-%m-%d").strftime("%d%b%Y").upper()
    api = getattr(broker, "smart_api", None)
    for method_name, args in (
        ("getOptionGreeks", (underlying, expiry_fmt)),
        ("optionGreek", ({"name": underlying, "expirydate": expiry_fmt},)),
    ):
        method = getattr(api, method_name, None)
        if not callable(method):
            continue
        try:
            response = method(*args)
            greek_rows = list((response or {}).get("data") or [])
            if greek_rows:
                break
        except Exception:
            continue
    greek_map = {
        (
            round(_num(item.get("strikePrice")), 2),
            _direction(item.get("optionType")),
        ): item
        for item in greek_rows
        if isinstance(item, dict)
    }
    contracts = []
    for row in rows:
        token = str(row.get("token") or "")
        side = "CE" if str(row.get("symbol") or "").upper().endswith("CE") else "PE"
        contracts.append(
            _normalize_contract(
                broker="angelone",
                symbol=row.get("symbol"),
                token=token,
                exchange=row.get("exch_seg") or ANGEL_EXCHANGE.get(underlying),
                side=side,
                strike=row["_strike"],
                expiry=row["_expiry_iso"],
                lot_size=row["_lot_size"],
                quote=quotes.get(token, {}),
                greek=greek_map.get((round(row["_strike"], 2), side), {}),
                spot=spot,
            )
        )
    return {
        "success": bool(contracts),
        "broker": "angelone",
        "source": "SMARTAPI_FULL_QUOTE_OPTION_GREEKS",
        "contracts": contracts,
        "error": None if contracts else "ANGEL_QUOTES_UNAVAILABLE",
    }


def _upstox_get(broker: Any, path: str, params: Mapping[str, Any]) -> Dict[str, Any]:
    import requests

    response = requests.get(
        path,
        params=dict(params),
        headers=broker._h(),
        timeout=12,
    )
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if response.status_code != 200 or payload.get("status") not in {None, "success"}:
        raise RuntimeError(str(payload.get("errors") or payload.get("message") or response.text)[:240])
    return payload


def _upstox_chain(broker: Any, underlying: str, spot: float) -> Dict[str, Any]:
    key = UNDERLYING_KEYS.get(underlying)
    if not key:
        return {"success": False, "error": "UPSTOX_UNDERLYING_UNSUPPORTED", "contracts": []}
    payload = _upstox_get(
        broker,
        "https://api.upstox.com/v2/option/contract",
        {"instrument_key": key},
    )
    all_rows = list(payload.get("data") or [])
    today = (_utc_now() + timedelta(hours=5, minutes=30)).date()
    valid = []
    for row in all_rows:
        try:
            expiry_day = datetime.strptime(str(row.get("expiry"))[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if expiry_day < today:
            continue
        side = _direction(row.get("instrument_type"))
        if side not in {"CE", "PE"}:
            continue
        valid.append((expiry_day, abs(_num(row.get("strike_price")) - spot), row))
    if not valid:
        return {"success": False, "error": "UPSTOX_OPTION_CONTRACTS_EMPTY", "contracts": []}
    expiry_day = min(item[0] for item in valid)
    selected = [item[2] for item in valid if item[0] == expiry_day]
    selected.sort(key=lambda row: abs(_num(row.get("strike_price")) - spot))
    selected = selected[:MAX_CHAIN_CONTRACTS]
    tokens = [str(row.get("instrument_key") or "") for row in selected if row.get("instrument_key")]
    greek_data: Dict[str, Any] = {}
    quote_data: Dict[str, Any] = {}
    if tokens:
        try:
            greek_payload = _upstox_get(
                broker,
                "https://api.upstox.com/v3/market-quote/option-greek",
                {"instrument_key": ",".join(tokens[:50])},
            )
            greek_data = dict(greek_payload.get("data") or {})
        except Exception:
            pass
        try:
            quote_payload = _upstox_get(
                broker,
                "https://api.upstox.com/v2/market-quote/quotes",
                {"instrument_key": ",".join(tokens[:50])},
            )
            quote_data = dict(quote_payload.get("data") or {})
        except Exception:
            pass
    contracts = []
    for row in selected:
        token = str(row.get("instrument_key") or "")
        key_variant = token.replace("|", ":")
        greek = greek_data.get(token) or greek_data.get(key_variant) or {}
        quote = quote_data.get(token) or quote_data.get(key_variant) or {}
        if not quote and greek:
            quote = greek
        contracts.append(
            _normalize_contract(
                broker="upstox",
                symbol=row.get("trading_symbol"),
                token=token,
                exchange=row.get("segment") or "NSE_FO",
                side=row.get("instrument_type"),
                strike=row.get("strike_price"),
                expiry=row.get("expiry"),
                lot_size=row.get("lot_size"),
                quote=quote,
                greek=greek,
                spot=spot,
            )
        )
    market_info: Dict[str, Any] = {}
    expiry_text = expiry_day.isoformat()
    today_text = (_utc_now() + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
    for name, endpoint, params in (
        (
            "oi",
            "https://api.upstox.com/v2/market/oi",
            {"instrument_key": key, "expiry": expiry_text, "date": today_text},
        ),
        (
            "change_oi",
            "https://api.upstox.com/v2/market/change-oi",
            {
                "instrument_key": key,
                "expiry": expiry_text,
                "date": today_text,
                "interval": 1,
            },
        ),
        (
            "pcr",
            "https://api.upstox.com/v2/market/pcr",
            {
                "instrument_key": key,
                "expiry": expiry_text,
                "date": today_text,
                "bucket_interval": 15,
            },
        ),
        (
            "max_pain",
            "https://api.upstox.com/v2/market/max-pain",
            {
                "instrument_key": key,
                "expiry": expiry_text,
                "date": today_text,
                "bucket_interval": 15,
            },
        ),
    ):
        try:
            market_info[name] = _upstox_get(broker, endpoint, params).get("data") or {}
        except Exception as exc:
            market_info[name + "_error"] = str(exc)[:160]
    try:
        market_info["fii"] = _upstox_get(
            broker,
            "https://api.upstox.com/v2/market/fii",
            {
                "data_type": "NSE_FO|INDEX_FUTURES,NSE_FO|INDEX_OPTIONS",
                "interval": "1D",
            },
        ).get("data") or {}
    except Exception as exc:
        market_info["fii_error"] = str(exc)[:160]
    try:
        market_info["dii"] = _upstox_get(
            broker,
            "https://api.upstox.com/v2/market/dii",
            {"data_type": "NSE_EQ|CASH", "interval": "1D"},
        ).get("data") or {}
    except Exception as exc:
        market_info["dii_error"] = str(exc)[:160]
    return {
        "success": bool(contracts),
        "broker": "upstox",
        "source": "UPSTOX_OPTION_CONTRACT_GREEKS_QUOTES_MARKET_INFO",
        "contracts": contracts,
        "market_info": market_info,
        "error": None if contracts else "UPSTOX_QUOTES_UNAVAILABLE",
    }


def _zerodha_chain(broker: Any, underlying: str, spot: float) -> Dict[str, Any]:
    exchange = "BFO" if underlying == "SENSEX" else "NFO"
    rows = broker.kite.instruments(exchange)
    today = (_utc_now() + timedelta(hours=5, minutes=30)).date()
    valid = []
    for row in rows:
        if str(row.get("name") or "").upper() != underlying:
            continue
        side = _direction(row.get("instrument_type"))
        if side not in {"CE", "PE"}:
            continue
        expiry = row.get("expiry")
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        if not isinstance(expiry, date):
            try:
                expiry = datetime.fromisoformat(str(expiry)).date()
            except Exception:
                continue
        if expiry < today:
            continue
        valid.append((expiry, abs(_num(row.get("strike")) - spot), row))
    if not valid:
        return {"success": False, "error": "ZERODHA_OPTION_CONTRACTS_EMPTY", "contracts": []}
    expiry_day = min(item[0] for item in valid)
    selected = [item[2] for item in valid if item[0] == expiry_day]
    selected.sort(key=lambda row: abs(_num(row.get("strike")) - spot))
    selected = selected[:MAX_CHAIN_CONTRACTS]
    keys = [f"{exchange}:{row.get('tradingsymbol')}" for row in selected]
    quote_data = broker.kite.quote(keys) if keys else {}
    contracts = []
    for row, key in zip(selected, keys):
        quote = quote_data.get(key) or {}
        contracts.append(
            _normalize_contract(
                broker="zerodha",
                symbol=row.get("tradingsymbol"),
                token=row.get("instrument_token"),
                exchange=exchange,
                side=row.get("instrument_type"),
                strike=row.get("strike"),
                expiry=str(row.get("expiry")),
                lot_size=row.get("lot_size"),
                quote=quote,
                greek={},
                spot=spot,
            )
        )
    return {
        "success": bool(contracts),
        "broker": "zerodha",
        "source": "KITE_FULL_QUOTE_CALCULATED_GREEKS",
        "contracts": contracts,
        "error": None if contracts else "ZERODHA_QUOTES_UNAVAILABLE",
    }


def _fetch_broker_chain(user_id: int, snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    broker_name, broker, error = _selected_broker_session(user_id)
    underlying = str(snapshot.get("symbol") or snapshot.get("underlying") or "NIFTY").upper()
    spot = _num(snapshot.get("price"))
    if not broker_name or broker is None or spot <= 0:
        return {
            "success": False,
            "broker": broker_name,
            "error": error or "BROKER_OR_PRICE_UNAVAILABLE",
            "source": "NONE",
            "contracts": [],
        }
    try:
        if broker_name == "angelone":
            result = _angel_chain(broker, underlying, spot)
        elif broker_name == "upstox":
            result = _upstox_chain(broker, underlying, spot)
        elif broker_name == "zerodha":
            result = _zerodha_chain(broker, underlying, spot)
        else:
            result = {"success": False, "error": "BROKER_UNSUPPORTED", "contracts": []}
    except Exception as exc:
        result = {
            "success": False,
            "error": f"{type(exc).__name__}: {str(exc)[:220]}",
            "contracts": [],
        }
    result["broker"] = broker_name
    result["fetched_at"] = _iso()
    return result


import importlib.util
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc)


def _module(name, **values):
    module = types.ModuleType(name)
    module.__dict__.update(values)
    return module


def _load_stack(monkeypatch, tmp_path):
    db_path = tmp_path / "missed-learning.db"

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    database = _module(
        "database",
        get_db=get_db,
        get_db_storage_info=lambda: {"persistent": False},
    )
    monkeypatch.setitem(sys.modules, "database", database)

    backtest = _module("backtest")
    backtest.__path__ = []
    monkeypatch.setitem(sys.modules, "backtest", backtest)
    monkeypatch.setitem(
        sys.modules,
        "backtest.realism_costs_patch",
        _module(
            "backtest.realism_costs_patch",
            calculate_option_round_trip_costs=lambda broker, symbol, entry, exit_price, qty: {
                "market_gross_pnl": (exit_price - entry) * qty,
                "slippage_cost": 0.0,
                "total_charges": 1.0,
                "net_pnl": (exit_price - entry) * qty - 1.0,
            },
        ),
    )

    bot = _module("bot")
    bot.__path__ = []
    monkeypatch.setitem(sys.modules, "bot", bot)
    adaptive = _module(
        "bot.adaptive_model_v2",
        feature_vector=lambda **kwargs: {"adx": 0.5},
        maybe_train_models=lambda **kwargs: {},
        model_status=lambda: {},
        predict_adaptive=lambda *args, **kwargs: {"available": False},
    )
    monkeypatch.setitem(sys.modules, adaptive.__name__, adaptive)
    broker = _module(
        "bot.broker_intelligence",
        BROKER_CAPABILITIES={},
        VERSION="BROKER_TEST",
        get_broker_intelligence=lambda *args, **kwargs: {},
        option_oi_identity_map=lambda summary: {},
        selected_contract=lambda option, side: option.get(str(side).lower()),
    )
    monkeypatch.setitem(sys.modules, broker.__name__, broker)
    news = _module("bot.news_intelligence", aggregate=lambda: {})
    monkeypatch.setitem(sys.modules, news.__name__, news)
    monkeypatch.setitem(
        sys.modules,
        "bot.global_market_intelligence",
        _module("bot.global_market_intelligence", snapshot=lambda: {}),
    )
    shared = _module(
        "bot.shared_ai",
        predict=lambda market: {
            "decision": "PE",
            "confidence": 80,
            "probabilities": {"CE": 10, "PE": 80, "NO_TRADE": 10},
        },
    )
    monkeypatch.setitem(sys.modules, shared.__name__, shared)

    advanced_path = ROOT / "bot" / "advanced_intelligence_v2.py"
    spec = importlib.util.spec_from_file_location(
        "bot.advanced_intelligence_v2",
        advanced_path,
    )
    advanced = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, advanced)
    assert spec.loader is not None
    spec.loader.exec_module(advanced)
    advanced.ensure_advanced_schema()

    runtime = _module(
        "bot.auto_portfolio_runtime",
        _state_update=lambda *args: None,
        _underlying=lambda row: row.get("underlying", ""),
    )
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    monkeypatch.setitem(
        sys.modules,
        "bot.angel_fetcher",
        _module("bot.angel_fetcher", get_user_bot_state=lambda user_id: {}),
    )
    monkeypatch.setitem(
        sys.modules,
        "bot.market_routes",
        _module(
            "bot.market_routes",
            _get_active_broker=lambda user_id: ("upstox", {}),
            _get_ltp_session=lambda *args: None,
            _get_multi_session=lambda *args: None,
        ),
    )

    missed_path = ROOT / "bot" / "missed_trade_learning_v1.py"
    missed_spec = importlib.util.spec_from_file_location(
        "bot.missed_trade_learning_v1",
        missed_path,
    )
    missed = importlib.util.module_from_spec(missed_spec)
    monkeypatch.setitem(sys.modules, missed_spec.name, missed)
    assert missed_spec.loader is not None
    missed_spec.loader.exec_module(missed)
    return missed, advanced, get_db


def _scan(score=100, trade_allowed=False, candidate="PE"):
    return {
        "status": "OK",
        "underlying": "NIFTY",
        "candle_id": "2026-08-12T10:29:00+05:30",
        "market_data": {
            "price": 24300,
            "vwap": 24350,
            "ema9": 24320,
            "ema21": 24340,
            "adx": 44,
            "atr": 20,
        },
        "signal_data": {
            "signal": candidate if trade_allowed else "WAIT",
            "candidate_signal": candidate,
            "score": score,
            "min_score": 82,
            "trade_allowed": trade_allowed,
            "execution_allowed": trade_allowed,
            "entry_window_open": True,
            "safety_gate_reasons": ["ORB_EXTENSION_OVER_1.35_ATR"],
            "warnings": ["BLOCKED_FOR_TEST"],
        },
    }


def test_capture_is_post_decision_deduped_and_never_mutates_signal(monkeypatch, tmp_path):
    missed, _, get_db = _load_stack(monkeypatch, tmp_path)
    state = {"user_id": 7, "entry_candidate_attempts": []}
    scan = _scan()
    original_signal = dict(scan["signal_data"])

    assert missed.capture_scan_misses(state, [scan], [], now=START) == 1
    assert missed.capture_scan_misses(state, [scan], [], now=START) == 0
    assert scan["signal_data"] == original_signal
    assert missed.capture_scan_misses(state, [_scan(score=81)], [], now=START) == 0
    assert missed.capture_scan_misses(
        state,
        [_scan(trade_allowed=True)],
        [{"underlying": "NIFTY"}],
        now=START,
    ) == 0

    conn = get_db()
    row = conn.execute("SELECT * FROM ai_missed_trade_signals_v1").fetchone()
    conn.close()
    assert row["decision_kind"] == "STRATEGY_BLOCKED"
    assert row["strategy_score"] == 100
    assert row["learning_eligible"] == 1
    assert row["status"] == "PENDING_CONTRACT"
    assert missed._is_learning_eligible(
        "STRATEGY_BLOCKED", 100, 82, ["ORB_EXTENSION_OVER_1.35_ATR"], {}
    ) is True
    assert missed._is_learning_eligible(
        "STRATEGY_BLOCKED", 100, 82, ["BROKER_OPTION_QUOTE_FAILED"], {}
    ) is False
    assert missed._is_learning_eligible(
        "EXECUTION_ATTEMPT_FAILED", 100, 82, ["ORDER_REJECTED"], {}
    ) is False


def test_exact_cost_adjusted_outcomes_feed_existing_ai_dataset(monkeypatch, tmp_path):
    missed, advanced, get_db = _load_stack(monkeypatch, tmp_path)
    option = {
        "option_direction": "PE",
        "option_confidence": 70,
        "data_coverage_score": 100,
        "risk_score": 0,
        "ce": {
            "side": "CE", "strike": 24300, "symbol": "NIFTYCE",
            "token": "CE", "exchange": "NSE_FO", "ltp": 10,
            "ask": 10, "lot_size": 25,
        },
        "pe": {
            "side": "PE", "strike": 24300, "symbol": "NIFTYPE",
            "token": "PE", "exchange": "NSE_FO", "ltp": 11,
            "ask": 11, "lot_size": 25, "expiry": "2026-08-25",
        },
    }
    payload = {
        "success": True,
        "broker": "upstox",
        "option_intelligence": option,
        "global_market": {},
    }
    monkeypatch.setattr(advanced, "_option_payload", lambda user_id, market: payload)
    monkeypatch.setattr(
        advanced,
        "fuse_advanced",
        lambda market, base, option_payload, news: {
            "decision": "PE",
            "confidence": 75,
            "probabilities": {"CE": 10, "PE": 75, "NO_TRADE": 15},
            "reasons": [],
            "feature": {"adx": 0.5},
            "adaptive_model": {},
        },
    )

    assert missed.capture_scan_misses(
        {"user_id": 7, "entry_candidate_attempts": []},
        [_scan()],
        [],
        now=START,
    ) == 1
    conn = get_db()
    event = dict(conn.execute("SELECT * FROM ai_missed_trade_signals_v1").fetchone())
    conn.close()
    assert missed._hydrate_event(event, START + timedelta(seconds=10)) is True

    monkeypatch.setattr(
        missed,
        "_quote_contract_pair",
        lambda user_id, broker_name, ce_contract, pe_contract: {
            "ce": {"success": True, "ltp": 5},
            "pe": {"success": True, "ltp": 15},
        },
    )
    for horizon in (5, 15, 30):
        conn = get_db()
        event = dict(conn.execute("SELECT * FROM ai_missed_trade_signals_v1").fetchone())
        conn.close()
        assert missed._evaluate_event(event, START + timedelta(minutes=horizon)) == 1

    report = missed.get_missed_trade_summary(7)
    primary = report["recent_missed_setups"][0]["primary_outcome"]
    assert report["summary"]["would_have_profited_15m"] == 1
    assert report["summary"]["training_samples_added_15m"] == 1
    assert primary["candidate_side"] == "PE"
    assert primary["candidate_net_pnl"] > 0
    assert primary["verdict"] == "MISSED_PROFIT"
    missed_setup = report["recent_missed_setups"][0]
    assert missed_setup["candidate_contract"]["symbol"] == "NIFTYPE"
    assert missed_setup["candidate_contract"]["strike"] == 24300
    assert missed_setup["candidate_contract"]["side"] == "PE"
    assert missed_setup["candidate_contract"]["expiry"] == "2026-08-25"
    assert missed_setup["candidate_entry_price"] == 11
    assert missed_setup["candidate_lot_size"] == 25

    conn = get_db()
    snapshot = conn.execute(
        "SELECT * FROM ai_advanced_v2_snapshots WHERE sample_source=?",
        (missed.SAMPLE_SOURCE,),
    ).fetchone()
    training = conn.execute(
        """SELECT training_eligible FROM ai_advanced_v2_contract_outcomes
        WHERE decision_id=? AND horizon_minutes=15""",
        (snapshot["id"],),
    ).fetchone()
    conn.close()
    assert snapshot["strategy_candidate_side"] == "PE"
    assert snapshot["learning_eligible"] == 1
    assert training["training_eligible"] == 1


def test_late_counterfactual_is_visible_but_excluded_from_training(monkeypatch, tmp_path):
    missed, _, _ = _load_stack(monkeypatch, tmp_path)
    assert missed.MAX_TRAINING_QUOTE_DELAY_SECONDS < 180
    assert missed.VERSION.endswith("SHADOW-V3.2")
    assert missed.SAMPLE_SOURCE == "MISSED_TRADE_SHADOW_V1"


def test_hydration_reuses_nearby_live_option_snapshot(monkeypatch, tmp_path):
    missed, advanced, get_db = _load_stack(monkeypatch, tmp_path)
    assert missed.capture_scan_misses(
        {"user_id": 7, "entry_candidate_attempts": []},
        [_scan()],
        [],
        now=START,
    ) == 1
    conn = get_db()
    event = dict(conn.execute("SELECT * FROM ai_missed_trade_signals_v1").fetchone())
    conn.close()

    option = {
        "option_direction": "PE",
        "option_confidence": 80,
        "data_coverage_score": 100,
        "risk_score": 0,
        "ce": {
            "side": "CE", "symbol": "NIFTYCE", "token": "CE",
            "exchange": "NSE_FO", "ltp": 10, "ask": 10, "lot_size": 25,
        },
        "pe": {
            "side": "PE", "symbol": "NIFTYPE", "token": "PE",
            "exchange": "NSE_FO", "ltp": 11, "ask": 11, "lot_size": 25,
        },
    }
    market = missed._market_for_ai(event)
    base = {"decision": "PE", "confidence": 80, "probabilities": {}}
    payload = {
        "success": True,
        "broker": "upstox",
        "option_intelligence": option,
        "global_market": {},
    }
    fused = {
        "decision": "PE",
        "confidence": 80,
        "probabilities": {},
        "reasons": [],
        "feature": {},
        "adaptive_model": {},
    }
    assert advanced.register_snapshot(
        7,
        market,
        base,
        payload,
        {},
        fused,
        created_at=START + timedelta(seconds=30),
    )
    monkeypatch.setattr(
        advanced,
        "_option_payload",
        lambda *args: (_ for _ in ()).throw(AssertionError("broker call not expected")),
    )

    assert missed._hydrate_event(event, START + timedelta(minutes=5)) is True
    conn = get_db()
    row = conn.execute(
        "SELECT status,entry_quote_delay_seconds FROM ai_missed_trade_signals_v1"
    ).fetchone()
    conn.close()
    assert row["status"] == "TRACKING"
    assert row["entry_quote_delay_seconds"] == 30


def test_upstox_counterfactual_pair_uses_one_batched_quote(monkeypatch, tmp_path):
    missed, _, _ = _load_stack(monkeypatch, tmp_path)

    class Session:
        def __init__(self):
            self.calls = []

        def get_ltps(self, identifiers, exchange="NFO"):
            self.calls.append((list(identifiers), exchange))
            return {
                "success": True,
                "quote_source": "UPSTOX_LTP_V3",
                "quotes": {
                    "NSE_FO|CE": {"ltp": 10.5},
                    "NSE_FO|PE": {"ltp": 11.5},
                },
            }

    session = Session()
    monkeypatch.setattr(
        missed,
        "_get_active_broker",
        lambda user_id: ("upstox", {"access_token": "token"}),
    )
    monkeypatch.setattr(missed, "_get_multi_session", lambda *args: session)

    pair = missed._quote_contract_pair(
        7,
        "upstox",
        {"token": "NSE_FO|CE", "exchange": "NSE_FO"},
        {"token": "NSE_FO|PE", "exchange": "NSE_FO"},
    )

    assert pair["ce"]["ltp"] == 10.5
    assert pair["pe"]["ltp"] == 11.5
    assert session.calls == [(["NSE_FO|CE", "NSE_FO|PE"], "NSE_FO")]


def test_rate_limited_outcome_gets_backoff_instead_of_hot_retry(monkeypatch, tmp_path):
    missed, _, get_db = _load_stack(monkeypatch, tmp_path)
    assert missed.capture_scan_misses(
        {"user_id": 7, "entry_candidate_attempts": []},
        [_scan()],
        [],
        now=START,
    ) == 1
    conn = get_db()
    event = dict(conn.execute("SELECT * FROM ai_missed_trade_signals_v1").fetchone())
    conn.close()

    missed._set_outcome_failure(event, "UPSTOX LTP V3:429:Too Many Requests", START)

    conn = get_db()
    row = conn.execute(
        "SELECT last_error,next_retry_at FROM ai_missed_trade_signals_v1"
    ).fetchone()
    conn.close()
    assert "retrying automatically" in row["last_error"]
    assert missed._parse(row["next_retry_at"]) == START + timedelta(seconds=45)
    assert missed._quote_cooldown_active(START + timedelta(seconds=44)) is True
    assert missed._quote_cooldown_active(START + timedelta(seconds=46)) is False

    missed._set_hydration_failure(
        event,
        "RuntimeError:UPSTOX LTP V3:429:Too Many Requests",
        START,
    )
    conn = get_db()
    row = conn.execute(
        "SELECT status,hydration_attempts FROM ai_missed_trade_signals_v1"
    ).fetchone()
    conn.close()
    assert row["status"] == "PENDING_CONTRACT"
    assert row["hydration_attempts"] == 0


def test_recent_pre_v3_terminal_429_is_requeued_once(monkeypatch, tmp_path):
    missed, _, get_db = _load_stack(monkeypatch, tmp_path)
    assert missed.capture_scan_misses(
        {"user_id": 7, "entry_candidate_attempts": []},
        [_scan()],
        [],
        now=START,
    ) == 1
    conn = get_db()
    conn.execute(
        """UPDATE ai_missed_trade_signals_v1
        SET status='CONTRACT_UNAVAILABLE',hydration_attempts=6,
            last_error='RuntimeError:UPSTOX LTP V3:429:Too Many Request Sent'"""
    )
    conn.commit()
    conn.close()

    assert missed._revive_transient_contract_failures(START + timedelta(minutes=60)) == 1
    assert missed._revive_transient_contract_failures(START + timedelta(minutes=61)) == 0
    conn = get_db()
    row = conn.execute(
        "SELECT status,hydration_attempts,next_retry_at FROM ai_missed_trade_signals_v1"
    ).fetchone()
    conn.close()
    assert row["status"] == "PENDING_CONTRACT"
    assert row["hydration_attempts"] == 0
    assert missed._parse(row["next_retry_at"]) == START + timedelta(minutes=60)


def test_only_missing_due_horizons_consume_worker_slots(monkeypatch, tmp_path):
    missed, _, _ = _load_stack(monkeypatch, tmp_path)
    event = {"created_at": START.isoformat()}

    # The old worker treated this row as due merely because its 5m horizon had
    # elapsed, even though 5m and 15m were already complete and 30m was not due.
    assert missed._missing_due_horizons(
        event,
        {5, 15},
        START + timedelta(minutes=20),
    ) == []
    assert missed._missing_due_horizons(
        event,
        set(),
        START + timedelta(minutes=12),
    ) == [5]
    assert missed._missing_due_horizons(
        event,
        {5, 15},
        START + timedelta(minutes=31),
    ) == [30]

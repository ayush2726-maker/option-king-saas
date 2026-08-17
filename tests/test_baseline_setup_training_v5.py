import importlib.util
from pathlib import Path


def _module():
    path = (
        Path(__file__).resolve().parents[1]
        / "bot"
        / "baseline_setup_training_v5_patch.py"
    )
    spec = importlib.util.spec_from_file_location("baseline_setup_training_v5_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_qualified_baseline_setup_becomes_training_features_only():
    module = _module()
    feature = module._setup_features(
        {
            "signal_direction": "CE",
            "strategy_score": 88,
            "min_strategy_score": 82,
            "server_trade_allowed": True,
            "execution_allowed": True,
            "price": 24380,
            "ema_fast": 24360,
            "ema_slow": 24340,
            "vwap": 24350,
            "supertrend_direction": "UPTREND",
            "mtf_direction": "CE",
            "structure_direction": "BULLISH",
            "orb_high": 24360,
            "orb_low": 24250,
        },
        {"probabilities": {"CE": 68, "PE": 20, "NO_TRADE": 12}},
    )

    assert feature["setup_candidate_ce"] == 1.0
    assert feature["setup_qualified"] == 1.0
    assert feature["setup_trade_allowed"] == 1.0
    assert feature["setup_execution_allowed"] == 1.0
    assert feature["setup_score_margin"] == 0.2
    assert feature["setup_base_alignment"] == 0.48
    assert feature["setup_indicator_alignment"] == 1.0
    assert feature["setup_orb_alignment"] == 1.0


def test_historical_rows_reconstruct_real_strategy_context_without_future_data():
    module = _module()
    feature = module._historical_setup(
        {
            "strategy_candidate_side": "PE",
            "strategy_score": 85,
            "strategy_min_score": 82,
            "strategy_trade_allowed": 0,
            "strategy_execution_allowed": 0,
        },
        {},
    )

    assert feature["setup_candidate_pe"] == 1.0
    assert feature["setup_qualified"] == 1.0
    assert feature["setup_trade_allowed"] == 0.0
    assert feature["setup_execution_allowed"] == 0.0
    assert round(feature["setup_score_margin"], 4) == 0.1
    assert feature["setup_indicator_alignment"] == 0.0


def test_persisted_decision_time_feature_wins_over_reconstruction():
    module = _module()
    feature = module._historical_setup(
        {
            "strategy_candidate_side": "CE",
            "strategy_score": 88,
            "strategy_min_score": 82,
            "strategy_trade_allowed": 1,
            "strategy_execution_allowed": 1,
        },
        {"setup_indicator_alignment": 0.8, "setup_orb_alignment": 1.0},
    )

    assert feature["setup_indicator_alignment"] == 0.8
    assert feature["setup_orb_alignment"] == 1.0

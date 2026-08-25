import importlib.util
from pathlib import Path
import types

import numpy as np


def _module():
    path = Path(__file__).resolve().parents[1] / "bot" / "adaptive_accuracy_v6_patch.py"
    spec = importlib.util.spec_from_file_location("adaptive_accuracy_v6_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _model():
    model = types.SimpleNamespace()
    model.FEATURE_NAMES = [
        "base_ce", "base_pe", "base_no_trade", "news_ce", "news_pe",
        "setup_candidate_ce", "setup_candidate_pe", "setup_score_margin",
        "adx", "rsi", "option_ce", "option_pe", "coverage",
        "gainzalgo_ce", "gainzalgo_pe", "gainzalgo_available",
        "free_indicator_available", "choppiness_index",
        "squeeze_momentum", "squeeze_direction_ce", "squeeze_direction_pe",
    ]
    model.NEWS_FEATURES = ("news_ce", "news_pe")
    model.NEWS_ABLATION_FEATURES = ("news_ce", "news_pe")
    model.LABELS = ("CE", "PE", "NO_TRADE")
    model._loads = lambda value, default: value if isinstance(value, dict) else default
    return model


def test_economic_label_prefers_profitable_setup_direction():
    module = _module()
    model = _model()
    values = [0.65, 0.20, 0.15, 0, 0, 1, 0, 0.2, 0.5, 0.6, 0.6, 0.2, 0.9]
    stored = {
        "ce_net_pnl": 420,
        "pe_net_pnl": -250,
        "ce_entry_price": 200,
        "pe_entry_price": 180,
        "details_json": {"ce": {"quantity": 50}, "pe": {"quantity": 50}},
    }
    assert module._economic_label(model, values, stored) == "CE"


def test_economic_label_rejects_tiny_post_cost_move():
    module = _module()
    model = _model()
    values = [0.65, 0.20, 0.15, 0, 0, 1, 0, 0.2, 0.5, 0.6, 0.6, 0.2, 0.9]
    stored = {
        "ce_net_pnl": 25,
        "pe_net_pnl": -80,
        "ce_entry_price": 200,
        "pe_entry_price": 180,
        "details_json": {"ce": {"quantity": 50}, "pe": {"quantity": 50}},
    }
    assert module._economic_label(model, values, stored) == "NO_TRADE"


def test_candidate_sets_never_include_news_or_news_derived_fields():
    module = _module()
    model = _model()
    candidates = module._feature_candidates(model)
    for indices in candidates.values():
        selected = {model.FEATURE_NAMES[index] for index in indices}
        assert "news_ce" not in selected
        assert "news_pe" not in selected


def test_gainzalgo_can_compete_as_fusion_candidate_without_becoming_a_gate():
    module = _module()
    model = _model()
    candidates = module._feature_candidates(model)
    selected = {model.FEATURE_NAMES[index] for index in candidates["GAINZALGO_FUSION"]}
    assert "gainzalgo_ce" in selected
    assert "gainzalgo_pe" in selected
    assert "setup_score_margin" in selected


def test_free_regime_features_compete_without_becoming_a_gate():
    module = _module()
    model = _model()
    candidates = module._feature_candidates(model)
    selected = {
        model.FEATURE_NAMES[index]
        for index in candidates["FREE_REGIME_FUSION"]
    }
    assert "choppiness_index" in selected
    assert "squeeze_momentum" in selected
    assert "squeeze_direction_ce" in selected
    assert "setup_score_margin" in selected


def test_baseline_blend_is_normalized_and_can_correct_noisy_model():
    module = _module()
    learned = np.asarray([[0.25, 0.50, 0.25]])
    baseline = np.asarray([[0.80, 0.10, 0.10]])
    mixed = module._mix_probabilities(np, learned, baseline, 0.75, 1.0)
    assert np.isclose(mixed.sum(), 1.0)
    assert int(mixed.argmax(axis=1)[0]) == 0

import importlib.util
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_market_mechanics_reads_canonical_premium_fields():
    mechanics = _load(
        "market_mechanics_premium_test",
        "bot/market_mechanics_learning_v4_patch.py",
    )
    derived = mechanics._derive(
        {
            "ce_premium_pct_spot": 0.30,
            "pe_premium_pct_spot": 0.10,
        }
    )
    assert round(derived["premium_expensive"], 6) == 0.075
    assert round(derived["premium_asymmetry"], 6) == 0.4


def test_market_mechanics_keeps_legacy_premium_alias_fallback():
    mechanics = _load(
        "market_mechanics_legacy_premium_test",
        "bot/market_mechanics_learning_v4_patch.py",
    )
    derived = mechanics._derive(
        {
            "ce_premium_to_spot": 0.25,
            "pe_premium_to_spot": 0.05,
        }
    )
    assert round(derived["premium_expensive"], 6) == 0.0625
    assert round(derived["premium_asymmetry"], 6) == 0.4


def test_exact_premium_features_are_normalized_from_entry_snapshot():
    premium = _load(
        "option_premium_feature_test",
        "bot/option_premium_learning_v3_patch.py",
    )
    result = premium._premium_features(
        20_000,
        {"ask": 200, "delta": 0.55, "theta": -8},
        {"ask": 100, "delta": -0.45, "theta": -4},
    )
    assert round(result["ce_premium_pct_spot"], 6) == 0.2
    assert round(result["pe_premium_pct_spot"], 6) == 0.1
    assert round(result["premium_skew"], 6) == round(1 / 3, 6)
    assert result["ce_abs_delta"] == 0.55
    assert result["pe_abs_delta"] == 0.45
    assert round(result["ce_theta_pressure"], 6) == 0.8
    assert round(result["pe_theta_pressure"], 6) == 0.8


def test_historical_training_rows_reconstruct_premium_without_future_data(monkeypatch):
    premium = _load(
        "option_premium_history_test",
        "bot/option_premium_learning_v3_patch.py",
    )

    stored = {
        "spot": 20_000,
        "feature_json": "{}",
        "ce_contract_json": json.dumps(
            {"ask": 200, "delta": 0.55, "theta": -8}
        ),
        "pe_contract_json": json.dumps(
            {"ask": 100, "delta": -0.45, "theta": -4}
        ),
    }

    class Connection:
        def execute(self, query, params):
            assert params == (15,)
            assert "ce_contract_json" in query
            return self

        def fetchall(self):
            return [stored]

        def close(self):
            pass

    model = types.ModuleType("bot.adaptive_model_v2")
    model.FEATURE_NAMES = ["base_ce"]
    model.FEATURE_GROUPS = {}
    model.VERSION = "TEST"
    model.feature_vector = lambda **kwargs: {"base_ce": 0.4}
    model._training_rows = lambda horizon: ([[0.4]], [0])
    model.get_db = Connection
    model._loads = lambda value, default: json.loads(value) if value else default

    broker = types.ModuleType("bot.broker_intelligence")
    broker.selected_contract = lambda option, side: option.get(side.lower(), {})
    bot = types.ModuleType("bot")
    bot.__path__ = []
    bot.adaptive_model_v2 = model
    monkeypatch.setitem(sys.modules, "bot", bot)
    monkeypatch.setitem(sys.modules, model.__name__, model)
    monkeypatch.setitem(sys.modules, broker.__name__, broker)

    assert premium.apply_option_premium_learning_v3_patch() is True
    rows, labels = model._training_rows(15)
    row = dict(zip(model.FEATURE_NAMES, rows[0]))
    assert labels == [0]
    assert row["base_ce"] == 0.4
    assert round(row["ce_premium_pct_spot"], 6) == 0.2
    assert round(row["pe_premium_pct_spot"], 6) == 0.1
    assert round(row["premium_skew"], 6) == round(1 / 3, 6)


def test_news_ablation_excludes_news_derived_features():
    learning = _load(
        "adaptive_learning_news_ablation_test",
        "bot/adaptive_learning_v3_patch.py",
    )
    mechanics = _load(
        "market_mechanics_news_ablation_test",
        "bot/market_mechanics_learning_v4_patch.py",
    )
    expected = {
        "news_ce_alignment",
        "news_pe_alignment",
        "bullish_context",
        "bearish_context",
        "event_shock",
        "context_conflict",
        "direction_quality_ce",
        "direction_quality_pe",
    }
    assert expected == set(
        learning._NEWS_DERIVED_FEATURES + mechanics._NEWS_DERIVED_FEATURES
    )

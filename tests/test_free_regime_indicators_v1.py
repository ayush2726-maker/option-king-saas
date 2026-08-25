import importlib.util
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name="free_regime_indicators_test"):
    path = ROOT / "bot" / "free_regime_indicators_v1.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _trend(count=80, step=0.5):
    candles = []
    for index in range(count):
        close = 100.0 + index * step
        candles.append(
            {
                "open": close - step * 0.4,
                "high": close + 0.25,
                "low": close - 0.25,
                "close": close,
            }
        )
    return candles


def _choppy(count=80):
    candles = []
    for index in range(count):
        close = 100.0 + (1.0 if index % 2 else -1.0)
        candles.append(
            {
                "open": 100.0,
                "high": max(100.0, close) + 0.2,
                "low": min(100.0, close) - 0.2,
                "close": close,
            }
        )
    return candles


def test_uptrend_has_directional_momentum_and_lower_choppiness():
    module = _load("free_regime_trend_test")
    trending = module.free_regime_features({"completed_candles": _trend()})
    choppy = module.free_regime_features({"completed_candles": _choppy()})
    assert trending["free_indicator_available"] == 1.0
    assert trending["squeeze_direction_ce"] == 1.0
    assert trending["squeeze_direction_pe"] == 0.0
    assert trending["squeeze_momentum"] > 0.0
    assert trending["choppiness_index"] < choppy["choppiness_index"]
    assert trending["choppiness_trending"] > trending["choppiness_sideways"]


def test_quiet_price_inside_wide_true_range_is_in_squeeze():
    module = _load("free_regime_squeeze_test")
    candles = []
    for index in range(80):
        close = 100.0 + (0.01 if index % 2 else -0.01)
        candles.append(
            {
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": close,
            }
        )
    features = module.free_regime_features({"completed_candles": candles})
    assert features["squeeze_on"] == 1.0
    assert features["squeeze_release"] == 0.0


def test_insufficient_or_invalid_candles_stay_neutral():
    module = _load("free_regime_neutral_test")
    expected = {name: 0.0 for name in module.FEATURES}
    assert module.free_regime_features({"completed_candles": _trend(20)}) == expected
    assert module.free_regime_features(
        {"completed_candles": [{"open": 0, "high": 0, "low": 0, "close": 0}] * 80}
    ) == expected


def test_chart_conversion_drops_forming_last_candle_and_bounds_history():
    module = _load("free_regime_completed_candle_test")
    candles = _trend(100)
    for index, row in enumerate(candles):
        row["time"] = f"minute-{index}"
    completed = module.completed_chart_candles({"chart_candles": candles}, limit=80)
    assert len(completed) == 80
    assert completed[-1]["time"] == "minute-98"
    assert completed[-1]["close"] == candles[-2]["close"]


def test_persisted_features_are_reused_without_rebuilding_from_future_data(monkeypatch):
    module = _load("free_regime_training_test")
    stored = {
        "feature_json": json.dumps(
            {
                "free_indicator_available": 1,
                "choppiness_index": 0.31,
                "squeeze_direction_ce": 1,
            }
        )
    }

    class Connection:
        def execute(self, query, params):
            assert params == (15,)
            return self

        def fetchall(self):
            return [stored]

        def close(self):
            pass

    model = types.ModuleType("bot.adaptive_model_v2")
    model.FEATURE_NAMES = ["base_ce"]
    model.FEATURE_GROUPS = {}
    model.VERSION = "TEST"
    model.feature_vector = lambda **kwargs: {"base_ce": 0.5}
    model._training_rows = lambda horizon: ([[0.5]], [0])
    model.get_db = Connection
    model._loads = lambda value, default: json.loads(value) if value else default
    bot = types.ModuleType("bot")
    bot.__path__ = []
    bot.adaptive_model_v2 = model
    monkeypatch.setitem(sys.modules, "bot", bot)
    monkeypatch.setitem(sys.modules, model.__name__, model)

    assert module.apply_free_regime_indicators_v1_patch() is True
    rows, labels = model._training_rows(15)
    values = dict(zip(model.FEATURE_NAMES, rows[0]))
    assert labels == [0]
    assert values["free_indicator_available"] == 1.0
    assert values["choppiness_index"] == 0.31
    assert values["squeeze_direction_ce"] == 1.0
    assert values["squeeze_direction_pe"] == 0.0
    assert model.DECISION_AUTHORITY == "BASELINE_STRATEGY_ONLY"


def test_status_confirms_free_non_blocking_operation():
    module = _load("free_regime_status_test")
    status = module.free_regime_status()
    assert status["paid_service_required"] is False
    assert status["tradingview_required"] is False
    assert status["trade_blocking"] is False
    assert status["decision_authority"] == "BASELINE_STRATEGY_ONLY"

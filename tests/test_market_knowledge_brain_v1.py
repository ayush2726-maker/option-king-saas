import importlib.util
import json
import sys
import types
from pathlib import Path


def _load_module():
    database = types.ModuleType("database")
    database.get_db = lambda: (_ for _ in ()).throw(RuntimeError("db not used in pure tests"))
    adaptive = types.ModuleType("bot.adaptive_model_v2")
    adaptive.FEATURE_NAMES = ["adx", "rsi", "atr_percent", "volume_ratio"]
    adaptive.feature_vector = lambda **kwargs: {
        "adx": float(kwargs["market"].get("adx", 0)) / 60.0,
        "rsi": float(kwargs["market"].get("rsi", 50)) / 100.0,
        "atr_percent": float(kwargs["market"].get("atr_percent", 0)) / 5.0,
        "volume_ratio": float(kwargs["market"].get("volume_ratio", 0)) / 4.0,
    }
    bot = types.ModuleType("bot")
    bot.__path__ = []
    sys.modules["database"] = database
    sys.modules["bot"] = bot
    sys.modules["bot.adaptive_model_v2"] = adaptive
    path = Path(__file__).resolve().parents[1] / "bot" / "market_knowledge_brain_v1.py"
    spec = importlib.util.spec_from_file_location("market_knowledge_brain_v1_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_regime_marks_high_volatility_and_bad_liquidity():
    module = _load_module()
    regime = module.classify_regime(
        {
            "adx": 31,
            "rsi": 71,
            "atr_percent": 1.5,
            "volume_ratio": 1.8,
            "market_open": True,
            "feed_connected": True,
        },
        {
            "option_intelligence": {
                "average_spread_percent": 4.5,
                "data_coverage_score": 35,
                "risk_score": 85,
            },
            "global_market": {
                "values": {"india_vix": {"last_price": 26, "change_percent": 9}}
            },
        },
        {"news_risk_score": 70},
    )
    assert regime["trend"] == "STRONG_TREND"
    assert regime["volatility"] == "HIGH"
    assert regime["liquidity"] == "POOR_OR_UNRELIABLE"
    assert regime["event_risk"] == "HIGH"


def test_similar_memory_uses_only_completed_rows_and_prefers_ce(monkeypatch):
    module = _load_module()
    feature = {name: 0.5 for name in module.FEATURE_NAMES}
    rows = []
    for index in range(30):
        label = "CE" if index < 24 else "PE"
        rows.append(
            {
                "feature_json": json.dumps(feature),
                "best_label": label,
                "ce_net_pnl": 120.0,
                "pe_net_pnl": -80.0,
                "no_trade_net_pnl": 0.0,
                "symbol": "NIFTY",
            }
        )
    monkeypatch.setattr(module, "_historical_rows", lambda symbol, horizon: rows)
    memory = module.similar_regime_memory(feature, "NIFTY")
    assert memory["reliable"] is True
    assert memory["decision"] == "CE"
    assert memory["probabilities"]["CE"] > memory["probabilities"]["PE"]
    assert memory["leakage_safe"] is True


def test_shadow_blend_never_enables_orders_or_trade_blocking():
    module = _load_module()
    advanced = {
        "probabilities": {"CE": 45, "PE": 35, "NO_TRADE": 20},
        "decision": "CE",
        "confidence": 45,
        "reasons": [],
        "trade_blocking": True,
        "order_execution": True,
    }
    knowledge = {
        "similar_regime_memory": {
            "reliable": True,
            "support": 60,
            "average_similarity_percent": 90,
            "probabilities": {"CE": 10, "PE": 85, "NO_TRADE": 5},
        }
    }
    result = module.blend_shadow_prediction(advanced, knowledge)
    assert result["market_knowledge_blend_weight_percent"] <= 22.0
    assert sum(result["probabilities"].values()) == 100
    assert result["trade_blocking"] is False
    assert result["order_execution"] is False
    assert "MARKET_KNOWLEDGE_SIMILAR_REGIME_MEMORY" in result["reasons"]


def test_weak_memory_is_explanation_only():
    module = _load_module()
    advanced = {
        "probabilities": {"CE": 50, "PE": 30, "NO_TRADE": 20},
        "decision": "CE",
        "confidence": 50,
    }
    knowledge = {
        "similar_regime_memory": {
            "reliable": False,
            "support": 5,
            "average_similarity_percent": 95,
            "probabilities": {"CE": 0, "PE": 100, "NO_TRADE": 0},
        }
    }
    result = module.blend_shadow_prediction(advanced, knowledge)
    assert result["probabilities"] == advanced["probabilities"]
    assert result["market_knowledge_blend_weight_percent"] == 0.0

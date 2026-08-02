import importlib.util
import sys
import types
from pathlib import Path


def _load_module():
    database = types.ModuleType("database")
    database.get_db = lambda: (_ for _ in ()).throw(RuntimeError("db not used in pure tests"))
    sys.modules["database"] = database
    path = Path(__file__).resolve().parents[1] / "bot" / "sector_rotation_ai_training_v1.py"
    spec = importlib.util.spec_from_file_location("sector_rotation_ai_training_v1_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _payload(rotation="BROAD_POSITIVE"):
    return {
        "success": True,
        "index": "NIFTY",
        "source": "angelone",
        "market_open": True,
        "summary": {
            "rotation": rotation,
            "average_change_percent": 1.2,
            "advancers": 38,
            "decliners": 10,
            "breadth_percent": 76.0,
            "coverage": 48,
            "constituents": 50,
        },
        "sectors": [
            {"sector": "Financial Services", "average_change_percent": 2.1},
            {"sector": "Information Technology", "average_change_percent": 0.7},
            {"sector": "Healthcare", "average_change_percent": -0.6},
        ],
    }


def test_broad_positive_rotation_produces_ce_training_bias():
    module = _load_module()
    payload = _payload("BROAD_POSITIVE")
    features = module.extract_sector_features(payload)

    assert module.sector_bias(payload) == "CE"
    assert features["sector_rotation_direction"] == 1.0
    assert features["sector_breadth"] == 0.76
    assert features["sector_adv_decl_balance"] > 0
    assert features["sector_strongest_change"] > 0
    assert features["sector_weakest_change"] < 0


def test_negative_and_mixed_rotation_labels_are_stable():
    module = _load_module()
    assert module.sector_bias(_payload("BROAD_NEGATIVE")) == "PE"
    assert module.sector_bias(_payload("NEGATIVE_BIAS")) == "PE"
    assert module.sector_bias(_payload("MIXED")) == "NO_TRADE"


def test_annotation_is_training_only_and_never_executes_orders():
    module = _load_module()
    annotation = module.training_annotation(_payload())

    assert annotation["training_only"] is True
    assert annotation["trade_blocking"] is False
    assert annotation["order_execution"] is False
    assert annotation["version"] == "OKAI-SECTOR-ROTATION-AI-TRAINING-V1"


def test_index_aliases_are_normalized_for_training():
    module = _load_module()
    assert module.normalize_index("NIFTY 50") == "NIFTY"
    assert module.normalize_index("NIFTY_BANK") == "BANKNIFTY"
    assert module.normalize_index("BSE SENSEX") == "SENSEX"

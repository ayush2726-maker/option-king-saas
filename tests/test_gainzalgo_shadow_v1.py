import importlib.util
import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name="gainzalgo_shadow_test"):
    database = types.ModuleType("database")
    database.get_db = lambda: None
    previous_database = sys.modules.get("database")
    sys.modules["database"] = database
    path = ROOT / "bot" / "gainzalgo_shadow_v1.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    try:
        spec.loader.exec_module(module)
    finally:
        if previous_database is None:
            sys.modules.pop("database", None)
        else:
            sys.modules["database"] = previous_database
    return module


def _database(tmp_path):
    path = tmp_path / "gainzalgo.db"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    return connect


def test_direction_and_symbol_normalization():
    module = _load("gainzalgo_normalization_test")
    assert module._direction("buy") == "CE"
    assert module._direction("SELL") == "PE"
    assert module._direction("exit") == "NO_TRADE"
    assert module._symbol("NSE:NIFTY BANK") == "BANKNIFTY"
    assert module._symbol("BSE:SENSEX") == "SENSEX"


def test_fresh_alert_becomes_shadow_features(tmp_path, monkeypatch):
    module = _load("gainzalgo_feature_test")
    monkeypatch.setattr(module, "get_db", _database(tmp_path))
    result = module.record_gainzalgo_signal(
        {
            "event_id": "signal-1",
            "symbol": "NSE:NIFTY",
            "action": "BUY",
            "confidence": 78,
            "price": 25000,
        }
    )
    assert result["direction"] == "CE"
    assert result["trade_blocking"] is False
    features = module.gainzalgo_features({"symbol": "NIFTY"})
    assert features["gainzalgo_ce"] == 1.0
    assert features["gainzalgo_pe"] == 0.0
    assert features["gainzalgo_confidence"] == 0.78
    assert features["gainzalgo_available"] == 1.0
    assert features["gainzalgo_freshness"] > 0.99


def test_stale_alert_is_neutral_not_a_trade_block(tmp_path, monkeypatch):
    module = _load("gainzalgo_stale_test")
    monkeypatch.setattr(module, "get_db", _database(tmp_path))
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    module.record_gainzalgo_signal(
        {"event_id": "old-1", "symbol": "NIFTY", "signal": "SELL", "time": old.isoformat()}
    )
    assert module.gainzalgo_features({"symbol": "NIFTY"}) == {
        name: 0.0 for name in module.FEATURES
    }


def test_persisted_feature_is_reused_without_future_data(tmp_path, monkeypatch):
    module = _load("gainzalgo_training_test")
    stored = {"feature_json": json.dumps({"gainzalgo_ce": 1, "gainzalgo_available": 1})}

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

    assert module.apply_gainzalgo_shadow_v1_patch() is True
    rows, labels = model._training_rows(15)
    values = dict(zip(model.FEATURE_NAMES, rows[0]))
    assert labels == [0]
    assert values["gainzalgo_ce"] == 1.0
    assert values["gainzalgo_available"] == 1.0
    assert values["gainzalgo_pe"] == 0.0

import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "bot" / "upstox_sector_change_fix_v1.py"
    spec = importlib.util.spec_from_file_location("upstox_sector_change_fix_v1_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_net_change_overrides_live_ohlc_close_equal_to_ltp():
    module = _module()
    basis = module._upstox_change_basis(
        {
            "last_price": 100.0,
            "net_change": 2.0,
            "ohlc": {"close": 100.0},
        }
    )

    assert basis["previous_close"] == 98.0
    assert round(basis["change_percent"], 4) == 2.0408
    assert basis["change_source"] == "upstox_net_change"


def test_negative_net_change_produces_negative_live_percent():
    module = _module()
    basis = module._upstox_change_basis(
        {
            "last_price": 97.5,
            "net_change": -2.5,
            "ohlc": {"close": 97.5},
        }
    )

    assert basis["previous_close"] == 100.0
    assert basis["change_percent"] == -2.5


def test_older_ohlc_payload_remains_supported_when_net_change_missing():
    module = _module()
    basis = module._upstox_change_basis(
        {
            "last_price": 105.0,
            "ohlc": {"close": 100.0},
        }
    )

    assert basis["previous_close"] == 100.0
    assert basis["change_percent"] == 5.0
    assert basis["change_source"] == "upstox_ohlc_close_fallback"


def test_ltpc_close_payload_is_supported_without_inventing_values():
    module = _module()
    basis = module._upstox_change_basis({"ltpc": {"ltp": 204.0, "cp": 200.0}})

    assert basis["ltp"] == 204.0
    assert basis["previous_close"] == 200.0
    assert basis["change_percent"] == 2.0
    assert module.VERSION == "OKAI-UPSTOX-SECTOR-NET-CHANGE-V1"

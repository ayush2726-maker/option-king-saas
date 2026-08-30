"""Regression tests for the global BANKNIFTY new-entry disable."""
from strategy import routes as strategy_routes
from bot import auto_portfolio_runtime as portfolio_runtime
from backtest import routes as backtest_routes
from backtest import daily_job_start_patch, monthly_job_start_patch, range_routes


def test_strategy_defaults_exclude_banknifty():
    assert strategy_routes.ALLOWED_INSTRUMENTS == ["NIFTY", "SENSEX"]
    assert strategy_routes.DEFAULT_SETTINGS["enabled_instruments"] == [
        "NIFTY",
        "SENSEX",
    ]


def test_saved_banknifty_only_settings_fall_back_to_nifty():
    cleaned = strategy_routes._sanitize_instrument_settings(
        {
            "primary_instrument": "BANKNIFTY",
            "enabled_instruments": ["BANKNIFTY"],
            "entry_threshold": 82,
        }
    )
    assert cleaned["primary_instrument"] == "NIFTY"
    assert cleaned["enabled_instruments"] == ["NIFTY"]
    assert cleaned["entry_threshold"] == 82


def test_runtime_never_scans_banknifty_from_stale_saved_settings():
    enabled = portfolio_runtime._enabled(
        {
            "primary_instrument": "BANKNIFTY",
            "enabled_instruments": [
                "NIFTY",
                "BANKNIFTY",
                "SENSEX",
            ],
        }
    )
    assert enabled == ["NIFTY", "SENSEX"]
    assert "BANKNIFTY" not in enabled


def test_backtest_never_scans_or_accepts_banknifty():
    assert backtest_routes._OKAI_AUTO_INSTRUMENTS == ("NIFTY", "SENSEX")
    assert "BANKNIFTY" not in daily_job_start_patch._ALLOWED_INSTRUMENTS
    assert "BANKNIFTY" not in monthly_job_start_patch._ALLOWED_INSTRUMENTS
    assert "BANKNIFTY" not in range_routes._ALLOWED_INSTRUMENTS

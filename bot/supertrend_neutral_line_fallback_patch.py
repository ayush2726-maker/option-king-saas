"""Use the Supertrend line as a fallback when ST_DIR remains NEUTRAL.

Some broker/history feeds can produce a numeric Supertrend line while the carried
ST_DIR flag is still NEUTRAL. The AUTO safety gate then blocks otherwise strong
fresh trend trades with SUPERTREND_DIRECTION_REQUIRED even when price is clearly
above/below the Supertrend line.

This patch keeps the mandatory Supertrend confirmation, but repairs only the
NEUTRAL state from the actual line position:
- CE confirms when close/price is above the Supertrend line.
- PE confirms when close/price is below the Supertrend line.

It does not change order placement, position sizing, exits, SL, cooldown, or risk
logic.
"""

from __future__ import annotations

from backtest import routes as backtest_routes
from bot import angel_fetcher
from bot import routes as bot_routes
from bot import strategy
from bot import auto_portfolio_runtime as runtime
from bot import mandatory_trend_structure_patch as mandatory_patch


def _f(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _line_direction(price, line):
    price = _f(price, 0.0)
    line = _f(line, 0.0)
    if price <= 0 or line <= 0:
        return "NEUTRAL"
    if price > line:
        return "UP"
    if price < line:
        return "DOWN"
    return "NEUTRAL"


def _repair_frame(frame):
    if frame is None or getattr(frame, "empty", True):
        return frame
    columns = set(getattr(frame, "columns", []))
    if not {"close", "ST_DIR", "SUPERTREND"}.issubset(columns):
        return frame

    try:
        repaired = frame.copy()
        directions = []
        fallback_used = []
        for _, row in repaired.iterrows():
            current = str(row.get("ST_DIR") or "NEUTRAL").upper()
            if current in {"UP", "DOWN"}:
                directions.append(current)
                fallback_used.append(False)
                continue

            fallback = _line_direction(row.get("close"), row.get("SUPERTREND"))
            directions.append(fallback)
            fallback_used.append(fallback in {"UP", "DOWN"})

        repaired["ST_DIR"] = directions
        repaired["ST_LINE_FALLBACK_USED"] = fallback_used
        return repaired
    except Exception:
        return frame


def _repair_market(market):
    if not isinstance(market, dict):
        return market

    current = str(market.get("supertrend_dir") or "NEUTRAL").upper()
    if current in {"UP", "DOWN"}:
        return market

    line = market.get("supertrend_value") or market.get("supertrend") or market.get("SUPERTREND")
    fallback = _line_direction(market.get("price"), line)
    if fallback in {"UP", "DOWN"}:
        market["supertrend_dir"] = fallback
        market["supertrend_line_fallback_used"] = True
    return market


def _repair_scan(scan):
    if not isinstance(scan, dict):
        return scan

    market = scan.get("market_data")
    signal = scan.get("signal_data")
    if isinstance(market, dict):
        _repair_market(market)

    if isinstance(signal, dict) and isinstance(market, dict):
        try:
            fixed = mandatory_patch._normalize(signal, market)
            scan["signal_data"] = fixed
            scan["market_data"]["signal"] = fixed.get("signal", "WAIT")
            scan["market_data"]["signal_score"] = fixed.get("score", 0)
        except Exception:
            pass
    return scan


def _install_paper_market_close_patch():
    try:
        from bot.paper_market_close_1530_patch import (
            apply_paper_market_close_1530_patch,
        )

        apply_paper_market_close_1530_patch()
    except Exception as exc:
        try:
            print(f"Paper market-close patch skipped: {str(exc)[:160]}")
        except Exception:
            pass


def apply_supertrend_neutral_line_fallback_patch() -> None:
    # This function is invoked by the final active-strategy patch, after the
    # existing EOD guard is installed, so PAPER/LIVE clock windows can be split
    # without changing startup order.
    _install_paper_market_close_patch()

    if getattr(strategy, "_okai_supertrend_neutral_line_fallback_v1", False):
        return

    original_calculate_indicators = angel_fetcher.calculate_indicators

    def calculate_indicators_with_line_fallback(dataframe):
        result = original_calculate_indicators(dataframe)
        if result is None:
            return None
        frame, trend = result
        return _repair_frame(frame), trend

    angel_fetcher.calculate_indicators = calculate_indicators_with_line_fallback
    backtest_routes.calculate_indicators = calculate_indicators_with_line_fallback

    original_checks = mandatory_patch._checks

    def checks_with_line_fallback(signal, market):
        market = dict(market or {})
        _repair_market(market)
        return original_checks(signal, market)

    mandatory_patch._checks = checks_with_line_fallback

    original_get_full_signal = strategy.get_full_signal

    def get_full_signal_with_supertrend_fallback(market_data, consecutive_losses=0, profile=None):
        market = dict(market_data or {})
        _repair_market(market)
        return original_get_full_signal(
            market,
            consecutive_losses=consecutive_losses,
            profile=profile,
        )

    strategy.get_full_signal = get_full_signal_with_supertrend_fallback
    angel_fetcher.get_full_signal = get_full_signal_with_supertrend_fallback
    bot_routes.get_full_signal = get_full_signal_with_supertrend_fallback

    original_build_scan = runtime._build_scan

    def build_scan_with_supertrend_fallback(user_id, underlying, df, profile, loss_streak):
        return _repair_scan(original_build_scan(user_id, underlying, df, profile, loss_streak))

    runtime._build_scan = build_scan_with_supertrend_fallback
    strategy._okai_supertrend_neutral_line_fallback_v1 = True

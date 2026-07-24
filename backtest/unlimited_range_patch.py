"""Remove the artificial calendar-day ceiling from date-range backtests.

The run is still limited by completed dates and by historical data available from the
user's selected broker. Only one range job per user may run at a time.
"""

from __future__ import annotations


def apply_unlimited_range_patch() -> None:
    from backtest import range_routes

    # `range_routes._range_weekdays` compares the selected range against this
    # module global at request time, so infinity removes the application ceiling
    # without changing completed-date or weekday validation.
    range_routes._MAX_CALENDAR_DAYS = float("inf")

    # Multi-year real-premium runs can legitimately take several hours. Keep an
    # active job recoverable across app restarts instead of expiring it after 4h.
    range_routes._STALE_JOB_SECONDS = 14 * 24 * 60 * 60

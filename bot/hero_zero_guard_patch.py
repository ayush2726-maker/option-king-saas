"""Hero Zero execution guard.

Hero Zero uses a separate API route from AUTO Portfolio.  The route imported
`resolve_option` before the AUTO expiry patches were installed, so it could keep
using the old next-week resolver.  It also accepted requests throughout market
hours even though the UI documents a 14:30-15:00 IST expiry window.

This patch updates the route globals used at request time:
- Hero Zero start is accepted only from 14:30 through 14:59 IST.
- The resolved option must expire today for every supported underlying.
- If today's contract is unavailable, the entry is skipped rather than rolling
  into the following expiry.
"""

from datetime import date, datetime, timedelta, timezone

from bot import angel_fetcher
from bot import routes


def _now_ist():
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _parse_expiry(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value or "").strip().upper()
    for fmt in ("%Y-%m-%d", "%d%b%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    return None


def _hero_window_open():
    now = _now_ist()
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and (14 * 60 + 30) <= minute < 15 * 60


def _hero_today_option(underlying, spot_price, option_type):
    resolved = angel_fetcher.resolve_option(
        underlying,
        spot_price,
        option_type,
    )
    if not isinstance(resolved, dict):
        return None

    expiry = _parse_expiry(
        resolved.get("expiry_date") or resolved.get("expiry")
    )
    if expiry != _now_ist().date():
        return None

    result = dict(resolved)
    result["same_day_expiry"] = True
    result["expiry_date"] = expiry.isoformat()
    return result


def apply_hero_zero_guard_patch():
    if getattr(routes, "_okai_hero_zero_guard_v2", False):
        return

    # hero_zero_start resolves these names from bot.routes globals at call time.
    routes.market_open = _hero_window_open
    routes.resolve_option = _hero_today_option
    routes._okai_hero_zero_guard_v2 = True

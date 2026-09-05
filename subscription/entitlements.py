from datetime import datetime, timedelta

LIVE_TRIAL_DAYS = 7
PAPER_TRIAL_DAYS = 30


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def entitlement_snapshot(user, now=None):
    """Return customer-facing access without changing legacy DB state.

    Existing trial_ends_at remains the LIVE trial boundary for compatibility.
    Paper access is derived from that boundary: registration ~= live_end - 7 days,
    therefore paper_end = live_end + 23 days. Paid/admin access unlocks both.
    """
    now = now or datetime.utcnow()
    is_admin = bool(user.get("is_admin"))
    status = str(user.get("subscription_status") or "").lower()

    if is_admin or status == "active":
        return {
            "live_allowed": True,
            "paper_allowed": True,
            "live_access": "unlimited" if is_admin else "paid",
            "paper_access": "unlimited" if is_admin else "paid",
            "live_trial_ends_at": None,
            "paper_trial_ends_at": None,
            "live_days_remaining": None,
            "paper_days_remaining": None,
        }

    live_end = _parse_dt(user.get("trial_ends_at"))
    if not live_end:
        return {
            "live_allowed": False,
            "paper_allowed": False,
            "live_access": "expired",
            "paper_access": "expired",
            "live_trial_ends_at": None,
            "paper_trial_ends_at": None,
            "live_days_remaining": 0,
            "paper_days_remaining": 0,
        }

    paper_end = live_end + timedelta(days=PAPER_TRIAL_DAYS - LIVE_TRIAL_DAYS)
    live_allowed = now < live_end
    paper_allowed = now < paper_end

    def days_remaining(end):
        if now >= end:
            return 0
        seconds = (end - now).total_seconds()
        return max(1, int((seconds + 86399) // 86400))

    return {
        "live_allowed": live_allowed,
        "paper_allowed": paper_allowed,
        "live_access": "trial" if live_allowed else "expired",
        "paper_access": "trial" if paper_allowed else "expired",
        "live_trial_ends_at": live_end.isoformat(),
        "paper_trial_ends_at": paper_end.isoformat(),
        "live_days_remaining": days_remaining(live_end),
        "paper_days_remaining": days_remaining(paper_end),
    }

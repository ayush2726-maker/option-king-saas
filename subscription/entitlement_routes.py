from fastapi import APIRouter, Header

from auth.routes import get_current_user
from subscription.entitlements import entitlement_snapshot, LIVE_TRIAL_DAYS, PAPER_TRIAL_DAYS

router = APIRouter(prefix="/subscription", tags=["Subscription"])


@router.get("/entitlements")
def get_entitlements(authorization: str = Header(None)):
    user = get_current_user(authorization)
    access = entitlement_snapshot(user)
    return {
        "success": True,
        "trial_policy": {
            "live_days": LIVE_TRIAL_DAYS,
            "paper_days": PAPER_TRIAL_DAYS,
            "paid_plan_days": 30,
            "paid_plan_amount": 5000,
        },
        **access,
    }

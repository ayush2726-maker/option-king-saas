import io
import os
from urllib.parse import urlencode

import qrcode
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response

from auth.routes import get_current_user

router = APIRouter(prefix="/subscription", tags=["Subscription"])

AMOUNT_RUPEES = 5000
DURATION_DAYS = 30


def _upi_id():
    return str(os.getenv("MANUAL_UPI_ID", "")).strip()


def _upi_name():
    return str(os.getenv("MANUAL_UPI_NAME", "Option King AI")).strip() or "Option King AI"


def _upi_uri():
    upi_id = _upi_id()
    if not upi_id:
        return ""
    query = urlencode(
        {
            "pa": upi_id,
            "pn": _upi_name(),
            "am": f"{AMOUNT_RUPEES:.2f}",
            "cu": "INR",
            "tn": "Option King AI 30 day subscription",
        }
    )
    return f"upi://pay?{query}"


@router.get("/manual-payment")
def manual_payment_details(authorization: str = Header(None)):
    user = get_current_user(authorization)
    upi_id = _upi_id()
    configured = bool(upi_id)
    base_url = str(
        os.getenv(
            "PUBLIC_API_BASE_URL",
            "https://option-king-saas-production.up.railway.app",
        )
    ).rstrip("/")

    return {
        "success": True,
        "configured": configured,
        "mode": "manual",
        "automatic_activation": False,
        "amount": AMOUNT_RUPEES,
        "display_amount": "₹5,000",
        "duration_days": DURATION_DAYS,
        "upi_id": upi_id if configured else "",
        "upi_name": _upi_name(),
        "upi_uri": _upi_uri() if configured else "",
        "qr_url": f"{base_url}/subscription/manual-payment/qr" if configured else "",
        "user_reference": str(user["email"] or user["id"]),
        "instructions": (
            "Pay ₹5,000 using the shown UPI/QR. Payment does not activate the account automatically. "
            "After confirming payment, the admin will activate the account for 30 days."
        ),
    }


@router.get("/manual-payment/qr")
def manual_payment_qr():
    uri = _upi_uri()
    if not uri:
        raise HTTPException(status_code=503, detail="Manual UPI ID is not configured")

    image = qrcode.make(uri)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(
        content=output.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )

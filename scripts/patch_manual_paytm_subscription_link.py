from pathlib import Path

path = Path("subscription/razorpay_routes.py")
text = path.read_text()

needle = '''def _manual_upi_id():\n    return str(os.getenv("MANUAL_UPI_ID", "")).strip()\n\n\n'''
insert = '''def _manual_upi_id():\n    return str(os.getenv("MANUAL_UPI_ID", "")).strip()\n\n\ndef _manual_payment_link():\n    return str(os.getenv("PAYTM_SUBSCRIPTION_LINK", "")).strip()\n\n\n'''
if "def _manual_payment_link():" not in text:
    if needle not in text:
        raise SystemExit("manual UPI helper anchor not found")
    text = text.replace(needle, insert, 1)

old = '''@router.post("/create-link")\ndef create_link(body: dict = None, authorization: str = Header(None)):\n    user = get_current_user(authorization)\n    if not _manual_upi_id():\n        raise HTTPException(status_code=503, detail="Manual UPI ID is not configured yet")\n    reference = str(user["email"] or user["id"])\n    checkout_url = "https://option-king-saas-production.up.railway.app/subscription/razorpay/manual-page?ref=" + quote(reference, safe="")\n    return {\n        "success": True,\n        "manual_payment": True,\n        "automatic_activation": False,\n        "checkout_url": checkout_url,\n        "amount_rupees": 5000,\n        "upi_supported": True,\n        "qr_supported": True,\n        "message": "Manual UPI/QR payment page created. Admin activation required after payment.",\n    }\n'''
new = '''@router.post("/create-link")\ndef create_link(body: dict = None, authorization: str = Header(None)):\n    user = get_current_user(authorization)\n    paytm_link = _manual_payment_link()\n    if paytm_link:\n        return {\n            "success": True,\n            "manual_payment": True,\n            "automatic_activation": False,\n            "gateway": "paytm_payment_link",\n            "checkout_url": paytm_link,\n            "amount_rupees": 5000,\n            "upi_supported": True,\n            "qr_supported": True,\n            "user_reference": str(user["email"] or user["id"]),\n            "message": "Paytm payment link opened. Admin activation required after payment.",\n        }\n    if not _manual_upi_id():\n        raise HTTPException(status_code=503, detail="Manual payment link or UPI ID is not configured yet")\n    reference = str(user["email"] or user["id"])\n    checkout_url = "https://option-king-saas-production.up.railway.app/subscription/razorpay/manual-page?ref=" + quote(reference, safe="")\n    return {\n        "success": True,\n        "manual_payment": True,\n        "automatic_activation": False,\n        "gateway": "manual_upi",\n        "checkout_url": checkout_url,\n        "amount_rupees": 5000,\n        "upi_supported": True,\n        "qr_supported": True,\n        "message": "Manual UPI/QR payment page created. Admin activation required after payment.",\n    }\n'''
if old not in text:
    if '"gateway": "paytm_payment_link"' not in text:
        raise SystemExit("create-link block not found")
else:
    text = text.replace(old, new, 1)

path.write_text(text)
print("manual Paytm subscription link patch applied")

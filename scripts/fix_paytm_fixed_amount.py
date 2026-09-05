from pathlib import Path

p = Path('subscription/paytm_routes.py')
s = p.read_text()

old = '''        "maxPaymentsAllowed": "1",\n        "linkNotes": merchant_request_id,\n        "redirectionUrlSuccess": _url_with_order(_return_base_url(), merchant_request_id),\n'''
new = '''        "maxPaymentsAllowed": "1",\n        "singleTransactionOnly": True,\n        "linkOrderId": merchant_request_id[:50],\n        "linkNotes": merchant_request_id,\n        "redirectionUrlSuccess": _url_with_order(_return_base_url(), merchant_request_id),\n'''
if old not in s:
    raise SystemExit('request_body anchor not found')
s = s.replace(old, new, 1)

old2 = '''    short_url = str(response_body.get("shortUrl") or "").strip()\n    link_id = str(response_body.get("linkId") or "").strip()\n    if not short_url or not link_id:\n        raise HTTPException(status_code=502, detail="Paytm payment link was not returned")\n'''
new2 = '''    short_url = str(response_body.get("shortUrl") or "").strip()\n    link_id = str(response_body.get("linkId") or "").strip()\n    returned_type = str(response_body.get("linkType") or "").strip().upper()\n    returned_amount = _decimal_amount(response_body.get("amount"))\n    expected_amount = _expected_amount()\n    if not short_url or not link_id:\n        raise HTTPException(status_code=502, detail="Paytm payment link was not returned")\n    # Never send a generic/editable-amount Paytm link to the customer.\n    # Paytm's FIXED link must echo FIXED and the exact plan amount.\n    if returned_type != "FIXED" or returned_amount != expected_amount:\n        raise HTTPException(\n            status_code=502,\n            detail=(\n                "Paytm did not create a fixed ₹5,000 payment link. "\n                "Please retry; no editable-amount link will be opened."\n            ),\n        )\n'''
if old2 not in s:
    raise SystemExit('response validation anchor not found')
s = s.replace(old2, new2, 1)

p.write_text(s)
print('patched subscription/paytm_routes.py')

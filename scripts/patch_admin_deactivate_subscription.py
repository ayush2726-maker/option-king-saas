from pathlib import Path

path = Path('admin/routes.py')
text = path.read_text(encoding='utf-8')

needle = '''@router.post("/users/{user_id}/extend-trial")\ndef extend_trial(user_id: int, body: dict, authorization: str = Header(None)):\n'''
if needle not in text:
    raise SystemExit('extend-trial anchor not found')

block = '''@router.post("/users/{user_id}/deactivate-subscription")\ndef deactivate_subscription(user_id: int, authorization: str = Header(None)):\n    admin_user = require_admin(authorization)\n    from datetime import datetime\n    from subscription.routes import _ensure_subscription_schema\n\n    _ensure_subscription_schema()\n    conn = get_db()\n    try:\n        target = conn.execute(\n            "SELECT id, name, email, is_admin FROM users WHERE id=? LIMIT 1",\n            (user_id,),\n        ).fetchone()\n        if not target:\n            raise HTTPException(status_code=404, detail="User not found")\n        if int(target["id"]) == int(admin_user["id"]):\n            raise HTTPException(status_code=400, detail="You cannot deactivate your own admin subscription")\n        if bool(target["is_admin"]):\n            raise HTTPException(status_code=400, detail="Remove admin access before deactivating subscription")\n\n        stamp = datetime.utcnow().isoformat()\n        columns = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)").fetchall()}\n        if {"gateway_state", "updated_at"}.issubset(columns):\n            conn.execute(\n                """\n                UPDATE subscriptions\n                SET status='expired', valid_till=?, gateway_state='ADMIN_DEACTIVATED', updated_at=?\n                WHERE user_id=? AND status='active'\n                """,\n                (stamp, stamp, user_id),\n            )\n        else:\n            conn.execute(\n                "UPDATE subscriptions SET status='expired', valid_till=? WHERE user_id=? AND status='active'",\n                (stamp, user_id),\n            )\n\n        # Keep login enabled so the customer can open Billing and renew.\n        conn.execute(\n            """\n            UPDATE users\n            SET is_active=1, subscription_status='expired', trial_ends_at=NULL\n            WHERE id=?\n            """,\n            (user_id,),\n        )\n        conn.commit()\n        return {\n            "success": True,\n            "message": f"{target['name'] or target['email']} subscription deactivated",\n            "user_id": user_id,\n            "subscription_status": "expired",\n            "login_enabled": True,\n        }\n    finally:\n        conn.close()\n\n\n'''

if 'def deactivate_subscription(user_id: int' not in text:
    text = text.replace(needle, block + needle, 1)

path.write_text(text, encoding='utf-8')
print('Admin deactivate-subscription endpoint added')

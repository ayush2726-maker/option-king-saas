from pathlib import Path


def replace_once(text, old, new, label):
    if old not in text:
        raise SystemExit(f"missing pattern for {label}")
    return text.replace(old, new, 1)

# 1) Monthly plan -> Rs 5,000 / 30 days.
path = Path("subscription/routes.py")
text = path.read_text(encoding="utf-8")
text = text.replace('PLAN_ID = "monthly_1999"', 'PLAN_ID = "monthly_5000"')
text = text.replace('"price": 199900,', '"price": 500000,')
text = text.replace('"amount_rupees": 1999,', '"amount_rupees": 5000,')
text = text.replace('"display_price": "₹1,999",', '"display_price": "₹5,000",')
text = text.replace('"udf11": "OKAI_MONTHLY_1999",', '"udf11": "OKAI_MONTHLY_5000",')
path.write_text(text, encoding="utf-8")

# 2) Admin dashboard Activate button grants 30 paid-plan days manually.
path = Path("admin/routes.py")
text = path.read_text(encoding="utf-8")
old = '''@router.post("/users/{user_id}/activate")
def activate_user(user_id: int, authorization: str = Header(None)):
    require_admin(authorization)

    conn = get_db()
    conn.execute("UPDATE users SET is_active=1 WHERE id=?", (user_id,))
    conn.commit()
    conn.close()

    return {"success": True, "message": f"User {user_id} activated"}
'''
new = '''@router.post("/users/{user_id}/activate")
def activate_user(user_id: int, authorization: str = Header(None)):
    admin_user = require_admin(authorization)
    from datetime import datetime, timedelta
    from subscription.routes import _ensure_subscription_schema

    _ensure_subscription_schema()
    conn = get_db()
    try:
        target = conn.execute(
            "SELECT id, name, email FROM users WHERE id=? LIMIT 1", (user_id,)
        ).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")

        now = datetime.utcnow()
        base_date = now
        current = conn.execute(
            """
            SELECT valid_till FROM subscriptions
            WHERE user_id=? AND status='active' AND valid_till IS NOT NULL
            ORDER BY datetime(valid_till) DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if current and current["valid_till"]:
            try:
                current_till = datetime.fromisoformat(current["valid_till"])
                if current_till > now:
                    base_date = current_till
            except Exception:
                pass

        valid_till = base_date + timedelta(days=30)
        stamp = now.isoformat()
        conn.execute(
            """
            INSERT INTO subscriptions(
                user_id, plan, amount, status, payment_gateway,
                gateway_state, valid_from, valid_till, activated_at, updated_at
            ) VALUES (?, 'admin_manual_30d', 0, 'active', 'admin',
                      'ADMIN_GRANTED', ?, ?, ?, ?)
            """,
            (user_id, stamp, valid_till.isoformat(), stamp, stamp),
        )
        conn.execute(
            """
            UPDATE users
            SET is_active=1, subscription_status='active', trial_ends_at=NULL
            WHERE id=?
            """,
            (user_id,),
        )
        conn.commit()
        return {
            "success": True,
            "message": f"{target['name'] or target['email']} activated for 30 days",
            "user_id": user_id,
            "valid_till": valid_till.isoformat(),
            "granted_by_admin_id": int(admin_user["id"]),
            "manual_activation": True,
        }
    finally:
        conn.close()
'''
text = replace_once(text, old, new, "admin activation route")
path.write_text(text, encoding="utf-8")

# 3) Make the admin UI explicit and confirm before granting 30 days.
path = Path("admin/panel.html")
text = path.read_text(encoding="utf-8")
text = text.replace("onclick=\"userAction(${u.id},'activate')\">Activate</button>", "onclick=\"userAction(${u.id},'activate')\">Activate 30 Days</button>")
old = '''async function userAction(id, action) {
  const data = await api('POST', `/admin/users/${id}/${action}`);
  if (data.success) { loadUsers(); }
}
'''
new = '''async function userAction(id, action) {
  const user = loadedUsers.find(item => Number(item.id) === Number(id));
  const label = user ? `${user.name || 'User'} (${user.email || `#${id}`})` : `User #${id}`;
  if (action === 'activate') {
    if (!confirm(`Activate ${label} for 30 days?\\n\\nThis is a manual admin activation. No payment will be recorded as revenue.`)) return;
  }
  const data = await api('POST', `/admin/users/${id}/${action}`);
  if (data.success) {
    if (action === 'activate') alert(data.message || `${label} activated for 30 days`);
    loadUsers();
    loadDashboard();
  } else {
    alert(data?.detail || data?.message || 'Action failed');
  }
}
'''
text = replace_once(text, old, new, "admin userAction")
path.write_text(text, encoding="utf-8")

print("subscription/admin activation patch applied")

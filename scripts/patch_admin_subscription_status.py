from pathlib import Path


def replace_once(path, old, new, label):
    text = Path(path).read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{label} marker not found in {path}")
    Path(path).write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "main.py",
    '''        if not existing:
            trial_ends = (
                datetime.utcnow() + timedelta(days=36500)
            ).isoformat()
            conn.execute(
                """
                INSERT INTO users (
                    name, email, password_hash, is_admin,
                    subscription_status, trial_ends_at
                ) VALUES (?, ?, ?, 1, 'active', ?)
                """,
                (
                    admin_name,
                    admin_email,
                    hash_password(admin_password),
                    trial_ends,
                ),
            )
            conn.commit()
            print(f"Admin created: {admin_email}")
        conn.close()
''',
    '''        if existing:
            conn.execute(
                """
                UPDATE users
                SET is_admin=1,
                    subscription_status='active',
                    trial_ends_at=NULL
                WHERE id=?
                """,
                (existing["id"],),
            )
            conn.commit()
            print(f"Admin status refreshed: {admin_email}")
        else:
            conn.execute(
                """
                INSERT INTO users (
                    name, email, password_hash, is_admin,
                    subscription_status, trial_ends_at
                ) VALUES (?, ?, ?, 1, 'active', NULL)
                """,
                (
                    admin_name,
                    admin_email,
                    hash_password(admin_password),
                ),
            )
            conn.commit()
            print(f"Admin created: {admin_email}")
        conn.close()
''',
    "admin bootstrap",
)

replace_once(
    "auth/routes.py",
    '''    # Check subscription status
    status = user["subscription_status"]
    trial_ends = user["trial_ends_at"]
    warning = None

    if status == "trial" and trial_ends:
        trial_end_dt = datetime.fromisoformat(trial_ends)
        days_left = (trial_end_dt - datetime.utcnow()).days
        if days_left <= 0:
            # Update to expired
            conn = get_db()
            conn.execute(
                "UPDATE users SET subscription_status='expired' WHERE id=?", (user["id"],)
            )
            conn.commit()
            conn.close()
            status = "expired"
            warning = "Your trial has expired. Please subscribe to continue."
        elif days_left <= 2:
            warning = f"Trial expires in {days_left} day(s). Subscribe now!"
''',
    '''    # Check subscription status. Admin access is permanent and must never
    # be downgraded by trial/subscription calculations.
    status = "active" if bool(user["is_admin"]) else user["subscription_status"]
    trial_ends = None if bool(user["is_admin"]) else user["trial_ends_at"]
    warning = None

    if bool(user["is_admin"]):
        conn = get_db()
        conn.execute(
            """
            UPDATE users
            SET subscription_status='active', trial_ends_at=NULL
            WHERE id=?
            """,
            (user["id"],),
        )
        conn.commit()
        conn.close()
    elif status == "trial" and trial_ends:
        trial_end_dt = datetime.fromisoformat(trial_ends)
        days_left = (trial_end_dt - datetime.utcnow()).days
        if days_left <= 0:
            conn = get_db()
            conn.execute(
                "UPDATE users SET subscription_status='expired' WHERE id=?", (user["id"],)
            )
            conn.commit()
            conn.close()
            status = "expired"
            warning = "Your trial has expired. Please subscribe to continue."
        elif days_left <= 2:
            warning = f"Trial expires in {days_left} day(s). Subscribe now!"
''',
    "login subscription block",
)

replace_once(
    "auth/routes.py",
    '''            "subscription_status": user["subscription_status"],
            "trial_ends_at": user["trial_ends_at"],
            "is_admin": bool(user["is_admin"]),
''',
    '''            "subscription_status": "active" if bool(user["is_admin"]) else user["subscription_status"],
            "trial_ends_at": None if bool(user["is_admin"]) else user["trial_ends_at"],
            "is_admin": bool(user["is_admin"]),
            "unlimited_access": bool(user["is_admin"]),
''',
    "auth me admin status",
)

replace_once(
    "subscription/routes.py",
    '''    user = get_current_user(authorization)
    now = _utcnow()
    conn = get_db()
''',
    '''    user = get_current_user(authorization)
    now = _utcnow()

    if bool(user["is_admin"]):
        conn = get_db()
        try:
            conn.execute(
                """
                UPDATE users
                SET subscription_status='active', trial_ends_at=NULL
                WHERE id=?
                """,
                (user["id"],),
            )
            history = conn.execute(
                """
                SELECT plan, amount, status, payment_gateway, merchant_order_id,
                       gateway_state, valid_from, valid_till, created_at
                FROM subscriptions WHERE user_id=? ORDER BY created_at DESC
                """,
                (user["id"],),
            ).fetchall()
            conn.commit()
        finally:
            conn.close()

        return {
            "success": True,
            "subscription_status": "active",
            "days_remaining": None,
            "unlimited": True,
            "is_admin": True,
            "active_subscription": {
                "plan": "admin_unlimited",
                "status": "active",
                "valid_from": None,
                "valid_till": None,
            },
            "history": [dict(row) for row in history],
            "plan": PLAN,
        }

    conn = get_db()
''',
    "admin subscription early return",
)

print("Admin subscription status patch applied")

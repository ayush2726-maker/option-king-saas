from pathlib import Path

path = Path('main.py')
text = path.read_text(encoding='utf-8')

anchor = '''    morning_cleanup = delete_admin_morning_paper_trades_20260804()\n'''
block = '''    # Admin-controlled one-time/user access overrides.\n    demote_emails = [e.strip().lower() for e in os.getenv("ADMIN_DEMOTE_EMAILS", "").split(",") if e.strip()]\n    deactivate_emails = [e.strip().lower() for e in os.getenv("ADMIN_DEACTIVATE_EMAILS", "").split(",") if e.strip()]\n    if demote_emails or deactivate_emails:\n        from datetime import datetime\n        from subscription.routes import _ensure_subscription_schema\n        _ensure_subscription_schema()\n        conn = open_database()\n        try:\n            for email in demote_emails:\n                if admin_email and email == str(admin_email).strip().lower():\n                    continue\n                conn.execute("UPDATE users SET is_admin=0 WHERE lower(email)=?", (email,))\n            stamp = datetime.utcnow().isoformat()\n            for email in deactivate_emails:\n                if admin_email and email == str(admin_email).strip().lower():\n                    continue\n                row = conn.execute("SELECT id FROM users WHERE lower(email)=? LIMIT 1", (email,)).fetchone()\n                if not row:\n                    continue\n                uid = int(row["id"])\n                cols = {r[1] for r in conn.execute("PRAGMA table_info(subscriptions)").fetchall()}\n                if {"gateway_state", "updated_at"}.issubset(cols):\n                    conn.execute(\n                        "UPDATE subscriptions SET status='expired', valid_till=?, gateway_state='ADMIN_DEACTIVATED', updated_at=? WHERE user_id=? AND status='active'",\n                        (stamp, stamp, uid),\n                    )\n                else:\n                    conn.execute("UPDATE subscriptions SET status='expired', valid_till=? WHERE user_id=? AND status='active'", (stamp, uid))\n                conn.execute("UPDATE users SET is_admin=0, is_active=1, subscription_status='expired', trial_ends_at=NULL WHERE id=?", (uid,))\n            conn.commit()\n            print(f"Admin access overrides | demote={len(demote_emails)} deactivate={len(deactivate_emails)}")\n        finally:\n            conn.close()\n\n'''
if 'ADMIN_DEACTIVATE_EMAILS' not in text:
    if anchor not in text:
        raise SystemExit('startup cleanup anchor not found')
    text = text.replace(anchor, block + anchor, 1)

path.write_text(text, encoding='utf-8')
print('Admin env override startup logic added')

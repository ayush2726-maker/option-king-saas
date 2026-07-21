from pathlib import Path

path = Path("local_gateway/service.py")
text = path.read_text(encoding="utf-8")

old = '''        if not armed:
            conn.execute(
                """
                UPDATE local_order_commands
                SET status='cancelled', error='Gateway disarmed', updated_at=?
                WHERE user_id=? AND action='PLACE_ENTRY'
                  AND status IN ('pending','leased')
                """,
                (_iso(), int(user_id)),
            )
        conn.commit()
'''

new = '''        if not armed:
            pending_trade_rows = conn.execute(
                """
                SELECT DISTINCT trade_id
                FROM local_order_commands
                WHERE user_id=? AND action='PLACE_ENTRY'
                  AND status IN ('pending','leased')
                  AND trade_id IS NOT NULL
                """,
                (int(user_id),),
            ).fetchall()
            conn.execute(
                """
                UPDATE local_order_commands
                SET status='cancelled', error='Gateway disarmed',
                    lease_token_hash=NULL, lease_expires_at=NULL,
                    updated_at=?
                WHERE user_id=? AND action='PLACE_ENTRY'
                  AND status IN ('pending','leased')
                """,
                (_iso(), int(user_id)),
            )
            trade_ids = [int(row["trade_id"]) for row in pending_trade_rows]
            if trade_ids:
                placeholders = ",".join("?" for _ in trade_ids)
                conn.execute(
                    f"""
                    UPDATE trades
                    SET status='cancelled', exit_reason='Gateway disarmed before entry'
                    WHERE user_id=? AND status='pending'
                      AND id IN ({placeholders})
                    """,
                    (int(user_id), *trade_ids),
                )
        conn.commit()
'''

if old not in text:
    raise RuntimeError("Patched disarm block not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Disarm now cancels pending trade rows atomically")

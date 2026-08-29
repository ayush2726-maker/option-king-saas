def promote_user_to_admin(conn, user_id: int) -> dict:
    """Promote exactly one user without changing any existing administrator."""
    target = conn.execute(
        "SELECT id, name, email, is_admin FROM users WHERE id=? LIMIT 1",
        (int(user_id),),
    ).fetchone()
    if not target:
        raise LookupError("User not found")

    already_admin = bool(target["is_admin"])
    if not already_admin:
        conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (int(user_id),))
        conn.commit()

    return {
        "id": int(target["id"]),
        "name": str(target["name"] or ""),
        "email": str(target["email"] or ""),
        "is_admin": True,
        "already_admin": already_admin,
    }

import sqlite3

from admin.user_roles import promote_user_to_admin


def _database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT, email TEXT, is_admin INTEGER)"
    )
    conn.executemany(
        "INSERT INTO users(id, name, email, is_admin) VALUES (?, ?, ?, ?)",
        [
            (1, "Ayush", "ayush2726@gmail.com", 1),
            (4, "Rakesh Vijayvargiya", "rakesh53643@gmail.com", 0),
        ],
    )
    conn.commit()
    return conn


def test_promoting_rakesh_keeps_ayush_admin_access():
    conn = _database()
    result = promote_user_to_admin(conn, 4)
    rows = conn.execute("SELECT id, is_admin FROM users ORDER BY id").fetchall()

    assert result["email"] == "rakesh53643@gmail.com"
    assert result["already_admin"] is False
    assert [(row["id"], row["is_admin"]) for row in rows] == [(1, 1), (4, 1)]


def test_promotion_is_idempotent():
    conn = _database()
    promote_user_to_admin(conn, 4)
    result = promote_user_to_admin(conn, 4)

    assert result["already_admin"] is True
    assert conn.execute("SELECT is_admin FROM users WHERE id=1").fetchone()[0] == 1


if __name__ == "__main__":
    test_promoting_rakesh_keeps_ayush_admin_access()
    test_promotion_is_idempotent()
    print("Admin promotion preserves existing access")

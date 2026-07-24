"""Selected-broker source of truth for every user workflow.

Exactly one saved broker may be active for a user. Paper, live, chart/data and
all backtest routes read this same row, preventing a stale token from another
broker being used after the user changes broker in the app.
"""

from __future__ import annotations

from database import get_db


UNIQUE_SELECTED_INDEX = "uq_broker_credentials_one_active_per_user"


def get_selected_broker(conn, user_id: int):
    """Return the one active broker row for a user, or None."""
    return conn.execute(
        """SELECT * FROM broker_credentials
           WHERE user_id=? AND is_active=1
           ORDER BY last_connected DESC, id DESC
           LIMIT 1""",
        (int(user_id),),
    ).fetchone()


def activate_selected_broker(conn, user_id: int, broker_name: str) -> bool:
    """Atomically make broker_name the user's only active broker."""
    user_id = int(user_id)
    selected = str(broker_name or "").lower().strip()
    conn.execute(
        "UPDATE broker_credentials SET is_active=0 WHERE user_id=?",
        (user_id,),
    )
    result = conn.execute(
        """UPDATE broker_credentials
           SET is_active=1
           WHERE user_id=? AND broker_name=?""",
        (user_id, selected),
    )
    return bool(result.rowcount)


def normalize_all_selected_brokers() -> int:
    """Repair legacy duplicates and enforce one selected broker in SQLite.

    The most recently connected active broker wins. Users with no active broker
    remain unselected; an old disconnected credential is never silently revived.
    """
    conn = get_db()
    repaired = 0
    try:
        users = conn.execute(
            """SELECT user_id, COUNT(*) AS active_count
               FROM broker_credentials
               WHERE is_active=1
               GROUP BY user_id
               HAVING COUNT(*) > 1"""
        ).fetchall()

        for row in users:
            user_id = int(row["user_id"])
            winner = conn.execute(
                """SELECT id FROM broker_credentials
                   WHERE user_id=? AND is_active=1
                   ORDER BY last_connected DESC, id DESC
                   LIMIT 1""",
                (user_id,),
            ).fetchone()
            if not winner:
                continue
            conn.execute(
                """UPDATE broker_credentials
                   SET is_active=CASE WHEN id=? THEN 1 ELSE 0 END
                   WHERE user_id=?""",
                (int(winner["id"]), user_id),
            )
            repaired += 1

        # Database-level guard: even concurrent requests cannot leave two active
        # broker rows for the same user after this migration has run.
        conn.execute(
            f"""CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_SELECTED_INDEX}
                ON broker_credentials(user_id)
                WHERE is_active=1"""
        )
        conn.commit()
        return repaired
    finally:
        conn.close()

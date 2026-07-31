"""Register a secure admin endpoint for cleanup archive totals and user-wise details."""

from __future__ import annotations

from collections import defaultdict

from fastapi import Header

from database import get_db


_INSTALLED = False


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def _archive_rows(conn):
    rows = []

    if _table_exists(conn, "invalid_far_expiry_trades_archive"):
        for row in conn.execute(
            """
            SELECT
                'far_expiry' AS source,
                trade_id,
                user_id,
                underlying,
                original_pnl,
                cleanup_reason AS reason,
                archived_at
            FROM invalid_far_expiry_trades_archive
            """
        ).fetchall():
            rows.append(dict(row))

    if _table_exists(conn, "invalid_blocked_trades_archive"):
        for row in conn.execute(
            """
            SELECT
                'blocked_entry' AS source,
                trade_id,
                user_id,
                underlying,
                original_pnl,
                audit_reason AS reason,
                archived_at
            FROM invalid_blocked_trades_archive
            """
        ).fetchall():
            rows.append(dict(row))

    return rows


def _build_report(conn) -> dict:
    rows = _archive_rows(conn)
    users = {
        int(row["id"]): dict(row)
        for row in conn.execute(
            "SELECT id, name, email FROM users"
        ).fetchall()
    }

    grouped = defaultdict(
        lambda: {
            "trade_ids": set(),
            "removed_pnl": 0.0,
            "far_expiry": 0,
            "blocked_entry": 0,
            "by_reason": defaultdict(int),
            "by_underlying": defaultdict(int),
        }
    )

    all_keys = set()
    total_pnl = 0.0
    by_source = defaultdict(int)
    by_reason = defaultdict(int)
    by_underlying = defaultdict(int)

    for row in rows:
        source = str(row.get("source") or "unknown")
        trade_id = int(row.get("trade_id") or 0)
        user_id = int(row.get("user_id") or 0)
        key = (source, trade_id)
        if trade_id <= 0 or key in all_keys:
            continue
        all_keys.add(key)

        pnl = float(row.get("original_pnl") or 0.0)
        reason = str(row.get("reason") or "UNKNOWN")
        underlying = str(row.get("underlying") or "UNKNOWN")

        bucket = grouped[user_id]
        bucket["trade_ids"].add(key)
        bucket["removed_pnl"] += pnl
        bucket[source] += 1
        bucket["by_reason"][reason] += 1
        bucket["by_underlying"][underlying] += 1

        total_pnl += pnl
        by_source[source] += 1
        by_reason[reason] += 1
        by_underlying[underlying] += 1

    user_rows = []
    for user_id, bucket in grouped.items():
        user = users.get(user_id, {})
        user_rows.append(
            {
                "user_id": user_id,
                "name": user.get("name") or f"User {user_id}",
                "email": user.get("email") or "--",
                "removed_trades": len(bucket["trade_ids"]),
                "far_expiry_trades": int(bucket["far_expiry"]),
                "blocked_entry_trades": int(bucket["blocked_entry"]),
                "removed_recorded_pnl": round(bucket["removed_pnl"], 2),
                "by_reason": dict(sorted(bucket["by_reason"].items())),
                "by_underlying": dict(sorted(bucket["by_underlying"].items())),
            }
        )

    user_rows.sort(
        key=lambda item: (-int(item["removed_trades"]), str(item["name"]).lower())
    )

    return {
        "success": True,
        "total_removed_trades": len(all_keys),
        "affected_users": len(user_rows),
        "removed_recorded_pnl": round(total_pnl, 2),
        "by_source": dict(sorted(by_source.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "by_underlying": dict(sorted(by_underlying.items())),
        "users": user_rows,
        "live_trades_removed": 0,
        "report_basis": "ARCHIVED_PAPER_CLEANUP_ROWS",
    }


def apply_admin_cleanup_report_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from admin import routes as admin_routes

    @admin_routes.router.get("/cleanup-report")
    def cleanup_report(authorization: str = Header(None)):
        admin_routes.require_admin(authorization)
        conn = get_db()
        try:
            return _build_report(conn)
        finally:
            conn.close()

    _INSTALLED = True

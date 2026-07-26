"""Keep PAPER equity continuous across days and preserve broker-funded LIVE sizing.

PAPER capital is a seed plus cumulative closed net P&L after the latest explicit
P&L reset.  LIVE mode remains isolated from PAPER settings: the first live slot
reads the broker's available funds, and any second slot retains that same broker
capital base so the configured 50%/40% allocation is not recalculated from the
already-reduced cash balance.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import Header

from database import get_db
from bot import auto_portfolio_runtime as runtime
from paper import routes as paper_routes


RESET_KEY = "paper_capital_reset_at"
CARRY_KEY = "paper_capital_carry_forward"
_INSTALLED = False


def _f(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _has_column(conn, table: str, column: str) -> bool:
    try:
        return any(
            str(row["name"]) == column
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        )
    except Exception:
        return False


def _paper_summary(conn, user_id: int, settings: dict) -> dict:
    seed = max(
        1000.0,
        _f(settings.get("paper_capital", 100000), 100000),
    )
    reset_at = str(settings.get(RESET_KEY) or "").strip()
    pnl_expression = (
        "COALESCE(net_pnl, pnl, 0)"
        if _has_column(conn, "paper_trades", "net_pnl")
        else "COALESCE(pnl, 0)"
    )

    where = [
        "user_id=?",
        "status='CLOSED'",
    ]
    if _has_column(conn, "paper_trades", "trading_mode"):
        where.append("COALESCE(trading_mode, 'paper')='paper'")

    params = [int(user_id)]
    if reset_at:
        where.append("datetime(created_at) >= datetime(?)")
        params.append(reset_at)

    row = conn.execute(
        f"""
        SELECT COALESCE(SUM({pnl_expression}), 0) AS net_pnl,
               COUNT(*) AS closed_trades
        FROM paper_trades
        WHERE {' AND '.join(where)}
        """,
        tuple(params),
    ).fetchone()

    cumulative_net_pnl = round(_f(row["net_pnl"] if row else 0), 2)
    equity = max(1000.0, round(seed + cumulative_net_pnl, 2))
    return {
        "seed_capital": round(seed, 2),
        "cumulative_net_pnl": cumulative_net_pnl,
        "equity": equity,
        "closed_trades": int(row["closed_trades"] if row else 0),
        "reset_at": reset_at or None,
    }


def _continuous_paper_base(conn, user_id, settings):
    """Runtime lot sizing always starts from the continuously carried equity."""
    return _paper_summary(conn, int(user_id), settings)["equity"]


def _replace_route(router, path: str, method: str, endpoint) -> None:
    for route in getattr(router, "routes", []):
        if getattr(route, "path", None) != path:
            continue
        if method not in getattr(route, "methods", set()):
            continue
        route.endpoint = endpoint
        try:
            route.dependant.call = endpoint
        except Exception:
            pass


def _continuous_paper_account(authorization: str = Header(None)):
    user = paper_routes.get_current_user(authorization)
    conn = get_db()
    try:
        settings = paper_routes.load_settings(conn, int(user["id"]))
        summary = _paper_summary(conn, int(user["id"]), settings)
    finally:
        conn.close()

    return {
        "success": True,
        "account": {
            "trading_mode": settings.get("trading_mode", "paper"),
            "paper_capital": summary["seed_capital"],
            "opening_capital": summary["seed_capital"],
            "total_pnl": summary["cumulative_net_pnl"],
            "equity": summary["equity"],
            "current_capital": summary["equity"],
            "total_trades": summary["closed_trades"],
            "capital_carry_forward": True,
            "capital_source": "PREVIOUS_CLOSE_PLUS_NET_PNL",
            "paper_capital_reset_at": summary["reset_at"],
        },
    }


def _reset_continuous_paper_account(
    body: dict = None,
    authorization: str = Header(None),
):
    user = paper_routes.get_current_user(authorization)
    body = body or {}
    capital = paper_routes.clamp_cap(body.get("capital", 100000))
    now = datetime.utcnow().isoformat()

    conn = get_db()
    try:
        settings = paper_routes.load_settings(conn, int(user["id"]))
        try:
            mode_filter = (
                " AND COALESCE(trading_mode, 'paper')='paper'"
                if _has_column(conn, "paper_trades", "trading_mode")
                else ""
            )
            open_trade = conn.execute(
                f"""
                SELECT id FROM paper_trades
                WHERE user_id=? AND status='OPEN'{mode_filter}
                LIMIT 1
                """,
                (int(user["id"]),),
            ).fetchone()
        except Exception:
            open_trade = None

        if open_trade:
            return {
                "success": False,
                "message": "Open PAPER trade close hone ke baad P&L reset karein.",
                "paper_capital": capital,
            }

        settings["paper_capital"] = capital
        settings["trading_mode"] = "paper"
        settings[RESET_KEY] = now
        settings[CARRY_KEY] = True
        paper_routes.save_settings(conn, int(user["id"]), settings)

        try:
            conn.execute(
                """
                UPDATE bot_status
                SET total_pnl=0, total_trades=0,
                    last_signal='PAPER_RESET', updated_at=?
                WHERE user_id=?
                """,
                (now, int(user["id"])),
            )
            conn.commit()
        except Exception:
            pass
    finally:
        conn.close()

    try:
        paper_routes.notify_user(
            int(user["id"]),
            f"♻️ <b>Paper Account Reset</b>\nCapital: ₹{capital:,.0f}\n"
            "Aaj se naya carry-forward cycle start hua.",
        )
    except Exception:
        pass

    return {
        "success": True,
        "message": "Paper account reset; new carry-forward cycle started",
        "paper_capital": capital,
        "capital_carry_forward": True,
        "paper_capital_reset_at": now,
    }


def apply_capital_continuity_patch() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    # PAPER: seed + all closed net P&L since the latest explicit reset.  This is
    # mathematically the previous day's closing capital carried into today.
    runtime._paper_base = _continuous_paper_base
    runtime._okai_paper_capital_carry_forward_v1 = True

    # LIVE remains broker-funded in auto_portfolio_runtime._open_common:
    # first slot reads live_cash(), while an open live row preserves that same
    # broker capital base for slot 2.  PAPER settings can never size LIVE orders.
    runtime._okai_live_capital_source = "BROKER_AVAILABLE_FUNDS"

    paper_routes.paper_account = _continuous_paper_account
    paper_routes.reset_paper_account = _reset_continuous_paper_account
    _replace_route(
        paper_routes.router,
        "/paper/account",
        "GET",
        _continuous_paper_account,
    )
    _replace_route(
        paper_routes.router,
        "/paper/reset",
        "POST",
        _reset_continuous_paper_account,
    )

    _INSTALLED = True

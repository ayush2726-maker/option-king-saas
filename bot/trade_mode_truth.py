"""Strict PAPER/LIVE ownership for mirrored trade rows.

`trades` is the broker-authoritative LIVE ledger.  `paper_trades` also contains
legacy LIVE mirrors, so a saved `trading_mode='live'` flag alone is not enough:
an old repair could set that flag on unrelated PAPER rows.  Broker ownership is
proved by an entry order id or by an exact broker fill mirror (same user,
contract, entry, quantity and near-identical entry time).
"""

from __future__ import annotations


VERSION = "OKAI-TRADE-MODE-TRUTH-V1-STRICT-BROKER-PROOF"


def _columns(conn, table: str) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def broker_proof_sql(conn, paper_alias: str = "paper_trades") -> str:
    """Return a correlated SQLite predicate proving a paper_trades row is LIVE."""
    paper_columns = _columns(conn, "paper_trades")
    trade_columns = _columns(conn, "trades")
    parts: list[str] = []

    if "entry_order_id" in paper_columns:
        parts.append(
            f"COALESCE(NULLIF(TRIM({paper_alias}.entry_order_id),''),'')<>''"
        )

    paper_time = next(
        (name for name in ("entry_time", "created_at") if name in paper_columns),
        None,
    )
    live_time = next(
        (name for name in ("entry_time", "created_at") if name in trade_columns),
        None,
    )
    exact_columns = (
        {"user_id", "symbol", "entry_price", "qty"}.issubset(paper_columns)
        and {"user_id", "symbol", "entry_price", "quantity"}.issubset(trade_columns)
        and paper_time
        and live_time
    )
    if exact_columns:
        parts.append(
            "EXISTS (SELECT 1 FROM trades AS broker_trade WHERE "
            f"broker_trade.user_id={paper_alias}.user_id "
            f"AND UPPER(COALESCE(broker_trade.symbol,''))=UPPER(COALESCE({paper_alias}.symbol,'')) "
            f"AND ABS(COALESCE(broker_trade.entry_price,0)-COALESCE({paper_alias}.entry_price,0))<=0.05 "
            f"AND COALESCE(broker_trade.quantity,0)=COALESCE({paper_alias}.qty,0) "
            f"AND {paper_alias}.{paper_time} IS NOT NULL "
            f"AND broker_trade.{live_time} IS NOT NULL "
            f"AND ABS((julianday(broker_trade.{live_time})-julianday({paper_alias}.{paper_time}))*1440.0)<=10.0)"
        )

    return "(" + " OR ".join(parts) + ")" if parts else "0"


def paper_truth_sql(conn, paper_alias: str = "paper_trades") -> str:
    return f"NOT {broker_proof_sql(conn, paper_alias)}"


def reconcile_trade_modes(conn, user_id: int | None = None) -> int:
    """Repair previously mislabelled rows without touching prices or P&L."""
    if "trading_mode" not in _columns(conn, "paper_trades"):
        return 0
    proof = broker_proof_sql(conn, "paper_trades")
    params: list[int] = []
    scope = ""
    if user_id is not None:
        scope = " AND user_id=?"
        params.append(int(user_id))
    cursor = conn.execute(
        f"""
        UPDATE paper_trades
        SET trading_mode=CASE WHEN {proof} THEN 'live' ELSE 'paper' END
        WHERE LOWER(COALESCE(trading_mode,''))<>
              CASE WHEN {proof} THEN 'live' ELSE 'paper' END
              {scope}
        """,
        tuple(params),
    )
    conn.commit()
    return max(0, int(cursor.rowcount or 0))


__all__ = [
    "VERSION",
    "broker_proof_sql",
    "paper_truth_sql",
    "reconcile_trade_modes",
]

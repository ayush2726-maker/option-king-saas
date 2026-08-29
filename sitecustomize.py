"""Runtime compatibility for Angel One SmartAPI symbol search.

Option King AI pins smartapi-python 1.3.4. Some SmartConnect builds do not
expose ``searchScrip`` even though the sector-rotation resolver needs the same
symbol-to-token lookup. Python imports ``sitecustomize`` automatically during
normal interpreter startup, so this module adds the missing method without
changing any order, risk, strategy, or broker-session logic.
"""

from __future__ import annotations

import threading
import time
from typing import Any

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)
CACHE_TTL_SECONDS = 12 * 60 * 60

_master_lock = threading.Lock()
_master_rows: list[dict[str, str]] = []
_master_loaded_at = 0.0


def _normalize(value: Any) -> str:
    return str(value or "").strip().upper()


def _download_equity_master() -> list[dict[str, str]]:
    import requests

    response = requests.get(
        SCRIP_MASTER_URL,
        headers={"User-Agent": "OptionKingAI/1.0"},
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()

    rows: list[dict[str, str]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue

        exchange = _normalize(item.get("exch_seg"))
        symbol = _normalize(item.get("symbol"))
        token = str(item.get("token") or "").strip()
        name = _normalize(item.get("name"))

        if exchange not in {"NSE", "BSE"} or not symbol or not token:
            continue

        # Keep cash-market equities and indices only. Derivatives are resolved
        # by the existing option-chain module and must not be mixed in here.
        instrument_type = _normalize(item.get("instrumenttype"))
        is_cash_equity = symbol.endswith("-EQ") or instrument_type in {
            "",
            "EQ",
            "AMXIDX",
            "INDEX",
        }
        if not is_cash_equity:
            continue

        rows.append(
            {
                "exchange": exchange,
                "tradingsymbol": symbol,
                "symboltoken": token,
                "name": name,
            }
        )

    if not rows:
        raise RuntimeError("Angel instrument master returned no cash-market rows")
    return rows


def _load_master() -> list[dict[str, str]]:
    global _master_rows, _master_loaded_at

    now = time.monotonic()
    with _master_lock:
        if _master_rows and now - _master_loaded_at < CACHE_TTL_SECONDS:
            return _master_rows

        stale = _master_rows
        try:
            fresh = _download_equity_master()
        except Exception:
            if stale:
                return stale
            raise

        _master_rows = fresh
        _master_loaded_at = now
        return _master_rows


def _search_master(exchange: Any, query: Any) -> list[dict[str, str]]:
    exchange_key = _normalize(exchange)
    search_key = _normalize(query)
    if not exchange_key or not search_key:
        return []

    candidates: list[tuple[int, str, dict[str, str]]] = []
    for row in _load_master():
        if row["exchange"] != exchange_key:
            continue

        symbol = row["tradingsymbol"]
        name = row.get("name", "")
        base_symbol = symbol[:-3] if symbol.endswith("-EQ") else symbol

        if base_symbol == search_key or symbol == search_key:
            rank = 0
        elif name == search_key:
            rank = 1
        elif base_symbol.startswith(search_key) or name.startswith(search_key):
            rank = 2
        elif search_key in base_symbol or search_key in name:
            rank = 3
        else:
            continue

        candidates.append((rank, symbol, row))

    candidates.sort(key=lambda item: (item[0], item[1]))
    return [
        {
            "exchange": row["exchange"],
            "tradingsymbol": row["tradingsymbol"],
            "symboltoken": row["symboltoken"],
        }
        for _, _, row in candidates[:50]
    ]


def _compat_search_scrip(self: Any, exchange: Any, searchscrip: Any) -> dict[str, Any]:
    try:
        data = _search_master(exchange, searchscrip)
        return {
            "status": bool(data),
            "message": "SUCCESS" if data else "No matching Angel instrument",
            "errorcode": "" if data else "NO_MATCH",
            "data": data,
        }
    except Exception as exc:
        return {
            "status": False,
            "message": f"Angel instrument lookup failed: {str(exc)[:180]}",
            "errorcode": "INSTRUMENT_MASTER_UNAVAILABLE",
            "data": [],
        }


def _install() -> None:
    try:
        from SmartApi import SmartConnect
    except Exception:
        return

    if not hasattr(SmartConnect, "searchScrip"):
        SmartConnect.searchScrip = _compat_search_scrip
        SmartConnect.__okai_search_scrip_compat__ = True


_install()


# One-time production recovery for the active PAPER account that was showing
# STOPPED / ₹0 / "Could not load status" even after pressing Start.  This is
# deliberately limited to PAPER mode: it cannot place live broker orders.
def _repair_rakesh_paper_runtime_once() -> None:
    # Let FastAPI startup finish creating/migrating the database first.
    time.sleep(12)

    for attempt in range(6):
        conn = None
        try:
            from database import get_db
            import bot.routes as routes

            # bot.routes has a broad Angel import fallback.  Older fallback
            # code did not define get_user_bot_state, which can turn /bot/signal
            # into HTTP 500.  Fill the runtime symbol safely if that happened.
            if not hasattr(routes, "get_user_bot_state"):
                try:
                    from bot import angel_fetcher as af
                    routes.get_user_bot_state = af.get_user_bot_state
                    routes.start_user_bot = af.start_user_bot
                    routes.stop_user_bot = af.stop_user_bot
                    routes.INDEX_TOKENS = af.INDEX_TOKENS
                    routes.INDEX_EXCHANGE = af.INDEX_EXCHANGE
                except Exception as import_exc:
                    def _safe_state(_user_id: int) -> dict[str, Any]:
                        return {
                            "running": False,
                            "status": "ENGINE_IMPORT_FAILED",
                            "signal": "WAITING",
                            "score": 0,
                            "error": str(import_exc)[:160],
                        }
                    routes.get_user_bot_state = _safe_state

            conn = get_db()
            routes.ensure_tables(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_repairs (
                    repair_key TEXT PRIMARY KEY,
                    applied_at TEXT DEFAULT (datetime('now'))
                )
                """
            )

            marker = conn.execute(
                "SELECT repair_key FROM runtime_repairs WHERE repair_key=?",
                ("rakesh_paper_runtime_20260826_v1",),
            ).fetchone()
            if marker:
                return

            user = conn.execute(
                """
                SELECT id, name FROM users
                WHERE lower(name) LIKE '%rakesh%'
                  AND lower(name) LIKE '%vijay%'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if not user:
                raise RuntimeError("Rakesh Vijayvargiya user not found")

            settings = routes.get_strategy_settings(conn, user["id"])
            mode = str(settings.get("trading_mode", "paper")).lower()
            if mode != "paper":
                raise RuntimeError(
                    f"Safety stop: Rakesh account mode is {mode}, not PAPER"
                )

            # Persist ON first so normal Railway/runtime recovery owns the engine.
            routes.save_bot_status(conn, user["id"], 1, "PAPER_MODE")
            conn.execute(
                "INSERT OR IGNORE INTO runtime_repairs (repair_key) VALUES (?)",
                ("rakesh_paper_runtime_20260826_v1",),
            )
            conn.commit()
            user_id = int(user["id"])
            conn.close()
            conn = None

            recovery = routes._start_saved_runtime_engine(user_id)
            print(
                "Rakesh PAPER runtime hotfix | "
                f"user={user_id} | started={recovery.get('started')} | "
                f"reason={recovery.get('reason')}"
            )
            return
        except Exception as exc:
            print(
                "Rakesh PAPER runtime hotfix retry | "
                f"attempt={attempt + 1} | error={str(exc)[:180]}"
            )
            time.sleep(5)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


threading.Thread(
    target=_repair_rakesh_paper_runtime_once,
    name="rakesh-paper-runtime-hotfix",
    daemon=True,
).start()


# Live-mode UI/data authority: local static-IP gateway publishes Angel funds;
# dashboard/history responses stay isolated by PAPER vs LIVE mode.
try:
    from bot.live_mode_ui_sync_patch import apply_live_mode_ui_sync_patch
    apply_live_mode_ui_sync_patch()
except Exception as exc:
    print(f"Live mode UI sync patch warning | {str(exc)[:180]}")

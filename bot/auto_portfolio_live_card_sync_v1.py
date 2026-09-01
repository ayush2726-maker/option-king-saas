"""Keep AUTO Portfolio active-position cards in sync with LIVE Angel truth.

The dedicated Active Live Trades endpoint already uses Angel gateway truth, but
AUTO Portfolio state is built from gateway status with a deliberately minimal
position shape. Enrich only the display state: Angel/trades supplies LTP and
quantity, while paper_trades supplies the strategy's authoritative trailing SL.
No entry, exit, sizing, signal or broker execution logic is changed.
"""
from __future__ import annotations

from database import get_db
from bot import auto_portfolio_runtime as runtime

VERSION = "AUTO_PORTFOLIO_LIVE_CARD_SYNC_V1_20260901"
_original_state_update = runtime._state_update


def _f(v, default=0.0):
    try:
        x = float(v)
        return x if x == x else float(default)
    except Exception:
        return float(default)


def _i(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return int(default)


def _rowv(row, key, default=None):
    try:
        v = row[key]
        return default if v is None else v
    except Exception:
        return default


def _display_truth(user_id, symbol):
    conn = get_db()
    try:
        live = conn.execute(
            "SELECT * FROM trades WHERE user_id=? AND symbol=? AND LOWER(status) IN ('open','pending','exit_pending') ORDER BY id DESC LIMIT 1",
            (int(user_id), str(symbol)),
        ).fetchone()
        trail = conn.execute(
            "SELECT * FROM paper_trades WHERE user_id=? AND symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1",
            (int(user_id), str(symbol)),
        ).fetchone()
        return live, trail
    except Exception:
        return None, None
    finally:
        conn.close()


def _patched_state_update(state, scans, selected, settings, rows):
    result = _original_state_update(state, scans, selected, settings, rows)
    if str(settings.get("trading_mode", "paper")).lower() != "live":
        return result

    user_id = state.get("user_id") or state.get("uid")
    if not user_id:
        # Gateway rows carry the user only indirectly. Infer from matching live symbol.
        try:
            conn = get_db()
            for pos in state.get("open_positions") or []:
                sym = str(pos.get("symbol") or "")
                if not sym:
                    continue
                hit = conn.execute(
                    "SELECT user_id FROM trades WHERE symbol=? AND LOWER(status) IN ('open','pending','exit_pending') ORDER BY id DESC LIMIT 1",
                    (sym,),
                ).fetchone()
                if hit:
                    user_id = _rowv(hit, "user_id")
                    break
        except Exception:
            pass
        finally:
            try: conn.close()
            except Exception: pass
    if not user_id:
        return result

    enriched = []
    for pos in state.get("open_positions") or []:
        item = dict(pos)
        symbol = str(item.get("symbol") or "")
        live, trail = _display_truth(user_id, symbol)
        if live is not None:
            qty = _i(_rowv(live, "quantity", item.get("qty", 0)), item.get("qty", 0))
            entry = _f(_rowv(live, "entry_price", 0), 0)
            # Gateway bridge persists the current broker quote in last_ltp when available.
            ltp = _f(_rowv(live, "last_ltp", 0), 0)
            if ltp <= 0:
                try:
                    import json
                    meta = json.loads(_rowv(live, "metadata_json", "{}") or "{}")
                    gp = meta.get("gateway_position") or {}
                    ltp = _f(gp.get("ltp"), 0)
                except Exception:
                    pass
            item["qty"] = qty
            item["quantity"] = qty
            item["entry_price"] = round(entry, 2) if entry > 0 else item.get("entry_price")
            if ltp > 0:
                item["ltp"] = round(ltp, 2)
                item["live_price"] = round(ltp, 2)
                item["current_price"] = round(ltp, 2)
                item["gross_pnl"] = round((ltp - entry) * qty, 2) if entry > 0 else None
        if trail is not None:
            sl = _f(_rowv(trail, "sl_price", 0), 0)
            if sl > 0:
                item["sl_price"] = round(sl, 2)
                item["sl"] = round(sl, 2)
            item["trail_stage"] = _rowv(trail, "trail_stage")
            item["peak_price"] = _rowv(trail, "peak_price")
            item["trail_updates"] = _rowv(trail, "trail_updates", 0)
        item["display_source"] = "ANGEL_LIVE_PLUS_AUTHORITATIVE_TRAIL"
        enriched.append(item)
    state["open_positions"] = enriched
    state["auto_portfolio_live_card_sync"] = VERSION
    return result


runtime._state_update = _patched_state_update
print(f"AUTO PORTFOLIO LIVE CARD SYNC INSTALLED | {VERSION}")

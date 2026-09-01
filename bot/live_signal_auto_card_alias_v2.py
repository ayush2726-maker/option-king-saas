"""Expose the exact field names consumed by the AUTO Portfolio card in LIVE mode.

The LIVE signal authority already carries Angel broker truth, but the AUTO card
uses legacy aliases (`ltp`, `sl`, `pnl`, `active_positions`). Keep all values
sourced from the same Angel LIVE payload and only add compatible aliases.
"""
from datetime import datetime, timezone

from fastapi.responses import JSONResponse

from auth.routes import get_current_user
from bot.live_mode_broker_truth_middleware import _settings_mode
from bot.live_signal_broker_truth_middleware import _payload

VERSION = "LIVE_SIGNAL_AUTO_CARD_ALIASES_V2_20260901"


def _num(value, default=0.0):
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _enrich_position(position):
    item = dict(position or {})

    ltp = _num(
        item.get("live_price")
        if item.get("live_price") is not None
        else item.get("current_price")
        if item.get("current_price") is not None
        else item.get("last_ltp"),
        0.0,
    )
    entry = _num(item.get("entry_price"), 0.0)
    qty = int(_num(item.get("qty") if item.get("qty") is not None else item.get("quantity"), 0))
    sl = _num(item.get("sl_price") if item.get("sl_price") is not None else item.get("sl"), 0.0)

    net = item.get("net_pnl")
    if net is None:
        net = item.get("unrealized_pnl")
    if net is None:
        net = item.get("pnl")
    if net is None and ltp > 0 and entry > 0 and qty > 0:
        net = (ltp - entry) * qty
    net = _num(net, 0.0)

    item["qty"] = qty
    item["quantity"] = qty
    item["entry"] = round(entry, 2) if entry > 0 else item.get("entry")
    item["entry_price"] = round(entry, 2) if entry > 0 else item.get("entry_price")

    if ltp > 0:
        item["ltp"] = round(ltp, 2)
        item["live_price"] = round(ltp, 2)
        item["current_price"] = round(ltp, 2)
        item["last_ltp"] = round(ltp, 2)
        item["last_price"] = round(ltp, 2)

    if sl > 0:
        item["sl"] = round(sl, 2)
        item["sl_price"] = round(sl, 2)
        item["live_sl"] = round(sl, 2)

    item["pnl"] = round(net, 2)
    item["net_pnl"] = round(net, 2)
    item["unrealized_pnl"] = round(net, 2)
    item["profit_loss"] = round(net, 2)
    item["live_pnl"] = round(net, 2)
    item["auto_card_source"] = "ANGEL_LIVE_SIGNAL_ALIAS_V2"
    return item


def _auto_payload(user_id):
    payload = dict(_payload(user_id) or {})
    positions = list(
        payload.get("open_positions")
        or payload.get("active_trades")
        or payload.get("active_positions")
        or []
    )
    positions = [_enrich_position(position) for position in positions]

    payload["open_positions"] = positions
    payload["active_positions"] = positions
    payload["active_trades"] = positions
    payload["portfolio_positions"] = positions
    payload["open_trade_count"] = len(positions)

    first = positions[0] if positions else None
    payload["active_trade"] = first
    if first:
        payload["trade_symbol"] = first.get("symbol")
        payload["trade_side"] = first.get("side")
        payload["trade_qty"] = first.get("qty")
        payload["entry_price"] = first.get("entry_price")
        payload["ltp"] = first.get("ltp")
        payload["live_price"] = first.get("live_price")
        payload["current_price"] = first.get("current_price")
        payload["sl"] = first.get("sl")
        payload["sl_price"] = first.get("sl_price")
        payload["trade_pnl"] = first.get("net_pnl")
        payload["unrealized_pnl"] = first.get("unrealized_pnl")

    payload["auto_portfolio_live_alias_version"] = VERSION
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


class LiveSignalAutoCardAliasMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope.get("type") != "http"
            or scope.get("method") != "GET"
            or (scope.get("path") or "") != "/bot/signal"
        ):
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        try:
            user = get_current_user(headers.get("authorization"))
        except Exception:
            await self.app(scope, receive, send)
            return

        if _settings_mode(user["id"]) != "live":
            await self.app(scope, receive, send)
            return

        try:
            response = JSONResponse(
                _auto_payload(user["id"]),
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "X-OKAI-Auto-Card-Authority": VERSION,
                    "X-OKAI-Live-Broker": "angelone",
                },
            )
        except Exception as exc:
            response = JSONResponse(
                {
                    "success": False,
                    "message": "AUTO Portfolio LIVE display unavailable: " + str(exc)[:180],
                    "version": VERSION,
                },
                status_code=500,
            )
        await response(scope, receive, send)


def install(app):
    app.add_middleware(LiveSignalAutoCardAliasMiddleware)
    print(f"LIVE SIGNAL AUTO CARD ALIASES INSTALLED | {VERSION}")

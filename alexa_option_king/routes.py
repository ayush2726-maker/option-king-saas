from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from ask_sdk_core.dispatch_components import AbstractExceptionHandler, AbstractRequestHandler
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_core.skill_builder import SkillBuilder
from ask_sdk_core.utils import is_intent_name, is_request_type
from ask_sdk_model import Response

from database import get_db

router = APIRouter(tags=["Alexa Option King"])


def _money(value: Any) -> str:
    try:
        amount = float(value or 0)
    except Exception:
        amount = 0.0
    sign = "minus " if amount < 0 else ""
    return f"{sign}{abs(amount):.2f}".rstrip("0").rstrip(".")


def _owner_user_id() -> int:
    email = (os.getenv("ALEXA_OWNER_EMAIL") or os.getenv("ADMIN_EMAIL") or "").strip().lower()
    if not email:
        raise RuntimeError("Set ALEXA_OWNER_EMAIL or ADMIN_EMAIL in Railway")
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM users WHERE lower(email)=? LIMIT 1", (email,)).fetchone()
        if not row:
            raise RuntimeError("Alexa owner user was not found")
        return int(row["id"])
    finally:
        conn.close()


def _today_expr() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _today_pnl(user_id: int) -> dict[str, Any]:
    conn = get_db()
    try:
        date_key = _today_expr()
        row = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(pnl,0)),0) AS pnl FROM paper_trades WHERE user_id=? AND substr(COALESCE(created_at,''),1,10)=?", (user_id, date_key)).fetchone()
        return {"count": int(row["n"] or 0), "pnl": float(row["pnl"] or 0)}
    finally:
        conn.close()


def _open_positions(user_id: int) -> list[dict[str, Any]]:
    conn = get_db()
    try:
        rows = conn.execute("SELECT symbol, side, entry_price, qty, pnl, last_ltp FROM paper_trades WHERE user_id=? AND upper(COALESCE(status,''))='OPEN' ORDER BY id DESC LIMIT 5", (user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _latest_signal(user_id: int, instrument: str = "") -> dict[str, Any] | None:
    conn = get_db()
    try:
        if instrument:
            row = conn.execute("SELECT instrument, score, signal, price, adx, volume_ratio, created_at FROM signal_history WHERE user_id=? AND upper(instrument)=upper(?) ORDER BY id DESC LIMIT 1", (user_id, instrument)).fetchone()
        else:
            row = conn.execute("SELECT instrument, score, signal, price, adx, volume_ratio, created_at FROM signal_history WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _bot_status(user_id: int) -> dict[str, Any]:
    conn = get_db()
    try:
        row = conn.execute("SELECT is_running,last_signal,total_trades,total_pnl,updated_at FROM bot_status WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else {"is_running": 0, "last_signal": "WAITING", "total_trades": 0, "total_pnl": 0}
    finally:
        conn.close()


def _last_trade(user_id: int) -> dict[str, Any] | None:
    conn = get_db()
    try:
        row = conn.execute("SELECT symbol,side,entry_price,exit_price,qty,pnl,status,reason,created_at FROM paper_trades WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _speak(handler_input: HandlerInput, text: str, reprompt: str | None = None) -> Response:
    b = handler_input.response_builder.speak(text)
    if reprompt:
        b.ask(reprompt)
    return b.response


class LaunchHandler(AbstractRequestHandler):
    def can_handle(self, handler_input): return is_request_type("LaunchRequest")(handler_input)
    def handle(self, handler_input):
        _owner_user_id(); return _speak(handler_input, "Option King ready hai. P and L, open positions, AI signal, last trade, ya bot status pucho.", "Kya check karna hai?")

class PnlHandler(AbstractRequestHandler):
    def can_handle(self, handler_input): return is_intent_name("TodayPnlIntent")(handler_input)
    def handle(self, handler_input):
        d=_today_pnl(_owner_user_id()); side="profit" if d["pnl"]>=0 else "loss"; return _speak(handler_input,f"Aaj {d['count']} trade hue. Net {side} {_money(d['pnl'])} rupaye hai.")

class PositionsHandler(AbstractRequestHandler):
    def can_handle(self, handler_input): return is_intent_name("OpenPositionsIntent")(handler_input)
    def handle(self, handler_input):
        rows=_open_positions(_owner_user_id())
        if not rows: return _speak(handler_input,"Abhi koi open position nahi hai.")
        parts=[f"{r.get('symbol') or 'position'}, quantity {r.get('qty') or 0}, current P and L {_money(r.get('pnl'))} rupaye" for r in rows]
        return _speak(handler_input,f"{len(rows)} open position hain. "+". ".join(parts)+".")

class SignalHandler(AbstractRequestHandler):
    def can_handle(self, handler_input): return is_intent_name("AiSignalIntent")(handler_input)
    def handle(self, handler_input):
        slots=getattr(handler_input.request_envelope.request.intent,"slots",{}) or {}; instrument=str((slots.get("instrument").value if slots.get("instrument") else "") or "").strip(); s=_latest_signal(_owner_user_id(),instrument)
        if not s: return _speak(handler_input,"Abhi AI signal data available nahi hai.")
        return _speak(handler_input,f"{s.get('instrument') or 'Market'} ka latest signal {s.get('signal') or 'WAIT'} hai. Score {int(s.get('score') or 0)} hai. ADX {_money(s.get('adx'))}.")

class LastTradeHandler(AbstractRequestHandler):
    def can_handle(self, handler_input): return is_intent_name("LastTradeIntent")(handler_input)
    def handle(self, handler_input):
        t=_last_trade(_owner_user_id())
        if not t: return _speak(handler_input,"Abhi koi trade record nahi mila.")
        return _speak(handler_input,f"Last trade {t.get('symbol') or ''} tha. Status {str(t.get('status') or '').lower()}. Quantity {t.get('qty') or 0}. P and L {_money(t.get('pnl'))} rupaye. Reason {t.get('reason') or 'available nahi'}.")

class BotStatusHandler(AbstractRequestHandler):
    def can_handle(self, handler_input): return is_intent_name("BotStatusIntent")(handler_input)
    def handle(self, handler_input):
        s=_bot_status(_owner_user_id()); running="running" if int(s.get("is_running") or 0) else "stopped"; return _speak(handler_input,f"Bot {running} hai. Last signal {s.get('last_signal') or 'WAITING'} hai. Total P and L {_money(s.get('total_pnl'))} rupaye.")

class WhyNoTradeHandler(AbstractRequestHandler):
    def can_handle(self, handler_input): return is_intent_name("WhyNoTradeIntent")(handler_input)
    def handle(self, handler_input):
        s=_latest_signal(_owner_user_id())
        if not s: return _speak(handler_input,"Recent scan data nahi mila, isliye exact no-trade reason nahi bata sakta.")
        return _speak(handler_input,f"Latest scan me signal {str(s.get('signal') or 'WAIT')} tha aur score {int(s.get('score') or 0)} tha. Detailed missed-trade reason next version me audit ledger se directly read hoga.")

class HelpHandler(AbstractRequestHandler):
    def can_handle(self, handler_input): return is_intent_name("AMAZON.HelpIntent")(handler_input)
    def handle(self, handler_input): return _speak(handler_input,"Aap bol sakte hain: aaj ka P and L, open positions, Nifty signal, last trade, bot status, ya trade kyon nahi liya.")

class StopHandler(AbstractRequestHandler):
    def can_handle(self, handler_input): return is_intent_name("AMAZON.StopIntent")(handler_input) or is_intent_name("AMAZON.CancelIntent")(handler_input)
    def handle(self, handler_input): return handler_input.response_builder.speak("Theek hai.").set_should_end_session(True).response

class CatchAll(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception): return True
    def handle(self, handler_input, exception):
        print(f"OPTION KING ALEXA ERROR | {exception}",flush=True); return _speak(handler_input,"Option King data read karne me problem aayi. Thodi der baad dobara try karo.")

sb=SkillBuilder()
for h in [LaunchHandler(),PnlHandler(),PositionsHandler(),SignalHandler(),LastTradeHandler(),BotStatusHandler(),WhyNoTradeHandler(),HelpHandler(),StopHandler()]: sb.add_request_handler(h)
sb.add_exception_handler(CatchAll())
_alexa_skill=sb.create()


def _get_webservice_handler():
    # Lazy import is intentional: ask-sdk-webservice-support pulls oscrypto/certvalidator,
    # which can fail to load libcrypto on Railway. The main SaaS must still boot/healthcheck.
    from ask_sdk_webservice_support.webservice_handler import WebserviceSkillHandler
    return WebserviceSkillHandler(skill=_alexa_skill, verify_signature=True, verify_timestamp=True)


@router.post("/api/alexa")
async def alexa_endpoint(request: Request):
    body=(await request.body()).decode("utf-8")
    try:
        result=_get_webservice_handler().verify_request_and_dispatch(dict(request.headers),body)
        if not isinstance(result,str): result=json.dumps(result)
        return JSONResponse(content=json.loads(result))
    except Exception as exc:
        print(f"OPTION KING ALEXA VERIFY ERROR | {exc}",flush=True)
        return JSONResponse(status_code=400,content={"error":"invalid alexa request"})

@router.get("/api/alexa/health")
def alexa_health():
    try:
        user_id=_owner_user_id(); return {"status":"ok","mode":"read_only","user_id":user_id}
    except Exception as exc:
        return JSONResponse(status_code=503,content={"status":"setup_required","detail":str(exc)})

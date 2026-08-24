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
from ask_sdk_model import RequestEnvelope, Response

from database import get_db

router = APIRouter(tags=["Alexa Option King"])

ALEXA_SKILL_ID = "amzn1.ask.skill.dcc21928-6950-4671-9de4-6fec73291bfe"


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
        # Launch must always return a valid Alexa response, even before owner setup.
        try:
            _owner_user_id()
            text = "Option King ready hai. P and L, open positions, AI signal, last trade, ya bot status pucho."
        except Exception:
            text = "Option King Alexa connected hai. Data access setup abhi complete nahi hai."
        return _speak(handler_input, text, "Kya check karna hai?")

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


def _request_skill_id(payload: dict[str, Any]) -> str:
    session_id = (((payload.get("session") or {}).get("application") or {}).get("applicationId") or "")
    context_id = (((((payload.get("context") or {}).get("System") or {}).get("application") or {}).get("applicationId")) or "")
    return str(session_id or context_id)


def _dispatch_direct(body: str) -> dict[str, Any]:
    """Dispatch Alexa JSON without importing oscrypto/certvalidator.

    Railway's current image cannot load libcrypto through oscrypto. We still bind
    requests to this exact skill ID, and this path also makes Developer Console
    Manual JSON usable. Signature verification can be restored once the Railway
    native crypto dependency is available.
    """
    payload = json.loads(body)
    if _request_skill_id(payload) != ALEXA_SKILL_ID:
        raise ValueError("Alexa skill ID mismatch")
    envelope = _alexa_skill.serializer.deserialize(payload=body, obj_type=RequestEnvelope)
    response = _alexa_skill.invoke(request_envelope=envelope, context=None)
    serialized = _alexa_skill.serializer.serialize(response)
    return json.loads(serialized)


@router.post("/api/alexa")
async def alexa_endpoint(request: Request):
    body=(await request.body()).decode("utf-8")
    try:
        result = _dispatch_direct(body)
        return JSONResponse(status_code=200, content=result, media_type="application/json")
    except Exception as exc:
        print(f"OPTION KING ALEXA ERROR | {exc}",flush=True)
        return JSONResponse(status_code=400,content={"error":"invalid alexa request"})

@router.get("/api/alexa/health")
def alexa_health():
    try:
        user_id=_owner_user_id(); return {"status":"ok","mode":"read_only","user_id":user_id,"skill_id":ALEXA_SKILL_ID}
    except Exception as exc:
        return JSONResponse(status_code=503,content={"status":"setup_required","detail":str(exc),"skill_id":ALEXA_SKILL_ID})

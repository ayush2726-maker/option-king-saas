from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

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


def _today_pnl(user_id: int) -> dict[str, Any]:
    conn = get_db()
    try:
        date_key = datetime.now().strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(pnl,0)),0) AS pnl FROM paper_trades WHERE user_id=? AND substr(COALESCE(created_at,''),1,10)=?",
            (user_id, date_key),
        ).fetchone()
        return {"count": int(row["n"] or 0), "pnl": float(row["pnl"] or 0)}
    finally:
        conn.close()


def _open_positions(user_id: int) -> list[dict[str, Any]]:
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT symbol, side, entry_price, qty, pnl, last_ltp FROM paper_trades WHERE user_id=? AND upper(COALESCE(status,''))='OPEN' ORDER BY id DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _latest_signal(user_id: int, instrument: str = "") -> dict[str, Any] | None:
    conn = get_db()
    try:
        if instrument:
            row = conn.execute(
                "SELECT instrument, score, signal, price, adx, volume_ratio, created_at FROM signal_history WHERE user_id=? AND upper(instrument)=upper(?) ORDER BY id DESC LIMIT 1",
                (user_id, instrument),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT instrument, score, signal, price, adx, volume_ratio, created_at FROM signal_history WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _bot_status(user_id: int) -> dict[str, Any]:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT is_running,last_signal,total_trades,total_pnl,updated_at FROM bot_status WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else {"is_running": 0, "last_signal": "WAITING", "total_trades": 0, "total_pnl": 0}
    finally:
        conn.close()


def _last_trade(user_id: int) -> dict[str, Any] | None:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT symbol,side,entry_price,exit_price,qty,pnl,status,reason,created_at FROM paper_trades WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _alexa_response(text: str, *, end_session: bool = False, reprompt: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "outputSpeech": {"type": "PlainText", "text": text},
        "shouldEndSession": end_session,
    }
    if reprompt and not end_session:
        response["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "sessionAttributes": {}, "response": response}


def _request_skill_id(payload: dict[str, Any]) -> str:
    session_id = (((payload.get("session") or {}).get("application") or {}).get("applicationId") or "")
    context_id = (((((payload.get("context") or {}).get("System") or {}).get("application") or {}).get("applicationId")) or "")
    return str(session_id or context_id)


def _slot_value(request_data: dict[str, Any], name: str) -> str:
    intent = request_data.get("intent") or {}
    slot = (intent.get("slots") or {}).get(name) or {}
    return str(slot.get("value") or "").strip()


def _dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    if _request_skill_id(payload) != ALEXA_SKILL_ID:
        raise ValueError("Alexa skill ID mismatch")

    req = payload.get("request") or {}
    req_type = str(req.get("type") or "")

    if req_type == "LaunchRequest":
        try:
            _owner_user_id()
            text = "Option King ready hai. P and L, open positions, AI signal, last trade, ya bot status pucho."
        except Exception:
            text = "Option King Alexa connected hai. Data access setup abhi complete nahi hai."
        return _alexa_response(text, reprompt="Kya check karna hai?")

    if req_type != "IntentRequest":
        return _alexa_response("Option King ready hai. Kya check karna hai?", reprompt="P and L, signal, ya bot status pucho.")

    intent = req.get("intent") or {}
    name = str(intent.get("name") or "")

    if name in {"AMAZON.StopIntent", "AMAZON.CancelIntent"}:
        return _alexa_response("Theek hai.", end_session=True)

    if name == "AMAZON.HelpIntent":
        return _alexa_response("Aap bol sakte hain: aaj ka P and L, open positions, Nifty signal, last trade, bot status, ya trade kyon nahi liya.")

    try:
        user_id = _owner_user_id()
    except Exception:
        return _alexa_response("Option King Alexa connected hai, lekin Railway me Alexa owner email setup abhi complete nahi hai.")

    try:
        if name == "TodayPnlIntent":
            d = _today_pnl(user_id)
            side = "profit" if d["pnl"] >= 0 else "loss"
            return _alexa_response(f"Aaj {d['count']} trade hue. Net {side} {_money(d['pnl'])} rupaye hai.")

        if name == "OpenPositionsIntent":
            rows = _open_positions(user_id)
            if not rows:
                return _alexa_response("Abhi koi open position nahi hai.")
            parts = [f"{r.get('symbol') or 'position'}, quantity {r.get('qty') or 0}, current P and L {_money(r.get('pnl'))} rupaye" for r in rows]
            return _alexa_response(f"{len(rows)} open position hain. " + ". ".join(parts) + ".")

        if name == "AiSignalIntent":
            instrument = _slot_value(req, "instrument")
            s = _latest_signal(user_id, instrument)
            if not s:
                return _alexa_response("Abhi AI signal data available nahi hai.")
            return _alexa_response(f"{s.get('instrument') or 'Market'} ka latest signal {s.get('signal') or 'WAIT'} hai. Score {int(s.get('score') or 0)} hai. ADX {_money(s.get('adx'))}.")

        if name == "LastTradeIntent":
            t = _last_trade(user_id)
            if not t:
                return _alexa_response("Abhi koi trade record nahi mila.")
            return _alexa_response(f"Last trade {t.get('symbol') or ''} tha. Status {str(t.get('status') or '').lower()}. Quantity {t.get('qty') or 0}. P and L {_money(t.get('pnl'))} rupaye. Reason {t.get('reason') or 'available nahi'}.")

        if name == "BotStatusIntent":
            s = _bot_status(user_id)
            running = "running" if int(s.get("is_running") or 0) else "stopped"
            return _alexa_response(f"Bot {running} hai. Last signal {s.get('last_signal') or 'WAITING'} hai. Total P and L {_money(s.get('total_pnl'))} rupaye.")

        if name == "WhyNoTradeIntent":
            s = _latest_signal(user_id)
            if not s:
                return _alexa_response("Recent scan data nahi mila, isliye exact no trade reason nahi bata sakta.")
            return _alexa_response(f"Latest scan me signal {str(s.get('signal') or 'WAIT')} tha aur score {int(s.get('score') or 0)} tha.")

        return _alexa_response("Ye command samajh nahi aayi. P and L, open positions, AI signal, last trade, ya bot status pucho.")
    except Exception as exc:
        print(f"OPTION KING ALEXA DATA ERROR | {exc}", flush=True)
        return _alexa_response("Option King data read karne me problem aayi. Thodi der baad dobara try karo.")


@router.post("/api/alexa")
async def alexa_endpoint(request: Request):
    try:
        payload = await request.json()
        result = _dispatch(payload)
        return JSONResponse(status_code=200, content=result)
    except Exception as exc:
        print(f"OPTION KING ALEXA REQUEST ERROR | {exc}", flush=True)
        return JSONResponse(status_code=400, content={"error": "invalid alexa request"})


@router.get("/api/alexa/health")
def alexa_health():
    try:
        user_id = _owner_user_id()
        return {"status": "ok", "mode": "read_only", "user_id": user_id, "skill_id": ALEXA_SKILL_ID}
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "setup_required", "detail": str(exc), "skill_id": ALEXA_SKILL_ID})

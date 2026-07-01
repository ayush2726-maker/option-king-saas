from fastapi import APIRouter, Header
from database import get_db
from auth.routes import get_current_user
import json
from datetime import datetime
from telegram.routes import notify_user

router = APIRouter(prefix="/strategy", tags=["Strategy"])

DEFAULT_SETTINGS = {
    "mode": "default",
    "base_score": 40,
    "adx_score": 20,
    "volume_score": 20,
    "mtf_score": 10,
    "regime_score": 10,
    "entry_threshold": 82,
    "adx_threshold": 25,
    "volume_threshold": 1.2,
    "max_trades_per_day": 5,
    "sl_percent": 12,
    "target_percent": 24,
    "trailing_sl": True,
    "expiry_gamma_mode": True,
    "trading_mode": "paper",
    "paper_capital": 100000,
    "allow_custom": True
}

PRESETS = {
    "safe": {
        **DEFAULT_SETTINGS,
        "mode": "safe",
        "entry_threshold": 88,
        "max_trades_per_day": 3,
        "sl_percent": 10,
        "target_percent": 20,
    },
    "default": DEFAULT_SETTINGS,
    "aggressive": {
        **DEFAULT_SETTINGS,
        "mode": "aggressive",
        "entry_threshold": 76,
        "max_trades_per_day": 7,
        "sl_percent": 15,
        "target_percent": 30,
    }
}

def clamp_num(v, lo, hi, default):
    try:
        x = float(v)
        if x < lo:
            return lo
        if x > hi:
            return hi
        return x
    except Exception:
        return default

def normalize_settings(body: dict):
    base = dict(DEFAULT_SETTINGS)
    mode = str(body.get("mode", base["mode"])).lower()
    if mode in PRESETS and mode != "custom":
        base.update(PRESETS[mode])
    else:
        base["mode"] = "custom"

    for k in ["base_score", "adx_score", "volume_score", "mtf_score", "regime_score"]:
        base[k] = int(clamp_num(body.get(k, base[k]), 0, 100, base[k]))

    total = base["base_score"] + base["adx_score"] + base["volume_score"] + base["mtf_score"] + base["regime_score"]
    if total <= 0:
        base.update(DEFAULT_SETTINGS)
        total = 100

    # Normalize to total 100
    if total != 100:
        for k in ["base_score", "adx_score", "volume_score", "mtf_score", "regime_score"]:
            base[k] = round(base[k] * 100 / total)

        diff = 100 - (base["base_score"] + base["adx_score"] + base["volume_score"] + base["mtf_score"] + base["regime_score"])
        base["base_score"] += diff

    base["entry_threshold"] = int(clamp_num(body.get("entry_threshold", base["entry_threshold"]), 50, 99, base["entry_threshold"]))
    base["adx_threshold"] = int(clamp_num(body.get("adx_threshold", base["adx_threshold"]), 5, 60, base["adx_threshold"]))
    base["volume_threshold"] = clamp_num(body.get("volume_threshold", base["volume_threshold"]), 0.5, 5, base["volume_threshold"])
    base["max_trades_per_day"] = int(clamp_num(body.get("max_trades_per_day", base["max_trades_per_day"]), 1, 20, base["max_trades_per_day"]))
    base["sl_percent"] = clamp_num(body.get("sl_percent", base["sl_percent"]), 3, 50, base["sl_percent"])
    base["target_percent"] = clamp_num(body.get("target_percent", base["target_percent"]), 5, 100, base["target_percent"])
    base["trailing_sl"] = bool(body.get("trailing_sl", base["trailing_sl"]))
    base["expiry_gamma_mode"] = bool(body.get("expiry_gamma_mode", base["expiry_gamma_mode"]))
    base["trading_mode"] = "live" if str(body.get("trading_mode", base.get("trading_mode", "paper"))).lower() == "live" else "paper"
    base["paper_capital"] = clamp_num(body.get("paper_capital", base.get("paper_capital", 100000)), 1000, 10000000, base.get("paper_capital", 100000))
    base["allow_custom"] = True
    return base

def ensure_settings(conn, user_id: int):
    row = conn.execute(
        "SELECT settings_json FROM strategy_settings WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if row:
        try:
            saved = json.loads(row["settings_json"])
            final = dict(DEFAULT_SETTINGS)
            final.update(saved)
            return final
        except Exception:
            pass

    settings = dict(DEFAULT_SETTINGS)
    conn.execute(
        "INSERT OR REPLACE INTO strategy_settings (user_id, settings_json, updated_at) VALUES (?, ?, ?)",
        (user_id, json.dumps(settings), datetime.utcnow().isoformat())
    )
    conn.commit()
    return settings

@router.get("/settings")
def get_settings(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    settings = ensure_settings(conn, user["id"])
    conn.close()
    return {"success": True, "settings": settings, "presets": PRESETS}

@router.post("/settings")
def save_settings(body: dict, authorization: str = Header(None)):
    user = get_current_user(authorization)
    settings = normalize_settings(body or {})
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO strategy_settings (user_id, settings_json, updated_at) VALUES (?, ?, ?)",
        (user["id"], json.dumps(settings), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    notify_user(user["id"], f"⚙️ <b>Strategy Settings Saved</b>\nMode: {settings.get('mode')}\nEntry Score: {settings.get('entry_threshold')}\nSL: {settings.get('sl_percent')}%\nTarget: {settings.get('target_percent')}%")
    return {"success": True, "message": "Strategy settings saved", "settings": settings}

@router.post("/reset")
def reset_settings(authorization: str = Header(None)):
    user = get_current_user(authorization)
    conn = get_db()
    settings = dict(DEFAULT_SETTINGS)
    conn.execute(
        "INSERT OR REPLACE INTO strategy_settings (user_id, settings_json, updated_at) VALUES (?, ?, ?)",
        (user["id"], json.dumps(settings), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    return {"success": True, "message": "Default strategy restored", "settings": settings}

"""Custom date-range backtest routes.

Provides a continuous-capital backtest from any completed start date to end date,
with day-wise, month-wise and year-wise summaries from the exact same strategy
engine used by Daily/Monthly backtests.
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Header

from backtest import routes


router = APIRouter(prefix="/backtest", tags=["Backtest"])

_ALLOWED_INSTRUMENTS = {"AUTO", "NIFTY", "BANKNIFTY", "SENSEX"}
_ALLOWED_STRATEGY_MODES = {"NORMAL", "HERO_ZERO", "COMBINED"}
_MAX_CALENDAR_DAYS = 366
_STALE_JOB_SECONDS = 4 * 60 * 60

_RANGE_JOBS: dict[str, dict] = {}
_RANGE_JOBS_LOCK = threading.Lock()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ist_today():
    return (_utc_now() + timedelta(hours=5, minutes=30)).date()


def _parse_date(value: object, field_name: str):
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception as exc:
        raise ValueError(f"{field_name} YYYY-MM-DD format me daalein.") from exc


def _range_weekdays(start_date: str, end_date: str) -> list[str]:
    start = _parse_date(start_date, "Start date")
    end = _parse_date(end_date, "End date")
    if end < start:
        raise ValueError("End date start date se pehle nahi ho sakti.")
    if (end - start).days + 1 > _MAX_CALENDAR_DAYS:
        raise ValueError("Ek range me maximum 366 calendar days allowed hain.")

    today = _ist_today()
    final_end = min(end, today - timedelta(days=1))
    if final_end < start:
        raise ValueError("Range me koi completed historical date available nahi hai.")

    dates: list[str] = []
    cursor = start
    while cursor <= final_end:
        if cursor.weekday() < 5:
            dates.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return dates


def _normalize_request(body: dict | None) -> dict:
    payload = dict(body or {})
    start = _parse_date(payload.get("start_date"), "Start date")
    end = _parse_date(payload.get("end_date"), "End date")
    # Full validation and completed-date clipping happen here too.
    _range_weekdays(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

    instrument = str(payload.get("instrument") or "AUTO").upper().strip()
    if instrument not in _ALLOWED_INSTRUMENTS:
        instrument = "AUTO"

    strategy_mode = str(payload.get("strategy_mode") or "NORMAL").upper().strip()
    if strategy_mode not in _ALLOWED_STRATEGY_MODES:
        strategy_mode = "NORMAL"

    try:
        capital = float(payload.get("capital") or payload.get("paper_capital") or 100000)
    except Exception as exc:
        raise ValueError("Backtest capital invalid hai.") from exc
    if not (capital >= 1000 and capital < 1_000_000_000):
        raise ValueError("Backtest capital kam se kam Rs 1,000 hona chahiye.")

    return {
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
        "instrument": instrument,
        "strategy_mode": strategy_mode,
        "capital": capital,
        "entry_threshold": 82,
        "sl_percent": 0.0,
        "target_percent": 0.0,
    }


def _new_bucket(label: str, capital: float) -> dict:
    return {
        "label": label,
        "tested_days": 0,
        "skipped_days": 0,
        "winning_days": 0,
        "losing_days": 0,
        "flat_days": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
        "capital_start": round(float(capital), 2),
        "capital_end": round(float(capital), 2),
        "equity_curve": [float(capital)],
    }


def _update_bucket(bucket: dict, day: dict) -> None:
    if day.get("status") == "SKIPPED":
        bucket["skipped_days"] += 1
    else:
        bucket["tested_days"] += 1
        status = str(day.get("status") or "FLAT").upper()
        if status == "PROFIT":
            bucket["winning_days"] += 1
        elif status == "LOSS":
            bucket["losing_days"] += 1
        else:
            bucket["flat_days"] += 1
        bucket["trades"] += int(day.get("trades") or 0)
        bucket["wins"] += int(day.get("wins") or 0)
        bucket["losses"] += int(day.get("losses") or 0)
        bucket["pnl"] += float(day.get("pnl") or 0)

    bucket["capital_end"] = round(float(day.get("capital_end") or bucket["capital_end"]), 2)
    bucket["equity_curve"].append(float(bucket["capital_end"]))


def _finalize_bucket(bucket: dict) -> dict:
    output = dict(bucket)
    curve = output.pop("equity_curve", [])
    drawdown = routes._okai_month_drawdown(curve)
    trades = int(output.get("trades") or 0)
    wins = int(output.get("wins") or 0)
    output["pnl"] = round(float(output.get("pnl") or 0), 2)
    output["win_rate"] = round(wins / trades * 100, 2) if trades else 0.0
    output["max_drawdown"] = drawdown["max_drawdown"]
    output["max_drawdown_percent"] = drawdown["max_drawdown_percent"]
    return output


def _update_job(job_id: str, **updates) -> None:
    if not job_id:
        return
    with _RANGE_JOBS_LOCK:
        job = _RANGE_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)
        job["updated_at"] = _utc_now().isoformat()


def _trim_jobs() -> None:
    with _RANGE_JOBS_LOCK:
        if len(_RANGE_JOBS) <= 24:
            return
        ordered = sorted(
            _RANGE_JOBS.items(),
            key=lambda item: str(item[1].get("updated_at") or item[1].get("created_at") or ""),
        )
        for job_id, job in ordered:
            if len(_RANGE_JOBS) <= 18:
                break
            if str(job.get("status") or "").upper() not in {"QUEUED", "RUNNING"}:
                _RANGE_JOBS.pop(job_id, None)


def _connect_broker(user_id: int):
    conn = routes.get_db()
    routes.ensure_backtest_table(conn)
    broker = conn.execute(
        """SELECT * FROM broker_credentials
           WHERE user_id=? AND is_active=1
           ORDER BY last_connected DESC LIMIT 1""",
        (user_id,),
    ).fetchone()
    conn.close()
    if not broker:
        raise RuntimeError("Broker connect karein range backtest ke liye.")

    broker_name = str(broker["broker_name"] or "").lower()
    creds = {
        "api_key": routes.decrypt_credential(broker["api_key"]),
        "client_id": broker["client_id"],
        "password": routes.decrypt_credential(broker["api_secret"]),
        "totp_secret": (
            routes.decrypt_credential(broker["totp_secret"])
            if broker["totp_secret"]
            else None
        ),
    }

    if broker_name == "angelone":
        obj = routes.angel_login(creds)
    else:
        obj = routes.create_broker(
            broker_name,
            creds["client_id"],
            creds["api_key"],
            creds["password"],
            creds.get("totp_secret"),
        )
        login_result = obj.login()
        if not login_result.get("success"):
            raise RuntimeError(
                "Broker login failed: " + str(login_result.get("message") or "")[:160]
            )
    return broker_name, obj


def _range_worker(job_id: str, payload: dict, authorization: str | None) -> None:
    try:
        user = routes.get_current_user(authorization)
        date_list = _range_weekdays(payload["start_date"], payload["end_date"])
        if not date_list:
            raise RuntimeError("Range me koi weekday available nahi hai.")

        _update_job(
            job_id,
            status="RUNNING",
            phase="LOGIN_AND_DATA",
            total_days=len(date_list),
            completed_days=0,
        )
        broker_name, obj = _connect_broker(user["id"])

        starting_capital = float(payload["capital"])
        current_capital = starting_capital
        day_results: list[dict] = []
        equity_curve = [starting_capital]
        month_buckets: dict[str, dict] = {}
        year_buckets: dict[str, dict] = {}

        totals = {
            "tested_days": 0,
            "skipped_days": 0,
            "winning_days": 0,
            "losing_days": 0,
            "flat_days": 0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "normal_pnl": 0.0,
            "hero_zero_pnl": 0.0,
        }

        for index, date_text in enumerate(date_list):
            _update_job(
                job_id,
                phase="RUNNING",
                current_date=date_text,
                completed_days=index,
                total_days=len(date_list),
            )
            if index > 0:
                time.sleep(0.75)

            raw_day = routes._okai_run_backtest_mode(
                broker_name=broker_name,
                obj=obj,
                instrument=payload["instrument"],
                date_str=date_text,
                capital=current_capital,
                entry_threshold=82,
                sl_percent=0.0,
                target_percent=0.0,
                strategy_mode=payload["strategy_mode"],
            )
            day = routes._json_safe(raw_day, {"non_finite": 0})

            if not isinstance(day, dict) or not day.get("success"):
                totals["skipped_days"] += 1
                row = {
                    "date": date_text,
                    "status": "SKIPPED",
                    "message": day.get("message") if isinstance(day, dict) else "Invalid result",
                    "capital_start": round(current_capital, 2),
                    "capital_end": round(current_capital, 2),
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0.0,
                    "pnl": 0.0,
                }
            else:
                day_pnl = float(day.get("total_pnl") or 0)
                ending_capital = float(day.get("ending_capital") or current_capital + day_pnl)
                status = "PROFIT" if day_pnl > 0 else "LOSS" if day_pnl < 0 else "FLAT"
                totals["tested_days"] += 1
                totals["winning_days"] += int(status == "PROFIT")
                totals["losing_days"] += int(status == "LOSS")
                totals["flat_days"] += int(status == "FLAT")
                totals["trades"] += int(day.get("total_trades") or 0)
                totals["wins"] += int(day.get("wins") or 0)
                totals["losses"] += int(day.get("losses") or 0)
                totals["normal_pnl"] += float(day.get("normal_pnl") or 0)
                totals["hero_zero_pnl"] += float(day.get("hero_zero_pnl") or 0)

                row = {
                    "date": date_text,
                    "status": status,
                    "capital_start": round(current_capital, 2),
                    "capital_end": round(ending_capital, 2),
                    "trades": int(day.get("total_trades") or 0),
                    "wins": int(day.get("wins") or 0),
                    "losses": int(day.get("losses") or 0),
                    "win_rate": float(day.get("win_rate") or 0),
                    "pnl": round(day_pnl, 2),
                    "normal_pnl": round(float(day.get("normal_pnl") or 0), 2),
                    "hero_zero_pnl": round(float(day.get("hero_zero_pnl") or 0), 2),
                    "max_score": day.get("debug_max_score"),
                    "per_instrument": day.get("per_instrument") or {},
                }
                current_capital = ending_capital

            day_results.append(row)
            equity_curve.append(current_capital)

            month_key = date_text[:7]
            year_key = date_text[:4]
            month_bucket = month_buckets.setdefault(
                month_key,
                _new_bucket(month_key, row["capital_start"]),
            )
            year_bucket = year_buckets.setdefault(
                year_key,
                _new_bucket(year_key, row["capital_start"]),
            )
            _update_bucket(month_bucket, row)
            _update_bucket(year_bucket, row)

            _update_job(
                job_id,
                completed_days=index + 1,
                total_days=len(date_list),
                current_date=date_text,
                phase="RUNNING",
            )

        net_pnl = round(current_capital - starting_capital, 2)
        win_rate = (
            round(totals["wins"] / totals["trades"] * 100, 2)
            if totals["trades"]
            else 0.0
        )
        drawdown = routes._okai_month_drawdown(equity_curve)
        months = [_finalize_bucket(month_buckets[key]) for key in sorted(month_buckets)]
        years = [_finalize_bucket(year_buckets[key]) for key in sorted(year_buckets)]

        result = {
            "success": True,
            "period": "RANGE",
            "start_date": payload["start_date"],
            "end_date": payload["end_date"],
            "instrument": payload["instrument"],
            "strategy_mode": payload["strategy_mode"],
            "capital": round(starting_capital, 2),
            "ending_capital": round(current_capital, 2),
            "total_pnl": net_pnl,
            "normal_pnl": round(totals["normal_pnl"], 2),
            "hero_zero_pnl": round(totals["hero_zero_pnl"], 2),
            "total_trades": totals["trades"],
            "wins": totals["wins"],
            "losses": totals["losses"],
            "win_rate": win_rate,
            "tested_days": totals["tested_days"],
            "skipped_days": totals["skipped_days"],
            "winning_days": totals["winning_days"],
            "losing_days": totals["losing_days"],
            "flat_days": totals["flat_days"],
            "max_drawdown": drawdown["max_drawdown"],
            "max_drawdown_percent": drawdown["max_drawdown_percent"],
            "days": day_results,
            "months": months,
            "years": years,
            "equity_curve": [round(value, 2) for value in equity_curve],
            "position_sizing": {
                "mode": "CONTINUOUS_CAPITAL_BASED_ALLOCATION",
                "equity_compounding": True,
                "whole_lots_only": True,
                "auto_slot_1_percent": 50,
                "auto_slot_2_percent": 40,
                "reserve_percent": 10,
            },
            "summary": {
                "period": "RANGE",
                "start_date": payload["start_date"],
                "end_date": payload["end_date"],
                "instrument": payload["instrument"],
                "strategy_mode": payload["strategy_mode"],
                "capital": round(starting_capital, 2),
                "ending_capital": round(current_capital, 2),
                "net_pnl": net_pnl,
                "trades": totals["trades"],
                "wins": totals["wins"],
                "losses": totals["losses"],
                "win_rate": win_rate,
                "tested_days": totals["tested_days"],
                "skipped_days": totals["skipped_days"],
                "winning_days": totals["winning_days"],
                "losing_days": totals["losing_days"],
                "flat_days": totals["flat_days"],
                "max_drawdown": drawdown["max_drawdown"],
                "max_drawdown_percent": drawdown["max_drawdown_percent"],
            },
            "note": (
                "Custom range uses one continuous capital curve from start date to end date. "
                "Day, month and year breakdowns are views of the same run."
            ),
        }

        _update_job(
            job_id,
            status="COMPLETED",
            phase="COMPLETED",
            completed_days=len(date_list),
            total_days=len(date_list),
            current_date=date_list[-1],
            result=result,
            error=None,
        )
        try:
            routes.notify_user(
                user["id"],
                "📅 Range Backtest Complete\n"
                f"{payload['start_date']} to {payload['end_date']}\n"
                f"Trades: {totals['trades']}\n"
                f"Net P&L: Rs {net_pnl}\n"
                f"Ending Capital: Rs {round(current_capital, 2)}",
            )
        except Exception:
            pass
    except Exception as exc:
        _update_job(
            job_id,
            status="FAILED",
            phase="FAILED",
            error=f"{exc.__class__.__name__}: {str(exc)}"[:300],
        )


@router.post("/range")
def start_range_backtest(
    background_tasks: BackgroundTasks,
    body: dict,
    authorization: str = Header(None),
):
    try:
        user = routes.get_current_user(authorization)
        payload = _normalize_request(body)
        now = _utc_now()
        now_text = now.isoformat()

        with _RANGE_JOBS_LOCK:
            for existing_id, existing in list(_RANGE_JOBS.items()):
                if existing.get("user_id") != user["id"]:
                    continue
                status = str(existing.get("status") or "").upper()
                updated_text = str(existing.get("updated_at") or existing.get("created_at") or "")
                try:
                    updated = datetime.fromisoformat(updated_text.replace("Z", "+00:00"))
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                except Exception:
                    updated = datetime.min.replace(tzinfo=timezone.utc)
                if status in {"QUEUED", "RUNNING"} and (now - updated).total_seconds() <= _STALE_JOB_SECONDS:
                    return {
                        "success": True,
                        "async": True,
                        "job_id": existing_id,
                        "status": status,
                        "phase": existing.get("phase"),
                        "message": "Range backtest already running.",
                    }
                if status in {"QUEUED", "RUNNING"}:
                    existing.update({
                        "status": "FAILED",
                        "phase": "STALE_JOB_EXPIRED",
                        "error": "Purana range job timeout ke baad clear kar diya gaya.",
                        "updated_at": now_text,
                    })

            job_id = uuid.uuid4().hex
            _RANGE_JOBS[job_id] = {
                "job_id": job_id,
                "user_id": user["id"],
                "status": "QUEUED",
                "phase": "QUEUED",
                "start_date": payload["start_date"],
                "end_date": payload["end_date"],
                "instrument": payload["instrument"],
                "strategy_mode": payload["strategy_mode"],
                "completed_days": 0,
                "total_days": 0,
                "current_date": None,
                "created_at": now_text,
                "updated_at": now_text,
                "result": None,
                "error": None,
            }

        background_tasks.add_task(_range_worker, job_id, payload, authorization)
        _trim_jobs()
        return {
            "success": True,
            "async": True,
            "job_id": job_id,
            "status": "QUEUED",
            "phase": "QUEUED",
            "start_date": payload["start_date"],
            "end_date": payload["end_date"],
            "instrument": payload["instrument"],
            "strategy_mode": payload["strategy_mode"],
            "message": "Date-range backtest background me start ho gaya.",
        }
    except Exception as exc:
        return {
            "success": False,
            "message": "Range job start failed: " + f"{exc.__class__.__name__}: {str(exc)}"[:220],
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }


@router.get("/range/status/{job_id}")
def range_backtest_status(job_id: str, authorization: str = Header(None)):
    try:
        user = routes.get_current_user(authorization)
        with _RANGE_JOBS_LOCK:
            job = _RANGE_JOBS.get(str(job_id))
            if not job:
                return {"success": False, "status": "NOT_FOUND", "message": "Range job nahi mila."}
            if job.get("user_id") != user["id"]:
                return {"success": False, "status": "FORBIDDEN", "message": "Ye range job is user ka nahi hai."}
            return {"success": True, **dict(job)}
    except Exception as exc:
        return {
            "success": False,
            "status": "FAILED",
            "message": str(exc),
            "error": str(exc),
        }

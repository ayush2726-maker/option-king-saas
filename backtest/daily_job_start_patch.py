"""Async daily backtest job API.

Daily backtest used to call /backtest/run synchronously from the mobile app.
On slower historical-data runs Android could lose/recreate the JS process before
the response returned, which looked like a logout. Monthly backtest already uses
a background job successfully, so this patch gives daily backtest the same
start -> poll -> result pattern while leaving the original /run route intact.
"""

from datetime import datetime, timezone
import asyncio
import inspect
import threading
import uuid

from fastapi import BackgroundTasks, Header

from backtest import routes


_ALLOWED_INSTRUMENTS = {"AUTO", "NIFTY", "BANKNIFTY", "SENSEX"}
_ALLOWED_STRATEGY_MODES = {"NORMAL", "HERO_ZERO", "COMBINED"}
_DAILY_JOBS = {}
_DAILY_JOBS_LOCK = threading.RLock()
_DAILY_JOBS_MAX = 80


def _now_text():
    return datetime.now(timezone.utc).isoformat()


def _normalize(body):
    payload = dict(body or {})
    date_text = str(payload.get("date") or "").strip()
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
    except Exception as exc:
        raise ValueError("Date YYYY-MM-DD format me daalein.") from exc

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

    payload.update({
        "date": date_text,
        "instrument": instrument,
        "strategy_mode": strategy_mode,
        "capital": capital,
        "entry_threshold": 82,
        "sl_percent": 0,
        "target_percent": 0,
    })
    return payload


def _find_original_run_endpoint():
    for route in routes.router.routes:
        path = str(getattr(route, "path", "") or "")
        methods = {str(x).upper() for x in (getattr(route, "methods", set()) or set())}
        if path == "/backtest/run" and "POST" in methods:
            return getattr(route, "endpoint", None)
    return None


def _trim_jobs():
    with _DAILY_JOBS_LOCK:
        if len(_DAILY_JOBS) <= _DAILY_JOBS_MAX:
            return
        ordered = sorted(
            _DAILY_JOBS.items(),
            key=lambda item: str(item[1].get("created_at") or ""),
        )
        for job_id, _job in ordered[: max(0, len(ordered) - _DAILY_JOBS_MAX)]:
            _DAILY_JOBS.pop(job_id, None)


def apply_daily_job_start_patch():
    if getattr(routes, "_okai_daily_job_v1", False):
        return

    original_endpoint = _find_original_run_endpoint()
    if original_endpoint is None:
        raise RuntimeError("Original /backtest/run endpoint not found")

    def worker(job_id, payload, authorization):
        with _DAILY_JOBS_LOCK:
            job = _DAILY_JOBS.get(job_id)
            if not job:
                return
            job.update({"status": "RUNNING", "phase": "RUNNING", "updated_at": _now_text()})

        try:
            result = original_endpoint(body=payload, authorization=authorization)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            safe = routes._json_safe(result) if hasattr(routes, "_json_safe") else result
            with _DAILY_JOBS_LOCK:
                job = _DAILY_JOBS.get(job_id)
                if job:
                    job.update({
                        "status": "COMPLETED",
                        "phase": "COMPLETED",
                        "result": safe,
                        "error": None,
                        "updated_at": _now_text(),
                    })
        except Exception as exc:
            with _DAILY_JOBS_LOCK:
                job = _DAILY_JOBS.get(job_id)
                if job:
                    job.update({
                        "status": "FAILED",
                        "phase": "FAILED",
                        "result": None,
                        "error": f"{exc.__class__.__name__}: {str(exc)}"[:300],
                        "updated_at": _now_text(),
                    })

    @routes.router.post("/daily/start")
    def start_daily_backtest(
        background_tasks: BackgroundTasks,
        body: dict,
        authorization: str = Header(None),
    ):
        try:
            user = routes.get_current_user(authorization)
            payload = _normalize(body)
            job_id = uuid.uuid4().hex
            now = _now_text()
            job = {
                "job_id": job_id,
                "user_id": user["id"],
                "status": "QUEUED",
                "phase": "QUEUED",
                "date": payload["date"],
                "instrument": payload["instrument"],
                "strategy_mode": payload["strategy_mode"],
                "created_at": now,
                "updated_at": now,
                "result": None,
                "error": None,
            }
            with _DAILY_JOBS_LOCK:
                _DAILY_JOBS[job_id] = job
            background_tasks.add_task(worker, job_id, payload, authorization)
            _trim_jobs()
            return {
                "success": True,
                "async": True,
                "job_id": job_id,
                "status": "QUEUED",
                "date": payload["date"],
                "message": "Daily backtest background me start ho gaya.",
            }
        except Exception as exc:
            return {
                "success": False,
                "message": "Daily job start failed: " + f"{exc.__class__.__name__}: {str(exc)}"[:220],
                "error": str(exc),
            }

    @routes.router.get("/daily/status/{job_id}")
    def daily_backtest_status(job_id: str, authorization: str = Header(None)):
        user = routes.get_current_user(authorization)
        with _DAILY_JOBS_LOCK:
            job = _DAILY_JOBS.get(job_id)
            if not job:
                return {"success": False, "status": "NOT_FOUND", "message": "Daily backtest job nahi mila."}
            if int(job.get("user_id") or 0) != int(user["id"]):
                return {"success": False, "status": "FORBIDDEN", "message": "Ye daily job is user ka nahi hai."}
            return dict(job)

    routes._OKAI_DAILY_JOBS = _DAILY_JOBS
    routes._OKAI_DAILY_JOBS_LOCK = _DAILY_JOBS_LOCK
    routes._okai_daily_job_v1 = True

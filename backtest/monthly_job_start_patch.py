"""Reliable monthly backtest job starter V2.

The original route creates a raw daemon thread inside the request handler. On a
small Railway worker this can fail before the job is queued, which returns only
"Monthly job start failed." This patch replaces that route before the router is
included in FastAPI and uses Starlette/FastAPI BackgroundTasks instead.

It also:
- validates month/capital before queueing;
- expires stale QUEUED/RUNNING jobs so one dead job cannot block future runs;
- returns the exact exception in the user-visible message;
- keeps the existing worker, progress registry and status endpoint unchanged.
"""

from datetime import datetime, timezone

from fastapi import BackgroundTasks, Header

from backtest import routes


STALE_JOB_SECONDS = 45 * 60
_ALLOWED_INSTRUMENTS = {"AUTO", "NIFTY", "BANKNIFTY", "SENSEX"}
_ALLOWED_STRATEGY_MODES = {"NORMAL", "HERO_ZERO", "COMBINED"}


def _utc_now():
    return datetime.now(timezone.utc)


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _is_stale(job, now):
    if str(job.get("status") or "").upper() not in {"QUEUED", "RUNNING"}:
        return False
    updated = _parse_timestamp(job.get("updated_at") or job.get("created_at"))
    if updated is None:
        return True
    return (now - updated).total_seconds() > STALE_JOB_SECONDS


def _normalize_request(body):
    payload = dict(body or {})

    month_text = str(
        payload.get("month")
        or payload.get("year_month")
        or ""
    ).strip()
    try:
        datetime.strptime(month_text, "%Y-%m")
    except Exception as exc:
        raise ValueError("Month YYYY-MM format me daalein.") from exc

    instrument = str(payload.get("instrument") or "AUTO").upper().strip()
    if instrument not in _ALLOWED_INSTRUMENTS:
        instrument = "AUTO"

    strategy_mode = str(
        payload.get("strategy_mode") or "NORMAL"
    ).upper().strip()
    if strategy_mode not in _ALLOWED_STRATEGY_MODES:
        strategy_mode = "NORMAL"

    try:
        capital = float(
            payload.get("capital")
            or payload.get("paper_capital")
            or 100000
        )
    except Exception as exc:
        raise ValueError("Backtest capital invalid hai.") from exc

    if not (capital >= 1000 and capital < 1_000_000_000):
        raise ValueError("Backtest capital kam se kam Rs 1,000 hona chahiye.")

    payload.update({
        "month": month_text,
        "instrument": instrument,
        "strategy_mode": strategy_mode,
        "capital": capital,
        "entry_threshold": 82,
        "sl_percent": 0,
        "target_percent": 0,
    })
    return payload


def _remove_original_monthly_start_route():
    kept = []
    removed = 0

    for route in routes.router.routes:
        path = str(getattr(route, "path", "") or "")
        methods = {
            str(method).upper()
            for method in (getattr(route, "methods", set()) or set())
        }
        if path == "/backtest/monthly" and "POST" in methods:
            removed += 1
            continue
        kept.append(route)

    routes.router.routes[:] = kept
    return removed


def apply_monthly_job_start_patch():
    if getattr(routes, "_okai_monthly_job_start_v2", False):
        return

    removed_routes = _remove_original_monthly_start_route()

    @routes.router.post("/monthly")
    def start_monthly_backtest_v2(
        background_tasks: BackgroundTasks,
        body: dict,
        authorization: str = Header(None),
    ):
        try:
            user = routes.get_current_user(authorization)
            payload = _normalize_request(body)
            now = _utc_now()
            now_text = now.isoformat()

            existing_job = None
            with routes._OKAI_MONTHLY_JOBS_LOCK:
                for existing_id, existing in routes._OKAI_MONTHLY_JOBS.items():
                    if existing.get("user_id") != user["id"]:
                        continue

                    if _is_stale(existing, now):
                        existing.update({
                            "status": "FAILED",
                            "phase": "STALE_JOB_EXPIRED",
                            "error": (
                                "Purana monthly job timeout ke baad automatically "
                                "clear kar diya gaya."
                            ),
                            "updated_at": now_text,
                        })
                        continue

                    if str(existing.get("status") or "").upper() in {
                        "QUEUED",
                        "RUNNING",
                    }:
                        existing_job = (existing_id, dict(existing))
                        break

            if existing_job is not None:
                existing_id, existing = existing_job
                return {
                    "success": True,
                    "async": True,
                    "job_id": existing_id,
                    "status": existing.get("status"),
                    "phase": existing.get("phase"),
                    "message": "Monthly backtest already running.",
                    "starter": "FASTAPI_BACKGROUND_TASK_V2",
                }

            job_id = routes.uuid.uuid4().hex
            job = {
                "job_id": job_id,
                "user_id": user["id"],
                "status": "QUEUED",
                "phase": "QUEUED",
                "month": payload["month"],
                "instrument": payload["instrument"],
                "strategy_mode": payload["strategy_mode"],
                "completed_days": 0,
                "total_days": 0,
                "current_date": None,
                "created_at": now_text,
                "updated_at": now_text,
                "result": None,
                "error": None,
                "starter": "FASTAPI_BACKGROUND_TASK_V2",
            }

            with routes._OKAI_MONTHLY_JOBS_LOCK:
                routes._OKAI_MONTHLY_JOBS[job_id] = job

            try:
                background_tasks.add_task(
                    routes._okai_monthly_worker,
                    job_id,
                    payload,
                    authorization,
                )
            except Exception:
                with routes._OKAI_MONTHLY_JOBS_LOCK:
                    routes._OKAI_MONTHLY_JOBS.pop(job_id, None)
                raise

            routes._okai_trim_monthly_jobs()

            return {
                "success": True,
                "async": True,
                "job_id": job_id,
                "status": "QUEUED",
                "phase": "QUEUED",
                "month": payload["month"],
                "instrument": payload["instrument"],
                "strategy_mode": payload["strategy_mode"],
                "message": "Monthly backtest background me start ho gaya.",
                "starter": "FASTAPI_BACKGROUND_TASK_V2",
            }

        except Exception as exc:
            detail = f"{exc.__class__.__name__}: {str(exc)}"
            return {
                "success": False,
                "message": "Monthly job start failed: " + detail[:220],
                "error": str(exc),
                "error_type": exc.__class__.__name__,
                "starter": "FASTAPI_BACKGROUND_TASK_V2",
            }

    routes.start_monthly_backtest = start_monthly_backtest_v2
    routes._okai_monthly_job_start_route_removed = removed_routes
    routes._okai_monthly_job_start_v2 = True

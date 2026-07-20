import json

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from auth.recovery_routes import (
    _email_service_available,
    consume_registration_email_token,
)


class SafeRegistrationEmailVerificationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            request.method.upper() != "POST"
            or request.url.path.rstrip("/") != "/auth/register"
            or not _email_service_available()
        ):
            return await call_next(request)

        body_bytes = await request.body()
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
            consume_registration_email_token(
                payload.get("email"),
                payload.get("email_verification_token"),
                request,
            )
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
            )
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"detail": "Invalid registration request"},
            )

        async def receive():
            return {
                "type": "http.request",
                "body": body_bytes,
                "more_body": False,
            }

        request._receive = receive
        return await call_next(request)

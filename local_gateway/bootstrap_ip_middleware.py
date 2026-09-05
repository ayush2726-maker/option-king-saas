from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from local_gateway.service import authenticate_gateway


class GatewayBootstrapIPMiddleware(BaseHTTPMiddleware):
    """Extra defense for the only endpoint that returns decrypted broker config.

    A valid per-user gateway token is necessary but not sufficient: the request
    must also originate from that gateway's assigned static IPv4. This keeps
    broker secrets bound to the dedicated AWS worker even if a token is ever
    copied elsewhere.
    """

    async def dispatch(self, request, call_next):
        if request.url.path != "/local-gateway/provision/bootstrap":
            return await call_next(request)

        token = str(request.headers.get("x-gateway-token") or "").strip()
        try:
            gateway = authenticate_gateway(token)
        except Exception:
            return JSONResponse(status_code=401, content={"detail": "Invalid gateway token"})

        expected = str(gateway["expected_static_ip"] or "").strip()
        forwarded = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        real = str(request.headers.get("x-real-ip") or "").strip()
        observed = real or forwarded or str(request.client.host if request.client else "").strip()

        if not expected:
            return JSONResponse(status_code=409, content={"detail": "Dedicated static IP is not assigned yet"})
        if observed != expected:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Gateway bootstrap blocked because source IP does not match the assigned static IP"
                },
            )
        return await call_next(request)

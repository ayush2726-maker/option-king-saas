"""Post-filter /bot/signal-history so score charts never mix instruments.

Legacy rows were allowed through when instrument was NULL/blank.  That could make
an NIFTY chart use an old SENSEX/default score and could also make the mobile app
prefer a flat saved series over the correct candle replay.  The existing route
continues to handle authentication and date filtering; this middleware only
keeps valid positive points for the explicitly requested index.
"""

import json
import math

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class StrictSignalHistoryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)

        if (
            request.url.path != "/bot/signal-history"
            or response.status_code != 200
        ):
            return response

        requested = str(
            request.query_params.get("instrument") or ""
        ).upper().strip()

        # The mobile app always supplies an instrument.  Leave other callers
        # untouched when no explicit index was requested.
        if not requested:
            return response

        try:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return response

        points = payload.get("points")
        if not isinstance(points, list):
            return JSONResponse(
                payload,
                status_code=response.status_code,
                headers={
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() != "content-length"
                },
            )

        filtered = []
        for point in points:
            if not isinstance(point, dict):
                continue

            instrument = str(
                point.get("instrument") or ""
            ).upper().strip()
            if instrument != requested:
                continue

            try:
                score = float(point.get("score"))
                price = float(point.get("price"))
            except Exception:
                continue

            if (
                not math.isfinite(score)
                or score <= 0
                or not math.isfinite(price)
                or price <= 0
            ):
                continue

            clean = dict(point)
            clean["instrument"] = requested
            filtered.append(clean)

        payload["points"] = filtered
        payload["count"] = len(filtered)
        payload["instrument"] = requested
        payload["strict_instrument_filter"] = True

        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() != "content-length"
        }
        return JSONResponse(
            payload,
            status_code=response.status_code,
            headers=headers,
        )

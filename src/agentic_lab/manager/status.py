"""A manager's status endpoint, for the secretary (read-only, tailnet only).

    GET /v1/status?hours=24   {"usage": ..., "events": [...]}   (bearer: the secretary's token)

Workers answer the same over MCP; a manager has no MCP server, so it serves this instead.
"""
import datetime as dt
import hmac

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def app(token, usage, events):
    async def status(request: Request):
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token or not hmac.compare_digest(
                given.encode(), token.encode()):
            return JSONResponse({"error": "unknown token"}, status_code=401)
        try:
            hours = min(float(request.query_params.get("hours", 24)), 24 * 14)
        except ValueError:
            hours = 24
        return JSONResponse({"usage": usage.load(),
                             "events": events.read(since=dt.timedelta(hours=hours), limit=2000)})

    return Starlette(routes=[Route("/v1/status", status, methods=["GET"])])

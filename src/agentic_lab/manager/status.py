"""A manager's status endpoint, for the secretary (tailnet only).

    GET  /v1/status?hours=24   {"usage": ..., "events": [...]}   (bearer: the secretary's token)
    POST /v1/usage/check       {"limits": ...}: read the seat's limits now (one tiny request)

Workers answer the same over MCP; a manager has no MCP server, so it serves this instead.
"""
import datetime as dt
import hmac

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def app(token, usage, events, check=None, github=None):
    def authorized(request):
        scheme, _, given = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and token and hmac.compare_digest(
            given.encode(), token.encode())

    async def status(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "unknown token"}, status_code=401)
        try:
            hours = min(float(request.query_params.get("hours", 24)), 24 * 14)
        except ValueError:
            hours = 24
        return JSONResponse({"usage": usage.load(),
                             "events": events.read(since=dt.timedelta(hours=hours), limit=2000),
                             "github": github() if github is not None else {}})

    async def check_usage(request: Request):
        if not authorized(request):
            return JSONResponse({"error": "unknown token"}, status_code=401)
        if check is None:
            return JSONResponse({"error": "this manager has no seat to check"}, status_code=404)
        try:
            return JSONResponse({"limits": await check()})
        except Exception as e:
            return JSONResponse({"error": f"usage check failed: {e}"}, status_code=502)

    return Starlette(routes=[Route("/v1/status", status, methods=["GET"]),
                             Route("/v1/usage/check", check_usage, methods=["POST"])])

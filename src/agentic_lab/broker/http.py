"""The broker's HTTP API for workers (tailnet only; bearer token = which worker is calling).

    POST /v1/requests            file an approval request -> the request (id, expires, options)
    GET  /v1/requests/{id}?wait= the request; with wait=N, hold up to N s for a decision
    GET  /v1/grants              the calling worker's active grants

Workers pull decisions; there is no way to push one to a worker. A worker sees only its
own requests and grants.
"""
import asyncio
import hmac

from starlette.applications import Starlette
from starlette.requests import Request as HttpRequest
from starlette.responses import JSONResponse
from starlette.routing import Route

from .core import PENDING, Broker, BrokerError

MAX_WAIT_S = 30
MAX_BODY = 64 * 1024


def app(broker: Broker, worker_tokens: dict[str, str]):
    """`worker_tokens`: {token: agent id}."""

    def caller(request: HttpRequest):
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token:
            for known, agent in worker_tokens.items():
                if hmac.compare_digest(known.encode(), token.encode()):
                    return agent
        return None

    def denied():
        return JSONResponse({"error": "unknown token"}, status_code=401)

    async def submit(request: HttpRequest):
        agent = caller(request)
        if not agent:
            return denied()
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        try:
            d = await request.json()
            req = broker.submit(agent, d["human"], d["tool"], d.get("input") or {},
                                task_id=d.get("task_id"), violations=d.get("violations") or [])
        except (BrokerError, KeyError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(req.view())

    async def get(request: HttpRequest):
        agent = caller(request)
        if not agent:
            return denied()
        rid = request.path_params["rid"]
        try:
            wait = min(float(request.query_params.get("wait", 0)), MAX_WAIT_S)
            req = broker.get(rid)
            if req.agent != agent:
                raise BrokerError(f"no request {rid!r}")
            deadline = asyncio.get_running_loop().time() + wait
            while req.state == PENDING and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.5)
                req = broker.get(rid)
        except (BrokerError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        return JSONResponse(req.view())

    async def grants(request: HttpRequest):
        agent = caller(request)
        if not agent:
            return denied()
        return JSONResponse({"agent": agent, "grants": broker.grants(agent)})

    return Starlette(routes=[
        Route("/v1/requests", submit, methods=["POST"]),
        Route("/v1/requests/{rid}", get, methods=["GET"]),
        Route("/v1/grants", grants, methods=["GET"]),
    ])

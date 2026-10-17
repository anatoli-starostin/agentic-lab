"""The broker's HTTP API for agents (tailnet only; bearer token = which agent is calling).

    POST /v1/requests            file an approval request -> the request (id, expires, options)
    GET  /v1/requests/{id}?wait= the request; with wait=N, hold up to N s for a decision
    GET  /v1/grants              the calling agent's active grants
  managers only:
    POST /v1/tasks               register a submitted task: {worker, task_id, requester}
    GET  /v1/pending             pending requests of its tasks (and its own)
    POST /v1/requests/{id}/context   add a note for the human: {text}

Agents pull decisions; there is no way to push one to an agent. An agent sees only its own
requests and grants; a manager also sees, and may annotate, those of its tasks.
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


def app(broker: Broker, tokens: dict[str, str]):
    """`tokens`: {token: agent id}. An agent's role comes from lab.yaml (broker.agents)."""

    def caller(request: HttpRequest):
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token:
            for known, agent in tokens.items():
                if hmac.compare_digest(known.encode(), token.encode()):
                    return agent
        return None

    def role(agent):
        return broker.agents.get(agent, {}).get("role", "worker")

    def denied(status=401, msg="unknown token"):
        return JSONResponse({"error": msg}, status_code=status)

    async def body(request):
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise ValueError("too large")
        return await request.json()

    async def submit(request: HttpRequest):
        agent = caller(request)
        if not agent:
            return denied()
        try:
            d = await body(request)
            req = broker.submit(agent, d["human"], d["tool"], d.get("input") or {},
                                task_id=d.get("task_id"), violations=d.get("violations") or [],
                                role=role(agent))
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
            if req.agent != agent and req.manager != agent:
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

    def manager_only(request):
        agent = caller(request)
        if not agent:
            return None, denied()
        if role(agent) != "manager":
            return None, denied(403, "managers only")
        return agent, None

    async def register(request: HttpRequest):
        mgr, err = manager_only(request)
        if err:
            return err
        try:
            d = await body(request)
            broker.register_task(mgr, d["worker"], d["task_id"], d["requester"])
        except (BrokerError, KeyError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"registered": d["task_id"]})

    async def pending(request: HttpRequest):
        mgr, err = manager_only(request)
        if err:
            return err
        return JSONResponse({"pending": [r.view() for r in broker.for_manager(mgr)]})

    async def context(request: HttpRequest):
        mgr, err = manager_only(request)
        if err:
            return err
        try:
            d = await body(request)
            req = broker.add_context(request.path_params["rid"], str(d["text"]), manager=mgr)
        except (BrokerError, KeyError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(req.view())

    return Starlette(routes=[
        Route("/v1/requests", submit, methods=["POST"]),
        Route("/v1/requests/{rid}", get, methods=["GET"]),
        Route("/v1/requests/{rid}/context", context, methods=["POST"]),
        Route("/v1/grants", grants, methods=["GET"]),
        Route("/v1/tasks", register, methods=["POST"]),
        Route("/v1/pending", pending, methods=["GET"]),
    ])

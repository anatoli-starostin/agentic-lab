"""`lab worker serve`: the worker's system service (DESIGN.md §4.2, §10).

Started by a system unit as the agent's own Unix user (lab-<agent-id>). Reads its
configuration from lab.yaml and its secrets from files only that user can read:

    ~/.config/agentic-lab/manager-tokens.yaml   {manager-id: bearer token} for managers
    ~/.config/agentic-lab/claude-oauth-token    the seat token (`claude setup-token`)
    ~/.config/agentic-lab/broker.yaml           {url: ..., token: ...} for approvals

State (the task queue) lives in ~/.local/state/agentic-lab/tasks.
"""
import asyncio
import datetime as dt
import logging
import os
import signal

import uvicorn
import yaml

from ..registry import load as load_lab
from . import tasks as T
from .approver import BrokerApprover
from .runner import NoChannelApprover, SdkRunner
from .server import Access, Caller, build_mcp, http_app, run_tasks
from .usage import UsageLog

CONFIG = "~/.config/agentic-lab"
STATE = "~/.local/state/agentic-lab"

log = logging.getLogger("agentic_lab.worker")


def _read_secret(path):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return None
    if os.stat(path).st_mode & 0o077:
        raise SystemExit(f"{path} must be readable by its owner only (chmod 600)")
    with open(path) as f:
        return f.read().strip()


def access_for(lab, agent_id, tokens_text):
    """Bearer tokens -> callers, from manager-tokens.yaml, checked against lab.yaml."""
    a = lab.agents[agent_id]
    by_manager = yaml.safe_load(tokens_text or "") or {}
    tokens = {}
    for mgr, token in by_manager.items():
        m = lab.agents.get(mgr)
        if not m or m.get("role") not in ("manager", "secretary"):
            raise SystemExit(f"manager-tokens.yaml: {mgr!r} is not a manager (or the "
                             "secretary) in lab.yaml")
        if not isinstance(token, str) or len(token) < 32:
            raise SystemExit(f"manager-tokens.yaml: token for {mgr} must be 32+ characters")
        # the secretary monitors: read-only, and never subject to "dedicated"
        tokens[token] = Caller(mgr, m["human"], readonly=m.get("role") == "secretary")
    dedicated = a.get("access", "dedicated") == "dedicated"
    return Access(tokens, allowed_humans={a["human"]} if dedicated else None)


async def serve(lab_path, agent_id, host, port, model=None, names=()):
    lab = load_lab(lab_path)
    if agent_id not in lab.agents or lab.agents[agent_id].get("role") != "worker":
        raise SystemExit(f"{agent_id!r} is not a worker in {lab_path}")
    a = lab.agents[agent_id]

    access = access_for(lab, agent_id, _read_secret(f"{CONFIG}/manager-tokens.yaml"))
    store = T.TaskStore(os.path.expanduser(f"{STATE}/tasks"))
    env = {}
    seat_token = _read_secret(f"{CONFIG}/claude-oauth-token")
    if seat_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = seat_token
    workdir = os.path.expanduser(a.get("workdir", "~/work"))
    os.makedirs(workdir, exist_ok=True)
    broker_cfg = yaml.safe_load(_read_secret(f"{CONFIG}/broker.yaml") or "") or {}
    if broker_cfg.get("url") and broker_cfg.get("token"):
        approver = BrokerApprover(broker_cfg["url"], broker_cfg["token"])
    else:
        log.warning("no broker configured: actions outside the sandbox will be denied")
        approver = NoChannelApprover()
    usage = UsageLog(os.path.expanduser(f"{STATE}/usage.json"))

    def settings_for(grants):
        return lab.agent_settings(agent_id, extra_grants=grants,
                                  now=dt.datetime.now(dt.timezone.utc))

    runner = SdkRunner(agent_id, workdir, settings_for, approver=approver,
                       model=model or a.get("model"), env=env, usage=usage)

    async def guardrails():
        active = await approver.active_grants()
        return {"settings": lab.agent_settings(agent_id),
                "standing_grants": a.get("grants") or [], "active_grants": active}

    mcp = build_mcp(agent_id, store, access, guardrails=guardrails, usage=usage,
                    workdir=workdir)
    # the names managers may use to reach us: the bind address, and e.g. the tailnet name
    allowed = [f"{h}:{port}" for h in (host, *names)]
    app, _ = http_app(mcp, access, allowed_hosts=allowed)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info("worker %s: %d manager(s), serving on %s:%d", agent_id,
             len(access._tokens), host, port)
    tasks = asyncio.create_task(run_tasks(store, runner, stop))
    web = asyncio.create_task(server.serve())
    await stop.wait()
    server.should_exit = True
    await web
    tasks.cancel()

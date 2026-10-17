"""`lab broker serve`: the approval broker as a service (DESIGN.md §6.3).

Runs on the secretary host as its own Unix user (lab-broker), never as an agent. Reads:

    /etc/agentic-lab/lab.yaml                    humans (Slack ids, timeouts), workers
    ~/.config/agentic-lab/agent-tokens.yaml      {agent-id: token} for workers and managers
    ~/.config/agentic-lab/slack.env              SLACK_BOT_TOKEN=xoxb-..., SLACK_APP_TOKEN=xapp-...
                                                 (the "Lab Approvals" Slack app)
State (requests, grants) lives in ~/.local/state/agentic-lab/broker.
"""
import asyncio
import logging
import os

import uvicorn
import yaml

from ..registry import load as load_lab
from ..worker.service import _read_secret
from .core import DEFAULT_TIMEOUT_S, Broker
from .http import app

CONFIG = "~/.config/agentic-lab"
STATE = "~/.local/state/agentic-lab/broker"
EXPIRE_EVERY_S = 2

log = logging.getLogger("agentic_lab.broker")


def humans_from_lab(lab):
    out = {}
    for hid, h in lab.humans.items():
        h = h or {}
        dm = next((c for c in h.get("channels") or [] if c.get("type") == "slack_dm"), {})
        out[hid] = {"slack_user": h.get("slack_user"),
                    "timeout_s": dm.get("timeout_s", DEFAULT_TIMEOUT_S)}
    return out


def tokens_from(lab, text):
    by_agent = yaml.safe_load(text or "") or {}
    tokens = {}
    for agent, token in by_agent.items():
        if lab.agents.get(agent, {}).get("role") not in ("worker", "manager", "secretary"):
            raise SystemExit(f"agent-tokens.yaml: {agent!r} is not an agent in lab.yaml")
        if not isinstance(token, str) or len(token) < 32:
            raise SystemExit(f"agent-tokens.yaml: token for {agent} must be 32+ characters")
        tokens[token] = agent
    return tokens


def _env_file(text):
    out = {}
    for line in (text or "").splitlines():
        k, _, v = line.strip().partition("=")
        if k and not k.startswith("#"):
            out[k] = v
    return out


async def serve(lab_path, host, port):
    lab = load_lab(lab_path)
    broker = Broker(os.path.expanduser(STATE), humans_from_lab(lab), lab.agents)
    tokens = tokens_from(lab, _read_secret(f"{CONFIG}/agent-tokens.yaml")
                         or _read_secret(f"{CONFIG}/worker-tokens.yaml"))
    slack_env = _env_file(_read_secret(f"{CONFIG}/slack.env"))

    channel = None
    if slack_env.get("SLACK_BOT_TOKEN") and slack_env.get("SLACK_APP_TOKEN"):
        from .slack import SlackChannel
        channel = SlackChannel(broker, slack_env["SLACK_BOT_TOKEN"], slack_env["SLACK_APP_TOKEN"])
        await channel.start()
    else:
        log.warning("no Slack tokens: nobody can be asked, every request will time out")

    server = uvicorn.Server(uvicorn.Config(app(broker, tokens), host=host, port=port,
                                           log_level="info"))
    # uvicorn handles SIGTERM/SIGINT (a graceful shutdown); when it returns, the rest stops
    stop = asyncio.Event()

    async def expire_loop():
        while not stop.is_set():
            for req in broker.expire():
                log.info("request %s timed out", req.id)
            await asyncio.sleep(EXPIRE_EVERY_S)

    log.info("broker: %d agent(s), %d human(s), slack=%s, serving on %s:%d",
             len(tokens), len(broker.humans), bool(channel), host, port)
    expiring = asyncio.create_task(expire_loop())
    await server.serve()
    stop.set()
    if channel:
        await channel.stop()
    expiring.cancel()

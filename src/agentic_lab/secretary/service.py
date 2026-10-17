"""`lab secretary serve`: the secretary's service (DESIGN.md §4.1).

For now: collects the lab's state every few seconds and serves the web UI on the tailnet.
(The secretary's chat agent in Slack, VM power and token-expiry notices come next.)
Secrets, readable by this service only:

    ~/.config/agentic-lab/monitor.yaml   {worker-id: read-only token}
    ~/.config/agentic-lab/broker.yaml    {url, token} (read-only overview)
"""
import asyncio
import json
import logging
import os
import signal
import tempfile

import uvicorn
import yaml

from ..registry import load as load_lab
from ..worker.service import _read_secret
from ..manager.workers import Workers
from .status import collect
from .web import app

CONFIG = "~/.config/agentic-lab"
STATE = "~/.local/state/agentic-lab"
COLLECT_EVERY_S = 15
log = logging.getLogger("agentic_lab.secretary")


async def serve(lab_path, host, port):
    lab = load_lab(lab_path)
    mon = yaml.safe_load(_read_secret(f"{CONFIG}/monitor.yaml") or "") or {}
    workers = Workers({w: lab.worker_url(w) for w in mon if w in lab.agents}, mon, timeout=15)
    bcfg = yaml.safe_load(_read_secret(f"{CONFIG}/broker.yaml") or "") or {}
    broker = (bcfg["url"], bcfg["token"]) if bcfg.get("url") and bcfg.get("token") else None
    here = {aid for aid, a in lab.agents.items()
            if a.get("host") == next((x.get("host") for x in lab.agents.values()
                                      if x.get("role") == "secretary"), None)}
    units = {aid: {"worker": f"lab-worker@{aid}", "manager": f"lab-manager@{aid}"}.get(
        lab.agents[aid].get("role")) for aid in here}
    units = {k: v for k, v in units.items() if v}
    snapshot_path = os.path.expanduser(f"{STATE}/lab-state.json")
    os.makedirs(os.path.dirname(snapshot_path), mode=0o700, exist_ok=True)
    latest = {"state": None}
    try:
        with open(snapshot_path) as f:
            latest["state"] = json.load(f)
    except (OSError, ValueError):
        pass

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def collector():
        while not stop.is_set():
            try:
                latest["state"] = await collect(lab, workers, broker, units)
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(snapshot_path))
                with os.fdopen(fd, "w") as f:
                    json.dump(latest["state"], f)
                os.replace(tmp, snapshot_path)
            except Exception:
                log.exception("collecting the lab's state failed")
            try:
                await asyncio.wait_for(stop.wait(), COLLECT_EVERY_S)
            except asyncio.TimeoutError:
                pass

    server = uvicorn.Server(uvicorn.Config(app(lambda: latest["state"]), host=host, port=port,
                                           log_level="warning"))
    log.info("secretary: watching %d worker(s), web UI on http://%s:%d/", len(workers.known()),
             host, port)
    tasks = [asyncio.create_task(collector()), asyncio.create_task(server.serve())]
    await stop.wait()
    server.should_exit = True
    for t in tasks:
        t.cancel()

"""The lab's state, as the secretary sees it: collected from every agent, every few seconds.

Workers are asked over MCP with the secretary's read-only tokens; the broker for pending
approvals and grants; systemd for the services on this host. Usage is aggregated per
account (each member's Claude seat): the seat's limit windows are shared by all its
agents, so the freshest reading wins, and every number keeps when and from which agent it
came.
"""
import datetime as dt
import subprocess

import httpx2

from ..manager.workers import WorkerError, Workers


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _unit_state(unit):
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True,
                           timeout=5)
        return r.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


async def broker_overview(url, token):
    try:
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                      timeout=10) as c:
            r = await c.get(f"{url.rstrip('/')}/v1/overview")
            r.raise_for_status()
            return r.json()
    except (httpx2.HTTPError, ValueError) as e:
        return {"error": str(e)}


async def manager_status(lab, manager, token, hours=0):
    """A manager's /v1/status (usage, and events for the last `hours`)."""
    name, port = lab.address(manager)
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=10) as c:
        r = await c.get(f"http://{name}:{port}/v1/status", params={"hours": hours})
        r.raise_for_status()
        return r.json()


async def collect(lab, workers: Workers, broker=None, local_units=None, managers=None):
    """One snapshot of the lab. `managers`: {manager-id: the secretary's token there}."""
    """One snapshot of the lab (a dict, ready for JSON and the web page)."""
    agents = {}
    for aid, a in lab.agents.items():
        agents[aid] = {"id": aid, "name": lab.display_name(aid), "role": a.get("role"),
                       "human": a.get("human"), "host": a.get("host"),
                       "access": a.get("access")}
    for m, token in (managers or {}).items():
        try:
            st = await manager_status(lab, m, token)
            agents[m].update(up=True, usage=st.get("usage"))
        except (httpx2.HTTPError, ValueError) as e:
            agents[m].update(up=False, error=str(e))
    for w in workers.known():
        try:
            h = await workers.call(w, "health_check", retries=0)
            tasks = await workers.call(w, "list_tasks", retries=0)
            agents[w].update(up=True, gpu=h.get("gpu"), queue=h.get("tasks"),
                             usage=h.get("usage"), disk_free_gb=h.get("disk_free_gb"),
                             recent=[{k: t.get(k) for k in ("id", "state", "requester",
                                                            "manager", "created", "finished",
                                                            "instruction")}
                                     for t in (tasks or [])[-8:]][::-1])
        except WorkerError as e:
            agents[w].update(up=False, error=str(e))
    for aid, unit in (local_units or {}).items():
        agents.setdefault(aid, {})["service"] = _unit_state(unit)

    accounts = {}
    for hid, h in lab.humans.items():
        mine = [aid for aid, a in agents.items() if a.get("human") == hid]
        limits, tokens_today, today = {}, {}, dt.date.today().isoformat()
        for aid in mine:
            u = agents[aid].get("usage") or {}
            for kind, lim in (u.get("limits") or {}).items():
                if kind not in limits or lim.get("observed_at", "") > limits[kind].get("observed_at", ""):
                    limits[kind] = {**lim, "from": aid}
            for model, t in ((u.get("days") or {}).get(today) or {}).items():
                acc = tokens_today.setdefault(aid, 0)
                tokens_today[aid] = acc + sum(t.values())
        accounts[hid] = {"name": (h or {}).get("name") or hid, "agents": mine,
                         "limits": limits, "tokens_today": tokens_today}

    approvals = await broker_overview(*broker) if broker else {"error": "no broker configured"}
    return {"lab": lab.name, "at": _now(), "agents": agents, "accounts": accounts,
            "approvals": approvals}
